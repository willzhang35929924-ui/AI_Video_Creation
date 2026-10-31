"""Freeze reusable library assets without creating a parallel screenplay database."""
import copy
import hashlib
import json
import base64
from io import BytesIO
from PIL import Image
from sqlalchemy import select
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models.screenplay_generation import ScreenplayInputAsset
from app.services.screenplay_generation_media import image_data,sha

FIELDS=('asset_ids','product_selections','product_mode','marketing_template','reference_video_ids','style_profile_id','video_creation_mode','new_theme','primary_reference_video_id','allow_cloud_media_upload')
def request_fields(payload):
    data=payload.model_dump()
    return {key:copy.deepcopy(data[key]) for key in FIELDS}

def frozen_sources(session,settings,payload):
    extra={'input_request':request_fields(payload),'allow_cloud_media_upload':payload.allow_cloud_media_upload,'temporary_source_cleanup':True}
    if payload.product_mode=='separate':raise AppError(422,'product_separate_batch','分别生成请使用批量任务入口')
    if payload.product_selections:
        from app.services.product_library import freeze_products
        extra['products']=freeze_products(session,settings,payload.product_selections)
    identifiers=payload.asset_ids or ([payload.asset_id] if payload.asset_id else [])
    if identifiers:
        assets=[]
        for identifier in identifiers:
            asset=session.get(ScreenplayInputAsset,identifier)
            if not asset or asset.input_mode!=payload.input_mode:raise AppError(422,'screenplay_asset_mode','素材不属于本次输入模式')
            path=safe_media_path(settings.media_root,asset.relative_path,must_exist=True)
            if sha(path.read_bytes())!=asset.sha256:raise AppError(409,'screenplay_source_changed','输入素材已变化')
            assets.append({'id':asset.id,'relative_path':asset.relative_path,'sha256':asset.sha256,'filename':asset.filename,'metadata':asset.metadata_json})
        extra['input_assets']=assets
    if payload.reference_video_ids:
        from app.services.reference_video_analysis import freeze_reference_context
        extra['reference_context']=freeze_reference_context(session,settings,payload.reference_video_ids,style_profile_id=payload.style_profile_id,
            primary_video_id=payload.primary_reference_video_id,request_key=payload.idempotency_key)
    return extra

def prepare_images(settings,job):
    chosen=[]
    for product in job.snapshot_json.get('products',[]):
        chosen.extend({**item,'product_id':product['id']} for item in product['images'])
    if not chosen:chosen=job.snapshot_json.get('input_assets',[])
    if not 1<=len(chosen)<=8:raise AppError(422,'screenplay_image_count','本次模型需要1至8张实际图片')
    content=[];proof=[]
    for item in chosen:
        path=safe_media_path(settings.media_root,item['relative_path'],must_exist=True)
        if sha(path.read_bytes())!=item['sha256']:raise AppError(409,'product_source_changed','已冻结图片字节发生变化')
        content.append({'type':'text','text':'下张图片属于产品 '+item.get('product_id','创作素材')+'，素材ID '+item['id']+'，用途 '+item.get('purpose','other')+'。不得把不同产品合并成同一个商品。'})
        part,digest=image_data(path,max_side=384 if len(chosen)>2 else 768);content.append(part)
        with Image.open(BytesIO(base64.b64decode(part['image_url']['url'].split(',',1)[1]))) as decoded:rgb_sha=sha(decoded.convert('RGB').tobytes())
        proof.append({k:v for k,v in item.items() if k not in {'relative_path','absolute_path'}}|{'request_image_sha256':digest,'request_rgb_sha256':rgb_sha})
    combined=sha(json.dumps([item['request_image_sha256'] for item in proof],separators=(',',':')).encode())
    return content,{'input_mode':'image','images':proof,'image_request_sha256':combined,'products':job.snapshot_json.get('products',[]),'frames':[],'source_kind':'product_library' if job.snapshot_json.get('products') else 'multiple_uploaded_images','analysis_image_max_side':384 if len(chosen)>2 else 768,'original_reference_images_unchanged':True}

def source_input(job):
    source=copy.deepcopy(job.snapshot_json.get('input_request',{}))
    # Historical single-input jobs have no extension snapshot.
    if not any(source.get(key) for key in ('asset_ids','product_selections','reference_video_ids')):
        source['asset_id']=job.input_asset_id
    return source

def cleanup_temporary_source(settings,job,source):
    if not job.snapshot_json.get('temporary_source_cleanup'):return source
    folder=safe_media_path(settings.media_root,f'screenplay_jobs/{job.id}/source_frames')
    root=safe_media_path(settings.media_root,f'screenplay_jobs/{job.id}').resolve()
    if not folder.resolve().is_relative_to(root):raise AppError(422,'screenplay_cleanup_scope','临时文件清理目录越界')
    errors=[]
    if folder.exists():
        for path in folder.iterdir():
            if path.is_symlink() or path.parent.resolve()!=folder.resolve() or not path.is_file():continue
            # Only this new job's generated temporary frames and extracted audio.
            if not (path.name.startswith('frame_') and path.suffix=='.jpg' or path.suffix=='.wav'):continue
            try:path.unlink()
            except OSError as error:errors.append({'file':path.name,'error':type(error).__name__})
        try:folder.rmdir()
        except OSError:pass
    return {**source,'temporary_artifacts_cleaned':not errors,'temporary_cleanup_errors':errors,'cache_policy':'derivative frames/audio removed after use; original library files and structured evidence retained'}

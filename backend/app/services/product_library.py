"""Products reuse validated screenplay images and freeze actual visual inputs."""
import copy
import io
import json
import datetime as dt
from pathlib import Path
from PIL import Image,ImageOps
from sqlalchemy import select,update
from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models.entities import new_id
from app.models.product_library import Product,ProductImage,ProjectProductBinding,ShotProductBinding
from app.models.screenplay_generation import ScreenplayInputAsset,ScreenplayGenerationJob
from app.services.screenplay_generation_media import upload_asset,sha

PURPOSES={'main','front','back','side','detail','usage','packaging','dimensions','other'}
LIMITS={'max_images_per_product':32,'max_generation_images':8,'max_upload_bytes':10*1024**2,'formats':['.png','.jpg','.jpeg','.webp']}

def get_product(session,identifier):
    item=session.get(Product,identifier)
    if not item:raise AppError(404,'product_missing','产品不存在')
    return item

def images(session,product_id):
    return list(session.scalars(select(ProductImage).where(ProductImage.product_id==product_id).order_by(ProductImage.order,ProductImage.id)))

def image_dto(session,item):
    asset=session.get(ScreenplayInputAsset,item.asset_id)
    return {'id':item.id,'asset_id':item.asset_id,'product_id':item.product_id,'filename':asset.filename,
        'media_url':'/api/media/'+asset.relative_path,'thumbnail_url':'/api/media/'+(item.thumbnail_relative_path or asset.relative_path),
        'sha256':asset.sha256,'width':asset.metadata_json['width'],'height':asset.metadata_json['height'],'bytes':asset.metadata_json['bytes'],
        'purpose':item.purpose,'order':item.order,'notes':item.notes}

def dto(session,item):
    note=session.scalar(select(m.ContentNote).where(m.ContentNote.resource=='product_file_analysis',m.ContentNote.record_id==item.id))
    try:analysis=json.loads(note.notes) if note else None
    except (TypeError,ValueError):analysis=None
    if analysis and {i['image_id'] for i in analysis.get('images',[])}!={i.id for i in images(session,item.id)}:analysis={**analysis,'status':'stale','reason':'产品图片列表已变化，请重新检查'}
    return {'id':item.id,'name':item.name,'brand':item.brand,'category':item.category,'notes':item.notes,
        'selling_points':item.selling_points_json,'evidence_notes':item.evidence_notes,'target_audience':item.target_audience,
        'state':item.state,'revision':item.revision,'analysis':analysis,'cover_image_id':item.cover_image_id,'images':[image_dto(session,i) for i in images(session,item.id)],
        'created_at':item.created_at.isoformat(),'updated_at':item.updated_at.isoformat()}

def list_products(session,q='',category='',include_archived=False):
    result=[]
    for item in session.scalars(select(Product).order_by(Product.created_at.desc())):
        if not include_archived and item.state=='archived':continue
        if category and item.category!=category:continue
        if q.casefold() not in (' '.join([item.name,item.brand,item.category,item.notes,*item.selling_points_json])).casefold():continue
        result.append(dto(session,item))
    return {'items':result,'limits':LIMITS,'purposes':sorted(PURPOSES)}

def create_product(session,payload):
    data=payload.model_dump();data['selling_points_json']=data.pop('selling_points')
    product=Product(**data);session.add(product);session.commit();return dto(session,product)

def reanalyze(session,settings,identifier):
    product=get_product(session,identifier);current=images(session,identifier)
    if len(current)>LIMITS['max_images_per_product']:raise AppError(422,'product_inspection_limit','图片数量超过图库上限，请明确分组；文件检查不会调用语义模型')
    result=[]
    for item in current:
        asset=session.get(ScreenplayInputAsset,item.asset_id)
        try:
            path=safe_media_path(settings.media_root,asset.relative_path,must_exist=True)
            if sha(path.read_bytes())!=asset.sha256:raise ValueError('sha256_mismatch')
            with Image.open(path) as picture:
                picture.load();size=picture.size;picture=ImageOps.exif_transpose(picture).convert('RGB');picture.thumbnail((32,32))
                pixels=list(picture.getdata());mean=[round(sum(p[c] for p in pixels)/len(pixels),2) for c in range(3)]
            result.append({'image_id':item.id,'sha256':asset.sha256,'status':'verified','width':size[0],'height':size[1],'whole_image_mean_rgb':mean})
        except (AppError,OSError,ValueError) as error:result.append({'image_id':item.id,'status':'unavailable','error':getattr(error,'code',type(error).__name__)})
    note=session.scalar(select(m.ContentNote).where(m.ContentNote.resource=='product_file_analysis',m.ContentNote.record_id==identifier))
    old={}
    if note:
        try:old=json.loads(note.notes)
        except ValueError:pass
    analysis={'version':old.get('version',0)+1,'analyzed_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'method':'local_full_image_decode_sha256_pixel_statistics',
        'status':'succeeded' if result and all(r['status']=='verified' for r in result) else 'needs_attention','images':result,
        'scope':'文件可读性与整幅图像统计；统计颜色不等于产品分割颜色，不推断功效/容量/规格。语义营销建议通过图库模型生成任务单独记录。','model_calls':0}
    if not note:note=m.ContentNote(resource='product_file_analysis',record_id=identifier,label='产品文件本地重新检查');session.add(note)
    note.notes=json.dumps(analysis,ensure_ascii=False)
    if product.state!='archived':
        if analysis['status']=='needs_attention' and result:product.state='needs_attention'
        elif product.state=='needs_attention':product.state='active'
    product.revision+=1;session.commit();return dto(session,product)

def edit_product(session,identifier,payload):
    product=get_product(session,identifier);values=payload.model_dump(exclude_unset=True)
    revision=values.pop('expected_revision')
    if values.get('name') is not None and not values['name'].strip():raise AppError(422,'product_name_blank','产品名称不能为空')
    if 'selling_points' in values:
        points=values.pop('selling_points')
        if points is None or any(not p.strip() or len(p)>2000 for p in points):raise AppError(422,'product_claim_invalid','每条卖点需为1至2000字')
        values['selling_points_json']=points
    if values.get('cover_image_id') and values['cover_image_id'] not in {i.id for i in images(session,identifier)}:
        raise AppError(422,'product_cover_scope','封面必须属于当前产品')
    if any(v is None for k,v in values.items() if k!='cover_image_id'):raise AppError(422,'product_field_empty','产品字段不能设为null')
    result=session.execute(update(Product).where(Product.id==identifier,Product.revision==revision).values(**values,revision=revision+1))
    if result.rowcount!=1:raise AppError(409,'product_revision_conflict','产品已被其他窗口修改，请刷新后再保存')
    session.commit();session.refresh(product);return dto(session,product)

def _add_image(session,settings,product_id,filename,data,purpose='other',notes=''):
    if purpose not in PURPOSES:raise AppError(422,'product_image_purpose','图片用途无效')
    product=get_product(session,product_id)
    if product.state=='archived':raise AppError(409,'product_archived','请先明确恢复产品，再添加图片')
    # A project-independent product image must not silently reuse another
    # product's label; only the immutable underlying bytes may be shared.
    session.execute(update(Product).where(Product.id==product_id).values(revision=Product.revision+1))
    current=images(session,product_id);digest=sha(data)
    for item in current:
        if session.get(ScreenplayInputAsset,item.asset_id).sha256==digest:
            session.rollback();return image_dto(session,item),True
    if len(current)>=32:raise AppError(422,'product_image_limit','一个产品最多保存32张图片')
    asset=session.scalar(select(ScreenplayInputAsset).where(ScreenplayInputAsset.sha256==digest,ScreenplayInputAsset.input_mode=='image').limit(1))
    if asset:
        path=safe_media_path(settings.media_root,asset.relative_path,must_exist=True)
        if sha(path.read_bytes())!=digest:raise AppError(409,'product_source_changed','共享原图校验失败')
    else:
        asset=upload_asset(session,settings,'image',filename,data,'产品图库原图；'+notes,commit=False)
        asset.metadata_json={**asset.metadata_json,'library_owner':'product_library'}
    item=ProductImage(id=new_id('pimage'),product_id=product_id,asset_id=asset.id,purpose=purpose,order=max([i.order for i in current],default=0)+1,notes=notes)
    relative=f'product_library/{product_id}/{item.id}/thumbnail.jpg';path=safe_media_path(settings.media_root,relative)
    session.info.setdefault('product_upload_paths',[]).append(relative)
    path.parent.mkdir(parents=True,exist_ok=True)
    with Image.open(io.BytesIO(data)) as picture:
        picture=ImageOps.exif_transpose(picture).convert('RGB');picture.thumbnail((320,320));picture.save(path,format='JPEG',quality=85)
    item.thumbnail_relative_path=relative;session.add(item)
    if not product.cover_image_id:product.cover_image_id=item.id
    session.add(m.ManagedFile(relative_path=relative,state='active',ownership_json=[{'table':'product_images','id':item.id}],metadata_json={'verified_owned':True,'sha256':sha(path.read_bytes())}))
    session.commit();return image_dto(session,item),False

def add_image(session,settings,product_id,filename,data,purpose='other',notes=''):
    session.info['product_upload_paths']=[]
    try:return _add_image(session,settings,product_id,filename,data,purpose,notes)
    except Exception as original:
        session.rollback()
        for relative in session.info['product_upload_paths']:
            path=safe_media_path(settings.media_root,relative)
            try:
                if path.is_file() and not path.is_symlink():path.unlink()
                if path.parent.exists():path.parent.rmdir()
            except OSError as error:
                if path.is_file():
                    session.add(m.ManagedFile(relative_path=relative,state='cleanup_failed',ownership_json=[],metadata_json={'verified_owned':True,'failed_upload':True,'error':type(error).__name__}))
                    session.commit()
        if isinstance(original,AppError):raise
        raise AppError(422,'product_image_store_failed','产品图片保存失败，未登记正常图片；临时文件已清理或进入待清理登记') from original
    finally:session.info.pop('product_upload_paths',None)

def edit_images(session,product_id,payload):
    product=get_product(session,product_id);current={item.id:item for item in images(session,product_id)}
    keys=[item.id for item in payload.images]
    if len(set(keys))!=len(keys) or set(keys)!=set(current):raise AppError(422,'product_image_scope','排序须包含当前产品的全部图片且不能重复；删除请使用明确删除入口')
    if len({item.order for item in payload.images})!=len(keys):raise AppError(422,'product_image_order','图片顺序不能重复')
    if payload.cover_image_id and payload.cover_image_id not in current:raise AppError(422,'product_cover_scope','封面不属于当前产品')
    changed=session.execute(update(Product).where(Product.id==product_id,Product.revision==payload.expected_revision).values(revision=Product.revision+1))
    if changed.rowcount!=1:raise AppError(409,'product_revision_conflict','产品已更新，请刷新')
    for value in payload.images:
        item=current[value.id];item.purpose=value.purpose;item.order=value.order;item.notes=value.notes
    if payload.cover_image_id is not None:product.cover_image_id=payload.cover_image_id
    session.commit();return dto(session,product)

def contains(value,identifier):
    if isinstance(value,dict):return identifier in value or any(contains(v,identifier) for v in value.values())
    if isinstance(value,list):return any(contains(v,identifier) for v in value)
    return value==identifier

def references(session,identifier):
    get_product(session,identifier);result=[]
    for item in session.scalars(select(ProjectProductBinding).where(ProjectProductBinding.product_id==identifier)):
        result.append({'kind':'project','id':item.project_id,'binding_id':item.id,'source_job_id':item.source_job_id})
    for job in session.scalars(select(ScreenplayGenerationJob)):
        if contains(job.snapshot_json.get('products',[]),identifier):result.append({'kind':'screenplay_job','id':job.id,'status':job.status,'project_id':job.project_id})
    return {'references':result,'protected':bool(result)}

def freeze_products(session,settings,selections):
    if len({s.product_id for s in selections})!=len(selections):raise AppError(422,'duplicate_product_selection','同一产品不能重复选择')
    result=[];count=0
    for selection in selections:
        product=get_product(session,selection.product_id)
        if product.state!='active':raise AppError(409,'product_archived','已移出图库的产品不能用于新任务')
        current=images(session,product.id);selected=selection.image_ids or [i.id for i in current]
        if len(set(selected))!=len(selected) or set(selected)-{i.id for i in current}:raise AppError(422,'product_image_scope','所选图片不属于此产品或有重复')
        if not selected:raise AppError(422,'product_images_missing','产品尚未保存图片')
        chosen=[]
        for item in current:
            if item.id not in selected:continue
            asset=session.get(ScreenplayInputAsset,item.asset_id);path=safe_media_path(settings.media_root,asset.relative_path,must_exist=True)
            if sha(path.read_bytes())!=asset.sha256:raise AppError(409,'product_source_changed','所选产品原图发生变化')
            chosen.append({**image_dto(session,item),'relative_path':asset.relative_path})
        count+=len(chosen)
        result.append({k:v for k,v in dto(session,product).items() if k!='images'}|{'images':chosen,'claims_status':'user_provided_unverified','factual_evidence':product.evidence_notes})
    if count>8:raise AppError(422,'product_model_image_limit','本次剧本模型最多接入8张图片，请从产品中选择部分图片')
    return result

def bind_created_project(session,job,result):
    products=job.snapshot_json.get('products',[])
    if not products:return
    shot_ids=result.get('shot_ids',[]);shot_ir=job.screenplay_json['shots']
    for product in products:
        if not session.scalar(select(ProjectProductBinding).where(ProjectProductBinding.project_id==job.project_id,ProjectProductBinding.product_id==product['id'])):
            session.add(ProjectProductBinding(project_id=job.project_id,product_id=product['id'],source_job_id=job.id,snapshot_json=copy.deepcopy(product)))
    mapping={p['id']:p for p in products}
    for source,shot_id in zip(shot_ir,shot_ids):
        visible=source.get('products',[])
        if not visible and len(products)==1:visible=[{'product_id':products[0]['id'],'visible_state':'模型未单独填写，需人工确认'}]
        for value in visible:
            identifier=value.get('product_id');product=mapping.get(identifier)
            if not product:raise AppError(422,'shot_product_scope','镜头引用未选中的产品')
            state={k:v for k,v in value.items() if k not in {'keyframe_image_ids','video_image_ids','reference_selection_confirmed'}}
            session.add(ShotProductBinding(project_id=job.project_id,shot_id=shot_id,product_id=identifier,image_ids_json=[i['id'] for i in product['images']],state_json=state))

def shot_references(session,settings,shot,*,stage='video'):
    result=[]
    for binding in session.scalars(select(ShotProductBinding).where(ShotProductBinding.shot_id==shot.id).order_by(ShotProductBinding.product_id)):
        project=session.scalar(select(ProjectProductBinding).where(ProjectProductBinding.project_id==shot.project_id,ProjectProductBinding.product_id==binding.product_id))
        if not project:raise AppError(409,'product_binding_missing','镜头产品缺少项目资料')
        chosen=set(binding.state_json.get('keyframe_image_ids',binding.image_ids_json) if stage=='keyframe' else binding.image_ids_json)
        if chosen-{item['id'] for item in project.snapshot_json.get('images',[])}:raise AppError(409,'product_frozen_image_missing','镜头所选产品图不在冻结项目资料中')
        for item in project.snapshot_json.get('images',[]):
            if item['id'] not in chosen:continue
            path=safe_media_path(settings.media_root,item['relative_path'],must_exist=True)
            if sha(path.read_bytes())!=item['sha256']:raise AppError(409,'product_source_changed','冻结产品参考图发生变化')
            result.append({'id':item['id'],'product_id':binding.product_id,'source':'product_library','purpose':item['purpose'],'order':item['order'],
                'relative_path':item['relative_path'],'absolute_path':str(path),'sha256':item['sha256'],'product_name':project.snapshot_json['name'],'visible_state':binding.state_json})
    return result

def shot_product_snapshot(session,shot):
    result=[]
    for binding in session.scalars(select(ShotProductBinding).where(ShotProductBinding.shot_id==shot.id).order_by(ShotProductBinding.product_id)):
        project=session.scalar(select(ProjectProductBinding).where(ProjectProductBinding.project_id==shot.project_id,ProjectProductBinding.product_id==binding.product_id))
        if project:result.append({'product_id':binding.product_id,'state':binding.state_json,'images':[{k:i[k] for k in ('id','sha256','relative_path','purpose','order')} for i in project.snapshot_json.get('images',[]) if i['id'] in binding.image_ids_json]})
    return result

def reference_selection(session,project_id,shot_id):
    shot=session.get(m.StoryboardShot,shot_id)
    if not shot or shot.project_id!=project_id:raise AppError(404,'product_shot_scope','镜头不属于当前项目')
    result=[]
    for binding in session.scalars(select(ShotProductBinding).where(ShotProductBinding.shot_id==shot_id).order_by(ShotProductBinding.product_id)):
        project=session.scalar(select(ProjectProductBinding).where(ProjectProductBinding.project_id==project_id,ProjectProductBinding.product_id==binding.product_id))
        if not project:raise AppError(409,'product_binding_missing','镜头产品缺少项目资料')
        result.append({'product_id':binding.product_id,'name':project.snapshot_json['name'],'revision':binding.revision,
            'images':project.snapshot_json.get('images',[]),'video_image_ids':binding.image_ids_json,
            'keyframe_image_ids':binding.state_json.get('keyframe_image_ids',binding.image_ids_json)})
    return {'project_id':project_id,'shot_id':shot_id,'products':result,'limits':{'keyframe_total_max':4,'video_total_max':9},'note':'总预算还包括人物与场景图，实际提交前再次校验；不自动截断图片'}

def save_reference_selection(session,project_id,shot_id,payload):
    current=reference_selection(session,project_id,shot_id)
    active={'queued','submitting','submitted','running','cancelling'}
    for model in (m.GenerationJob,m.ProductionVisual,m.MediaProcessTask):
        if session.scalar(select(model.id).where(model.project_id==project_id,model.status.in_(active)).limit(1)):raise AppError(409,'product_binding_busy','项目仍有制作任务，请等待结束后修改参考')
    from app.services.studio_pipeline import latest_run,RUN_TERMINAL
    run=latest_run(session,project_id)
    if run and run.status not in RUN_TERMINAL|{'paused','exceptions'}:raise AppError(409,'product_binding_busy','自动制作仍在执行')
    mapping={p['product_id']:p for p in current['products']}
    if len({p.product_id for p in payload.products})!=len(payload.products) or {p.product_id for p in payload.products}!=set(mapping):raise AppError(422,'product_reference_scope','请提交当前镜头全部已绑定产品')
    for value in payload.products:
        known=mapping[value.product_id];available={i['id'] for i in known['images']}
        for selected in (value.video_image_ids,value.keyframe_image_ids):
            if len(set(selected))!=len(selected) or set(selected)-available:raise AppError(422,'product_reference_scope','图片不属于本镜冻结产品或存在重复')
        row=session.scalar(select(ShotProductBinding).where(ShotProductBinding.shot_id==shot_id,ShotProductBinding.product_id==value.product_id))
        changed=session.execute(update(ShotProductBinding).where(ShotProductBinding.id==row.id,ShotProductBinding.revision==value.expected_revision).values(image_ids_json=value.video_image_ids,state_json={**row.state_json,'keyframe_image_ids':value.keyframe_image_ids,'reference_selection_confirmed':True},revision=ShotProductBinding.revision+1))
        if changed.rowcount!=1:raise AppError(409,'product_reference_revision','镜头产品参考已被修改，请刷新')
    session.add(m.AuditEvent(object_type='storyboard_shot',object_id=shot_id,action='product_reference_selection_saved',details_json=payload.model_dump()))
    session.commit();return reference_selection(session,project_id,shot_id)


def bind_existing_project(session, settings, project_id, payload):
    """Bind explicitly selected real product photos to existing draft shots.

    Snapshots are immutable. Existing visuals remain untouched; their input
    hashes will correctly become stale and require review/regeneration.
    """
    if not session.get(m.Project, project_id):
        raise AppError(404, 'project_missing', '项目不存在')
    active={'queued','submitting','submitted','running','cancelling'}
    for model in (m.GenerationJob,m.ProductionVisual,m.MediaProcessTask):
        if session.scalar(select(model.id).where(model.project_id==project_id,model.status.in_(active)).limit(1)):
            raise AppError(409,'product_binding_busy','项目仍有制作任务，请等待结束后修改产品参考')
    from app.services.studio_pipeline import latest_run,RUN_TERMINAL
    run=latest_run(session,project_id)
    if run and run.status not in RUN_TERMINAL|{'paused','exceptions'}:
        raise AppError(409,'product_binding_busy','项目自动制作仍在执行，请先暂停')
    shot_ids=[entry.shot_id for entry in payload.shots]
    if len(set(shot_ids))!=len(shot_ids):
        raise AppError(422,'duplicate_product_shot','同一镜头不能重复绑定')
    selected=set(payload.image_ids)
    if len(selected)!=len(payload.image_ids):
        raise AppError(422,'duplicate_product_image','产品图片不能重复选择')
    for entry in payload.shots:
        shot=session.get(m.StoryboardShot,entry.shot_id)
        if not shot or shot.project_id!=project_id:
            raise AppError(422,'product_shot_scope','镜头不属于此项目')
        if shot.status!='draft':
            raise AppError(409,'product_shot_not_draft','只能向尚未审核的草稿镜头新增产品绑定')
        if len(set(entry.image_ids))!=len(entry.image_ids) or set(entry.image_ids)-selected:
            raise AppError(422,'product_reference_scope','镜头图片不属于本次产品快照或存在重复')
    from app.schemas_product_library import ProductSelection
    frozen=freeze_products(session,settings,[ProductSelection(product_id=payload.product_id,image_ids=payload.image_ids)])[0]
    project_binding=session.scalar(select(ProjectProductBinding).where(ProjectProductBinding.project_id==project_id,ProjectProductBinding.product_id==payload.product_id))
    if project_binding:
        old={x['id']:x['sha256'] for x in project_binding.snapshot_json.get('images',[])}
        new={x['id']:x['sha256'] for x in frozen['images']}
        if old!=new:
            raise AppError(409,'product_snapshot_conflict','已有冻结产品快照不同，不能静默覆盖')
    else:
        project_binding=ProjectProductBinding(project_id=project_id,product_id=payload.product_id,snapshot_json=copy.deepcopy(frozen))
        session.add(project_binding)
    for entry in payload.shots:
        existing=session.scalar(select(ShotProductBinding).where(ShotProductBinding.shot_id==entry.shot_id,ShotProductBinding.product_id==payload.product_id))
        if existing:
            if existing.project_id!=project_id or existing.image_ids_json!=entry.image_ids or existing.state_json.get('visible_state','')!=entry.visible_state:
                raise AppError(409,'product_binding_conflict','镜头已有不同绑定，请明确编辑已有引用')
        else:
            session.add(ShotProductBinding(project_id=project_id,shot_id=entry.shot_id,product_id=payload.product_id,
                image_ids_json=entry.image_ids,state_json={'visible_state':entry.visible_state,'reference_selection_confirmed':False,'source':'existing_project_binding'}))
    session.add(m.AuditEvent(object_type='project',object_id=project_id,action='product_bound_to_draft_shots',
        details_json={'product_id':payload.product_id,'shot_ids':shot_ids,'image_ids':payload.image_ids,'human_visual_approval':False}))
    session.commit()
    return {'project_id':project_id,'product_id':payload.product_id,'shots':[reference_selection(session,project_id,x) for x in shot_ids],
        'human_visual_approval':False,'note':'产品原图已冻结；旧关键画面需重新核对，尚未批准镜头'}


def project_references(session, project_id, product_id):
    """Editable future-production choices, separate from historical outputs."""
    from app.api.routes import project_shot_statement
    from app.services.cinematic_quality import production_context
    if not session.get(m.Project, project_id):
        raise AppError(404,'project_missing','项目不存在')
    product=get_product(session,product_id)
    binding=session.scalar(select(ProjectProductBinding).where(
        ProjectProductBinding.project_id==project_id,ProjectProductBinding.product_id==product_id))
    rows=list(session.scalars(select(ShotProductBinding).where(
        ShotProductBinding.project_id==project_id,ShotProductBinding.product_id==product_id).order_by(ShotProductBinding.shot_id)))
    editable_shots=list(session.scalars(project_shot_statement(session,project_id)))
    editable_ids={shot.id for shot in editable_shots}
    # Include library revision as well as frozen choices to catch another tab's
    # uploads/edits. Locking the project in save serializes writers across products.
    token=sha(json.dumps({'editable_shots':sorted(editable_ids),'product_revision':product.revision,'snapshot':binding.snapshot_json if binding else None,
        'shots':[(row.shot_id,row.revision,row.image_ids_json,row.state_json) for row in rows]},
        ensure_ascii=False,sort_keys=True).encode('utf-8'))
    current_rows=[row for row in rows if row.shot_id in editable_ids]
    selected=list(dict.fromkeys(image_id for row in current_rows for image_id in row.image_ids_json))
    result=[]
    name=product.name.strip().casefold()
    for shot in editable_shots:
        entities=production_context(session,shot)[4]
        text=' '.join([shot.scene_title or '',shot.action or '',shot.visual_prompt or '',
            *(entity.name for entity in entities if entity and entity.kind=='prop')]).casefold()
        result.append({'id':shot.id,'order':shot.order,'title':shot.scene_title,
            'suggested':len(name)>=2 and name in text})
    current=dto(session,product)
    # Keep frozen photos selectable even if the library was later edited.
    available={image['id']:image for image in current['images']}
    for image in (binding.snapshot_json.get('images',[]) if binding else []):
        available[image['id']]=image
    current['images']=list(available.values())
    return {'project_id':project_id,'product':current,'expected_token':token,
        'image_ids':selected,'shot_ids':[row.shot_id for row in current_rows],
        'shots':result,'bound':binding is not None,
        'note':'仅所选分镜使用产品原图；更改后旧视频保留，后续生成会重新检查输入。'}


def save_project_references(session, settings, project_id, payload):
    session.execute(update(m.Project).where(m.Project.id==project_id).values(name=m.Project.name).execution_options(synchronize_session=False))
    current=project_references(session,project_id,payload.product_id)
    if current['expected_token']!=payload.expected_token:
        raise AppError(409,'project_product_revision','产品图片或项目引用已变化，请重新读取后保存')
    active={'queued','submitting','submitted','running','cancelling','reconciling','unknown'}
    for model in (m.GenerationJob,m.ProductionVisual,m.MediaProcessTask):
        if session.scalar(select(model.id).where(model.project_id==project_id,model.status.in_(active)).limit(1)):
            raise AppError(409,'product_binding_busy','项目仍有制作任务，请结束后再修改产品参考')
    from app.services.studio_pipeline import latest_run,RUN_TERMINAL
    run=latest_run(session,project_id)
    if run and run.status not in RUN_TERMINAL|{'paused','exceptions'}:
        raise AppError(409,'product_binding_busy','自动制作仍在执行，请先暂停')
    chosen=set(payload.image_ids);shots=set(payload.shot_ids)
    if len(chosen)!=len(payload.image_ids) or len(shots)!=len(payload.shot_ids):
        raise AppError(422,'product_reference_scope','产品图片或分镜不能重复')
    editable_ids={shot['id'] for shot in current['shots']}
    if shots-editable_ids:
        raise AppError(422,'product_shot_scope','所选分镜不属于本项目当前剧本')
    existing=list(session.scalars(select(ShotProductBinding).where(ShotProductBinding.project_id==project_id,
        ShotProductBinding.product_id==payload.product_id,ShotProductBinding.shot_id.in_(editable_ids))))
    if not shots:
        # Removing current production references must remain possible when an
        # archived or damaged source can no longer be used for new generation.
        # Historical-script bindings and all original files stay untouched.
        for row in existing:session.delete(row)
        if existing and run and run.status in {'paused','exceptions'}:
            run.snapshot_json={**run.snapshot_json,'product_references_changed':True}
            run.status='paused'
        session.add(m.AuditEvent(object_type='project',object_id=project_id,action='project_product_references_removed',
            details_json={'product_id':payload.product_id,'shot_ids':[row.shot_id for row in existing]}))
        session.commit()
        return project_references(session,project_id,payload.product_id)
    if not chosen:
        raise AppError(422,'product_images_missing','使用产品的分镜至少需要一张真实产品参考图')
    project=session.scalar(select(ProjectProductBinding).where(ProjectProductBinding.project_id==project_id,
        ProjectProductBinding.product_id==payload.product_id))
    old_images={image['id']:image for image in project.snapshot_json.get('images',[])} if project else {}
    library_ids={image.id for image in images(session,payload.product_id)}
    if chosen-library_ids-set(old_images):
        raise AppError(422,'product_reference_scope','所选图片不属于当前产品')
    # Existing immutable photo entries are retained; only newly chosen entries
    # are frozen. No old file, video, or historical input snapshot is overwritten.
    new_ids=chosen-set(old_images)
    if new_ids:
        from app.schemas_product_library import ProductSelection
        frozen=freeze_products(session,settings,[ProductSelection(product_id=payload.product_id,image_ids=sorted(new_ids))])[0]
    else:
        if current['product']['state']!='active':raise AppError(409,'product_archived','已移出图库的产品不能用于新配置')
        frozen={k:v for k,v in current['product'].items() if k!='images'}|{'images':[]}
    for image_id in chosen & set(old_images):
        image=old_images[image_id]
        path=safe_media_path(settings.media_root,image['relative_path'],must_exist=True)
        if sha(path.read_bytes())!=image['sha256']:
            raise AppError(409,'product_source_changed','已冻结产品原图发生变化')
    merged={**old_images,**{image['id']:image for image in frozen['images']}}
    frozen['images']=list(merged.values())
    for shot_id in shots:
        others=list(session.scalars(select(ShotProductBinding).where(ShotProductBinding.shot_id==shot_id,
            ShotProductBinding.product_id!=payload.product_id)))
        total=len(chosen)+sum(len(row.state_json.get('keyframe_image_ids',row.image_ids_json)) for row in others)
        if total>4:raise AppError(422,'product_keyframe_budget','此镜产品关键画面参考超过4张，请减少图片或调整分镜产品选择')
    if not project:
        project=ProjectProductBinding(project_id=project_id,product_id=payload.product_id,snapshot_json=frozen);session.add(project)
    else:project.snapshot_json=frozen
    by_shot={row.shot_id:row for row in existing}
    for row in existing:
        if row.shot_id not in shots:session.delete(row)
    for shot_id in shots:
        row=by_shot.get(shot_id)
        if not row:
            row=ShotProductBinding(project_id=project_id,shot_id=shot_id,product_id=payload.product_id,revision=0);session.add(row)
        row.image_ids_json=list(payload.image_ids)
        row.state_json={**(row.state_json or {}),'keyframe_image_ids':list(payload.image_ids),
            'source':'project_product_preparation','reference_selection_confirmed':True}
        row.revision+=1
    if run and run.status in {'paused','exceptions'}:
        run.snapshot_json={**run.snapshot_json,'product_references_changed':True}
        run.status='paused'
    session.add(m.AuditEvent(object_type='project',object_id=project_id,action='project_product_references_saved',
        details_json={'product_id':payload.product_id,'image_ids':payload.image_ids,'shot_ids':payload.shot_ids,
            'human_visual_approval':False}))
    session.commit()
    return project_references(session,project_id,payload.product_id)


def apply_studio_product_plan(session, settings, project_id, shots, plan):
    """Apply an explicit product/photo choice using traceable script prop links.

    A branded prop anchors the product; only props explicitly co-occurring with
    that anchor in a shot are linked. The inference and each affected shot are
    recorded. Existing video versions are never modified or invalidated.
    """
    import re
    from app.schemas_product_library import ProductSelection
    from app.services.cinematic_quality import production_context
    values=plan.model_dump() if hasattr(plan,'model_dump') else dict(plan)
    identifiers=values['image_ids']
    if len(set(identifiers))!=len(identifiers):
        raise AppError(422,'duplicate_product_image','产品参考图不能重复')
    reason=values.get('selection_reason','').strip()
    if not reason:
        raise AppError(422,'product_selection_reason_required','请记录用户对产品与图片的选择')
    for model in (m.GenerationJob,m.ProductionVisual,m.MediaProcessTask):
        if session.scalar(select(model.id).where(model.project_id==project_id,model.status.in_({'queued','submitting','submitted','running','cancelling'})).limit(1)):
            raise AppError(409,'product_binding_busy','项目仍有制作任务，不能更改产品参考')
    product=get_product(session,values['product_id'])
    frozen=freeze_products(session,settings,[ProductSelection(product_id=product.id,image_ids=identifiers)])[0]
    existing=session.scalar(select(ProjectProductBinding).where(ProjectProductBinding.project_id==project_id,ProjectProductBinding.product_id==product.id))
    if existing:
        known={item['id']:item['sha256'] for item in existing.snapshot_json.get('images',[])}
        if any(known.get(item['id'])!=item['sha256'] for item in frozen['images']):
            raise AppError(409,'product_snapshot_conflict','选择的原图不在现有冻结快照或文件已变化')
    else:
        existing=ProjectProductBinding(project_id=project_id,product_id=product.id,snapshot_json=copy.deepcopy(frozen))
        session.add(existing)
    # Use the entire nontrivial Latin brand phrase, not filenames or colors.
    # If no such phrase exists, require explicit work rather than guessing.
    brand=[x.casefold() for x in re.findall(r'[A-Za-z0-9]+', product.brand or product.name) if len(x)>=3]
    if len(brand)<2:
        raise AppError(422,'product_script_anchor_missing','产品名称缺少足够的明确品牌词，无法从剧本安全匹配')
    from app.api.routes import project_shot_statement
    # Establish product lineage from the current complete script, even when
    # this run only selects a later tray/toy close-up without the box.
    script_shots=list(session.scalars(project_shot_statement(session,project_id)))
    states={}
    entities={}
    for shot in script_shots:
        production=production_context(session,shot)[3]
        ids=[]
        for prop in production.get('prop_states',[]):
            entity_id=prop.get('entity_id')
            entity=session.get(m.ProductionEntity,entity_id) if entity_id else None
            if entity and entity.project_id==project_id and entity.kind=='prop':
                entities[entity.id]=entity
                if entity.id not in ids:ids.append(entity.id)
        states[shot.id]=ids
    def matches(entity):
        spec=entity.spec_json or {}
        text=' '.join([entity.name or '',*(str(x) for x in spec.get('visible_text',[]) if isinstance(x,str))]).casefold()
        tokens=set(re.findall(r'[a-z0-9]+',text))
        return all(term in tokens for term in brand)
    anchors={entity.id for entity in entities.values() if matches(entity)}
    if not anchors:
        raise AppError(422,'product_script_anchor_missing','剧本道具没有明确匹配的产品品牌，未自动猜测绑定')
    linked=set(anchors)
    for ids in states.values():
        if anchors.intersection(ids):linked.update(ids)
    affected=[];skipped=[];changes=[]
    for shot in shots:
        matched=[entity_id for entity_id in states[shot.id] if entity_id in linked]
        if not matched:continue
        if session.scalar(select(m.VideoRevision.id).where(m.VideoRevision.shot_id==shot.id).limit(1)):
            skipped.append({'shot_id':shot.id,'reason':'existing_video_preserved'})
            continue
        item=session.scalar(select(ShotProductBinding).where(ShotProductBinding.shot_id==shot.id,ShotProductBinding.product_id==product.id))
        previous=list(item.image_ids_json) if item else None
        evidence={'method':'brand_anchor_and_same_shot_props','anchor_entity_ids':sorted(anchors),
                  'matched_entity_ids':matched,'user_selection_reason':reason}
        if item:
            if item.project_id!=project_id:raise AppError(409,'product_binding_conflict','镜头产品绑定属于其他项目')
            item.image_ids_json=list(identifiers)
            item.state_json={**item.state_json,'keyframe_image_ids':list(identifiers),'automation_product_evidence':evidence}
            item.revision+=1
        else:
            item=ShotProductBinding(project_id=project_id,shot_id=shot.id,product_id=product.id,image_ids_json=list(identifiers),
                    state_json={'keyframe_image_ids':list(identifiers),'reference_selection_confirmed':False,'automation_product_evidence':evidence})
            session.add(item)
        affected.append({'shot_id':shot.id,'matched_entity_ids':matched})
        changes.append({'shot_id':shot.id,'previous_image_ids':previous,'image_ids':list(identifiers)})
    if not affected and not skipped:
        raise AppError(422,'product_script_shot_missing','没有镜头明确引用匹配的产品道具')
    result={'product_id':product.id,'image_ids':list(identifiers),'image_sha256':{x['id']:x['sha256'] for x in frozen['images']},
            'selection_reason':reason,'brand_tokens':brand,'anchor_entity_ids':sorted(anchors),'linked_entity_ids':sorted(linked),
            'affected_shots':affected,'skipped_shots':skipped,'changes':changes,
            'human_visual_approval':False,'keyframes':'input hashes change; existing keyframes require rebuild'}
    session.add(m.AuditEvent(object_type='project',object_id=project_id,action='studio_product_plan_applied',details_json=copy.deepcopy(result)))
    session.flush()
    return result

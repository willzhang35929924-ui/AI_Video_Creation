"""Canonical model-slot HTTP routes over existing production services."""
import hashlib
from pathlib import Path
from fastapi import APIRouter,Depends,Request,UploadFile,File,Form
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import models as m
from app.core.db import get_db
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.schemas_routing import ModelServicesOverview,RouteProfile,RouteSave,RouteScope,TaskSlot,TaskSubmit,UnifiedTask,AudioAssetDTO,ServiceGenerationTest,RouteSettings,TaskCandidate
from app.services import task_routing as routing
from app.services.task_projection import tasks,get_task,dto

router=APIRouter(prefix='/api/model-services',tags=['model-services'])

@router.get('',response_model=ModelServicesOverview)
def overview(request:Request,session:Session=Depends(get_db)):
    return routing.overview(session,request.app.state.settings)

@router.get('/routes/{scope}/{target}/{slot}',response_model=RouteProfile)
def get_route(scope:RouteScope,target:str,slot:TaskSlot,session:Session=Depends(get_db)):
    return routing.route_profile(session,scope,target,slot)

@router.put('/routes/{scope}/{target}/{slot}',response_model=RouteProfile)
def save_route(scope:RouteScope,target:str,slot:TaskSlot,payload:RouteSave,session:Session=Depends(get_db)):
    result=routing.save_route(session,scope,target,slot,payload);session.commit();return result

@router.delete('/routes/{scope}/{target}/{slot}',response_model=RouteProfile)
def delete_route(scope:RouteScope,target:str,slot:TaskSlot,session:Session=Depends(get_db)):
    routing.validate_scope(session,scope,target,slot);row=routing.policy(session,scope,target,slot)
    if row:session.delete(row);session.flush()
    session.commit();return routing.route_profile(session,scope,target,slot)

@router.get('/tasks',response_model=list[UnifiedTask])
def task_list(project_id:str|None=None,shot_id:str|None=None,session:Session=Depends(get_db)):
    return tasks(session,project_id,shot_id)

@router.get('/tasks/{identifier}',response_model=UnifiedTask)
def task_detail(identifier:str,session:Session=Depends(get_db)):
    return get_task(session,identifier)


@router.post('/services/{service_id}/generation-test',response_model=UnifiedTask,status_code=202)
def generation_test(service_id:str,payload:ServiceGenerationTest,request:Request,session:Session=Depends(get_db)):
    """Explicit one-item bootstrap. No production-route writes or paid fallback."""
    service=session.get(m.AIService,service_id)
    if not service or not service.enabled:raise AppError(404,'service_missing','服务不存在或已停用')
    if service.provider!='openai_tts' or service.module!='dialogue_generation' or payload.slot!='dialogue_generation':
        raise AppError(422,'verification_adapter_scope','此单次验证入口目前仅支持已实现的OpenAI语音适配器')
    if not payload.allow_cloud_costs:raise AppError(409,'cloud_cost_confirmation_required','本次单条云端测试需要明确的费用授权')
    if not service.encrypted_secret:raise AppError(409,'cloud_credentials_missing','请先在服务档案安全配置密钥')
    if set(payload.inputs)!={'text'}:raise AppError(422,'verification_one_text_only','此能力测试只接受一条显式文本，不导入剧本或发布角色声线')
    model=payload.model_id or service.default_model
    configured={service.default_model,*[entry.get('id','') if isinstance(entry,dict) else str(entry) for entry in service.models_json]}
    if not model or model not in configured:raise AppError(422,'verification_model_unconfigured','请先在此服务档案登记要验证的模型ID')
    from app.services.ai_services import service_snapshot
    from app.services.media_processing import create_task
    snap=service_snapshot(service,model)
    candidate=TaskCandidate(service_id=service.id,model_id=model,name=service.name,provider=service.provider,
        module=service.module,is_cloud=True,enabled=True,eligible=False,verification='not_tested',
        reasons=['Explicit one-item capability test; not yet a production candidate'])
    choice={'original_index':0,'candidate':candidate.model_dump(),'service_snapshot':snap}
    policy=RouteProfile(scope='project',target_id=payload.project_id,slot=payload.slot,revision=0,
        inherited_from='explicit_single_service_verification',settings=RouteSettings(mode='manual',choices=[{'service_id':service.id,'model_id':model}]))
    route={'slot':payload.slot,'profile':policy.model_dump(),'choices':[choice],'selected':choice,
        'configured':False,'verification_run':True,'allow_cloud_costs':True,'allow_paid_fallback':False,'rejections':[]}
    task=create_task(session,request.app.state.settings,TaskSubmit(slot=payload.slot,project_id=payload.project_id,
        shot_id=payload.shot_id,inputs=payload.inputs,parameters=payload.parameters,allow_cloud_costs=True),route=route)
    session.commit();return dto(session,'media',task)

@router.post('/tasks',response_model=UnifiedTask,status_code=202)
async def submit(payload:TaskSubmit,request:Request,session:Session=Depends(get_db)):
    settings=request.app.state.settings
    if not session.get(m.Project,payload.project_id):raise AppError(404,'project_missing','项目不存在')
    shot=session.get(m.StoryboardShot,payload.shot_id) if payload.shot_id else None
    if payload.shot_id and (not shot or shot.project_id!=payload.project_id or shot.status=='deleted'):raise AppError(422,'shot_scope','请选择当前项目有效镜头')
    if payload.slot in {'dialogue_generation','lip_sync'}:
        from app.services.media_processing import create_task
        task=create_task(session,settings,payload);session.commit();return dto(session,'media',task)
    route=routing.freeze_route(session,settings,payload.project_id,payload.shot_id,payload.slot,payload.allow_cloud_costs,payload.allow_paid_fallback)
    service,snapshot=routing.assert_frozen(session,route);parameters={**route['profile']['settings']['parameters'],**payload.parameters};routing.validate_parameters(parameters)
    if payload.slot=='video_generation':
        if not shot:raise AppError(422,'shot_required','视频生成需要镜头')
        from app.api.routes import generate_for_shot
        from app.schemas import GenerationRequest
        job,_=await generate_for_shot(request,session,shot,GenerationRequest(service_id=service.id,model_id=snapshot['model_id'],seed=parameters.pop('seed',None),parameters={**parameters,'task_route_snapshot':route}))
        session.commit();return dto(session,'video',job)
    if payload.slot in {'character_image','keyframe_image'}:
        supported={'width','height','seed'} if payload.slot=='keyframe_image' or payload.inputs.get('scene_id') else {'width','height','seed','quality'}
        if set(parameters)-supported:raise AppError(422,'image_route_parameters_unsupported','此图片入口不支持所填参数',{'fields':sorted(set(parameters)-supported)})
        if payload.slot=='keyframe_image' or payload.inputs.get('scene_id'):
            from app.services.cinematic_visuals import build_visual
            from app.schemas_cinematic import VisualInput
            visual=build_visual(session,settings,payload.project_id,VisualInput(kind='keyframe' if payload.slot=='keyframe_image' else 'scene_reference',shot_id=payload.shot_id,scene_id=payload.inputs.get('scene_id'),service_id=service.id,model_id=snapshot['model_id'],
                width=int(parameters.get('width',1024)),height=int(parameters.get('height',576)),seed=int(parameters.get('seed',20261023)),instructions=str(payload.inputs.get('instructions','')),confirm_cloud_test=payload.allow_cloud_costs))
            visual.snapshot_json={**visual.snapshot_json,'task_route_snapshot':route};session.commit();return dto(session,'visual',visual)
        from app.services.character_factory import build_batch_plan
        from app.schemas_product import BatchRequest
        character=session.get(m.Character,payload.inputs.get('character_id'));version=session.get(m.CharacterVersion,payload.inputs.get('version_id'))
        if not character or not version or version.character_id!=character.id or version.approval_state=='approved':raise AppError(422,'character_draft_required','请选择当前角色的未锁定草稿版本')
        plan=build_batch_plan(session,settings,character.id,BatchRequest(version_id=version.id,project_id=payload.project_id,kind='candidates',candidate_count=1,max_tasks=1,max_retries=0,service_id=service.id,model_id=snapshot['model_id'],width=int(parameters.get('width',832)),height=int(parameters.get('height',1024)),seed=int(parameters.get('seed',202610210)),quality=parameters.get('quality','standard')))
        batch=m.CharacterBatch(character_id=character.id,character_version_id=version.id,kind='candidates',snapshot_json={**plan,'task_route_snapshot':route});session.add(batch);session.flush()
        first=plan['jobs'][0];job=m.CharacterGenerationJob(batch_id=batch.id,asset_type=first['asset_type'],prompt=first['prompt'],negative_prompt=first['negative_prompt'],seed=first['seed'],snapshot_json={**{k:v for k,v in plan.items() if k not in {'jobs','preview_hash'}},'type':first['type'],'attempt':0,'task_route_snapshot':route,'project_id':payload.project_id})
        session.add(job);session.commit();return dto(session,'image',job)
    if payload.slot=='video_upscale':
        from app.services.video_quality import create_render
        from app.schemas_quality import QualitySettings
        video=session.get(m.VideoRevision,payload.inputs.get('video_id'))
        if not video or video.project_id!=payload.project_id or payload.shot_id and video.shot_id!=payload.shot_id:raise AppError(422,'video_scope','源视频不属于项目镜头')
        config={k:v for k,v in parameters.items() if k in QualitySettings.model_fields};config.setdefault('mode',route['profile']['settings']['quality_mode'])
        task=create_render(session,settings,video.id,QualitySettings(**config),route_snapshot=route);session.commit();return dto(session,'quality',task)
    if payload.slot=='final_assembly':
        if parameters:raise AppError(422,'assembly_route_parameters_unsupported','合成输出参数由已保存的时间线管理，此入口不静默覆盖')
        from app.api.assemblies import submit as render
        from app.schemas_product import AssemblySubmit
        assembly=session.get(m.Assembly,payload.inputs.get('assembly_id'))
        if not assembly or assembly.project_id!=payload.project_id:raise AppError(422,'assembly_scope','时间线不属于项目')
        result=render(assembly.id,AssemblySubmit(revision=assembly.revision),request,session,'render',route_snapshot=route);return dto(session,'assembly',session.get(m.AssemblyRender,result['id']))
    raise AppError(409,'slot_explicit_entry_required','此槽通过原有剧本导入/技术检查入口执行，或尚无已验证适配器；没有建立平行任务')

def audio_view(asset):
    return {'id':asset.id,'project_id':asset.project_id,'shot_id':asset.shot_id,'participant_id':asset.participant_id,'character_version_id':asset.character_version_id,
        'relative_path':asset.relative_path,'sha256':asset.sha256,'metadata':asset.metadata_json,'media_url':'/api/media/'+asset.relative_path}

@router.get('/audio-assets',response_model=list[AudioAssetDTO])
def audio_list(project_id:str,character_version_id:str|None=None,session:Session=Depends(get_db)):
    query=select(m.AudioAsset).where(m.AudioAsset.project_id==project_id)
    if character_version_id:query=query.where(m.AudioAsset.character_version_id==character_version_id)
    return [audio_view(a) for a in session.scalars(query.order_by(m.AudioAsset.created_at.desc()))]

@router.post('/audio-assets',response_model=AudioAssetDTO,status_code=201)
async def upload_voice(request:Request,project_id:str=Form(...),character_version_id:str=Form(...),source_type:str=Form(...),declaration:str=Form(...),file:UploadFile=File(...),session:Session=Depends(get_db)):
    if source_type not in {'ai_generated','user_authorized','open_license'} or not 1<=len(declaration.strip())<=4000:raise AppError(422,'audio_source_declaration','请选择来源类型并提供来源/授权声明')
    version=session.get(m.CharacterVersion,character_version_id)
    linked=session.scalar(select(m.ShotCharacter.id).join(m.StoryboardShot,m.StoryboardShot.id==m.ShotCharacter.shot_id).where(m.StoryboardShot.project_id==project_id,m.ShotCharacter.character_version_id==character_version_id).limit(1))
    if not version or not linked:raise AppError(422,'voice_version_scope','角色版本未绑定到当前项目')
    suffix=Path(file.filename or '').suffix.lower()
    if suffix not in {'.wav','.flac','.mp3','.m4a','.aac','.ogg'}:raise AppError(415,'voice_audio_type','请选择可解码的WAV/FLAC/MP3/M4A/AAC/OGG音频')
    data=await file.read(100*1024*1024+1)
    if not data or len(data)>100*1024*1024:raise AppError(413,'voice_audio_size','音频为空或超过100MiB')
    asset=m.AudioAsset(project_id=project_id,character_version_id=version.id,relative_path='',sha256=hashlib.sha256(data).hexdigest(),metadata_json={})
    session.add(asset);session.flush();relative=f'audio_assets/{project_id}/{asset.id}'+suffix
    path=safe_media_path(request.app.state.settings.media_root,relative);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as stream:stream.write(data)
    try:
        from app.services.media_processing import _probe
        import asyncio
        actual=await asyncio.to_thread(_probe,path,request.app.state.settings,True)
        if not .1<=actual['duration']<=30:raise AppError(422,'voice_reference_duration','角色参考音频需为0.1至30秒；不会静默截断')
        asset.relative_path=relative;asset.metadata_json={'source_type':source_type,'declaration':declaration,'actual':actual,
            'human_approved':False,'voice_identity_verification':'NOT RUN','runtime_reference_policy':'Original immutable; local reference TTS makes a separate 24kHz mono PCM derivative'}
        session.add(m.ManagedFile(relative_path=relative,state='active',ownership_json=[{'table':'audio_assets','id':asset.id}],metadata_json={'verified_owned':True,'sha256':asset.sha256,'kind':'voice_reference'}))
        session.commit();return audio_view(asset)
    except Exception:
        session.rollback()
        if path.exists() and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==hashlib.sha256(data).hexdigest():path.unlink()
        raise

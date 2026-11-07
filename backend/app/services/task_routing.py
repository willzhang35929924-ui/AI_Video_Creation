"""Ten independent selections over the existing service registry, not a second runner."""
import json
import re
from pathlib import Path
from typing import get_args
from sqlalchemy import select
from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.schemas_routing import TaskSlot, RouteSettings, RouteProfile, TaskCandidate
from app.services.ai_services import service_snapshot
from app.services.runtime_registry import file_sha, manifest_status

SLOTS={
 'script_planning':('剧本解析与镜头规划',['language_processing']),
 'screenplay_from_text':('文生剧本',['language_processing']),
 'screenplay_from_image':('图片生剧本',['language_processing']),
 'screenplay_from_video':('视频生剧本',['language_processing']),
 'character_image':('人物、服装与场景图片',['character_generation']),
 'keyframe_image':('分镜关键画面',['character_generation']),
 'video_generation':('主视频生成',['video_generation']),
 'motion_driving':('动作与表演驱动',['video_generation']),
 'dialogue_generation':('对白生成',['dialogue_generation']),
 'lip_sync':('口型修正',['lip_sync']),
 'video_upscale':('视频超分',['video_upscale']),
 'quality_check':('技术质量检查',['quality_check','final_assembly']),
 'final_assembly':('最终合成',['final_assembly'])}
BLOCKED_KEYS={'api_key','token','secret','authorization','headers','password','python','helper','repo','model_dir','workflow_path','output_path','receipt_path','video_path','audio_path','reference_audio_path','service_snapshot','route_snapshot'}

def validate_parameters(parameters):
    if not isinstance(parameters,dict) or len(json.dumps(parameters,ensure_ascii=False))>20000:raise AppError(422,'route_parameters','运行参数无效或过长')
    for key,value in parameters.items():
        if key.lower() in BLOCKED_KEYS or isinstance(value,(dict,list)):
            raise AppError(422,'unsafe_route_parameter','参数不接受密钥、路径、嵌套对象或执行命令')

def validate_scope(session,scope,target,slot):
    if slot not in SLOTS or scope not in {'system','project','shot'}:raise AppError(422,'route_scope','任务槽或作用域无效')
    if scope=='system':
        if target!='default':raise AppError(422,'route_scope','系统默认ID必须是default')
    elif not session.get(m.Project if scope=='project' else m.StoryboardShot,target):raise AppError(404,'route_target_missing','路由配置对象不存在')

def policy(session,scope,target,slot):
    return session.scalar(select(m.RoutePolicy).where(m.RoutePolicy.scope==scope,m.RoutePolicy.target_id==target,m.RoutePolicy.slot==slot))

def route_profile(session,scope,target,slot):
    validate_scope(session,scope,target,slot)
    scopes=[(scope,target)]
    project=target if scope=='project' else session.get(m.StoryboardShot,target).project_id if scope=='shot' else None
    if scope=='shot':scopes.append(('project',project))
    if scope!='system':scopes.append(('system','default'))
    for level,identifier in scopes:
        row=policy(session,level,identifier,slot)
        if row:return RouteProfile(scope=scope,target_id=target,slot=slot,revision=row.revision if level==scope else 0,
            inherited_from=None if level==scope else level+':'+identifier,settings=row.settings_json)
    choices=[]
    for module in SLOTS[slot][1]:
        binding=session.scalar(select(m.ProjectServiceBinding).where(m.ProjectServiceBinding.project_id==project,m.ProjectServiceBinding.module==module)) if project else None
        service=session.get(m.AIService,binding.service_id) if binding else session.scalar(select(m.AIService).where(m.AIService.module==module,m.AIService.is_default.is_(True),m.AIService.enabled.is_(True)))
        if service and service.enabled:choices=[{'service_id':service.id,'model_id':(binding.model_id if binding else '') or service.default_model}];break
    return RouteProfile(scope=scope,target_id=target,slot=slot,revision=0,inherited_from='legacy_module_default' if choices else None,
        settings=RouteSettings(mode='manual' if choices else 'local_only',choices=choices))

def save_route(session,scope,target,slot,payload):
    validate_scope(session,scope,target,slot);validate_parameters(payload.settings.parameters)
    row=policy(session,scope,target,slot)
    if payload.expected_revision!=(row.revision if row else 0):raise AppError(409,'route_revision_changed','路由已修改，请刷新后保存')
    resolved=set()
    for choice in payload.settings.choices:
        service=session.get(m.AIService,choice.service_id)
        if not service or service.module not in SLOTS[slot][1]:raise AppError(422,'route_module_mismatch','服务不属于此任务槽允许的业务模块')
        key=(service.id,choice.model_id or service.default_model)
        if key in resolved:raise AppError(422,'route_duplicate_resolved_model','不能重复选择默认模型及其同名显式模型')
        resolved.add(key)
    if not row:row=m.RoutePolicy(scope=scope,target_id=target,slot=slot,revision=0);session.add(row)
    row.settings_json=payload.settings.model_dump();row.revision+=1;session.flush()
    session.add(m.AuditEvent(object_type='route_policy',object_id=row.id,action='explicit_route_saved',details_json={'slot':slot,'revision':row.revision,'allow_paid_fallback':payload.settings.allow_paid_fallback}))
    return route_profile(session,scope,target,slot)

def _matches(snapshot,service,model):
    return snapshot.get('service_id')==service.id and snapshot.get('service_revision')==service.revision and snapshot.get('model_id')==model

def _file(settings,path,sha):
    try:return bool(sha) and file_sha(safe_media_path(settings.media_root,path,must_exist=True))==sha
    except (OSError,AppError):return False

def _context_video_evidence(session, settings, service, model, job, snapshot):
    from app.services.h3_context_provider import context_origin
    from app.services.h3_workflow_router import freeze_workflow

    params = job.parameters_json
    workflow = params.get('h3_workflow')
    if (service.provider != 'comfyui' or service.encrypted_secret or job.provider != 'comfyui_h3'
            or not job.provider_job_id or not isinstance(workflow, dict) or snapshot.get('h3_workflow') != workflow
            or snapshot.get('service_id') != service.id or snapshot.get('service_revision') != service.revision
            or snapshot.get('provider') != service.provider or snapshot.get('is_cloud') is not False
            or snapshot.get('requested_model_id') != model
            or not str(model).lower().replace('-', '_').startswith('minimax_h3')):
        return None
    try:
        expected = freeze_workflow(workflow.get('quality_mode'))
        from app.services.video_services import context_service_origins
        origin, legacy = context_service_origins(settings, {'base_url': service.base_url, 'settings': service.settings_json})
        if (snapshot.get('base_url') != origin
                or snapshot.get('legacy_comfyui_base_url') != legacy):
            return None
    except AppError:
        return None
    if (workflow != expected or snapshot.get('workflow_sha256') != expected['workflow_sha256']
            or snapshot.get('model_id') != expected['model_name'] or snapshot.get('workflow_version') != expected['workflow_id']
            or job.workflow_version != expected['workflow_id'] or params.get('steps') != expected['actual_steps']):
        return None
    receipt = params.get('context_ir_execution') or {}
    graph_sha = params.get('actual_workflow_sha256') or ''
    if (not isinstance(receipt, dict) or receipt.get('workflow') != workflow or not receipt.get('compiler_receipt')
            or not re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('compiled_prompt_sha256')))
            or not re.fullmatch(r'[0-9a-f]{64}', str(graph_sha))
            or (workflow['quality_mode'] == 'fast' and not receipt.get('smd_sampling_receipt'))):
        return None
    for video in session.scalars(select(m.VideoRevision).where(m.VideoRevision.generation_job_id == job.id)):
        metadata = video.metadata_json
        if (video.project_id != job.project_id or video.shot_id != job.shot_id
                or not metadata.get('playable_video') or metadata.get('h3_workflow') != workflow
                or metadata.get('context_ir_execution') != receipt or metadata.get('actual_workflow_sha256') != graph_sha
                or metadata.get('comfyui_prompt_id') != job.provider_job_id):
            continue
        decoded = session.scalar(select(m.QualityCheck.id).where(m.QualityCheck.video_revision_id == video.id,
            m.QualityCheck.check_type == 'media_decode', m.QualityCheck.status == 'passed').limit(1))
        if decoded and _file(settings, video.relative_path, metadata.get('sha256')):
            return {'kind':'decoded_context_video', 'job_id':job.id, 'video_id':video.id, 'sha256':metadata['sha256'],
                    'workflow_id':workflow['workflow_id'], 'quality_mode':workflow['quality_mode'],
                    'actual_steps':workflow['actual_steps'], 'workflow_sha256':workflow['workflow_sha256'],
                    'actual_workflow_sha256':graph_sha, 'compiled_prompt_sha256':receipt['compiled_prompt_sha256']}
    return None


def _evidence(session,settings,service,model,slot):
    if service.provider=='cloud_draft':return None
    if slot in {'screenplay_from_text','screenplay_from_image','screenplay_from_video'}:
        if service.provider not in {'local_llm','openai_compatible'}:return None
        from app.services.screenplay_generation import screenplay_evidence
        return screenplay_evidence(session,settings,service,model,slot)
    if service.provider=='local_runtime' or service.module=='video_upscale' and service.provider=='comfyui':
        family=service.settings_json.get('runtime_family') or ('seedvr2' if model=='seedvr2_3b_fp8_e4m3fn.safetensors' else '')
        if family=='seedvr2' and service.base_url.rstrip('/')!='http://127.0.0.1:8189':return None
        runtime=manifest_status(family,settings)
        if runtime['state']=='verified' and runtime['manifest']['model_id']==model and slot in runtime['manifest']['task_capabilities']:
            return {'kind':'deployment_smoke','runtime':runtime}
        return None
    if slot=='motion_driving':return None  # No implemented motion adapter, even if a video model was tested.
    if slot in {'character_image','keyframe_image'}:
        for visual in session.scalars(select(m.ProductionVisual).where(m.ProductionVisual.status.in_(['approved','needs_review'])).order_by(m.ProductionVisual.created_at.desc())):
            snapshot=visual.snapshot_json
            if _matches(snapshot.get('service_snapshot',{}),service,model) and (slot!='keyframe_image' or visual.kind=='keyframe') and _file(settings,visual.relative_path,visual.sha256):
                return {'kind':'decoded_image','visual_id':visual.id,'sha256':visual.sha256,'reference_count':len(snapshot.get('references',[]))}
        if slot=='character_image':
            for job in session.scalars(select(m.CharacterGenerationJob).where(m.CharacterGenerationJob.status.in_(['needs_review','approved','generated']))):
                asset=session.get(m.CharacterAsset,job.result_asset_id) if job.result_asset_id else None
                if _matches(job.snapshot_json.get('service_snapshot',{}),service,model) and asset and _file(settings,asset.relative_path,asset.sha256):return {'kind':'decoded_image','job_id':job.id,'asset_id':asset.id,'sha256':asset.sha256}
    elif slot=='video_generation':
        current_workflow=legacy_evidence=None
        if service.provider=='comfyui':
            try:current_workflow=file_sha(Path(service.settings_json.get('workflow_path') or settings.h3_workflow_api_json))
            except (OSError,TypeError):pass
        for job in session.scalars(select(m.GenerationJob).where(m.GenerationJob.status.in_(['generated','approved','revised']))):
            snapshot=job.parameters_json.get('service_snapshot') or {}
            if job.parameters_json.get('h3_workflow') or job.parameters_json.get('context_ir_execution') or snapshot.get('h3_workflow'):
                proof = _context_video_evidence(session, settings, service, model, job, snapshot)
                if proof:return proof
                continue
            if legacy_evidence or not _matches(snapshot,service,model):continue
            if service.provider=='comfyui' and (not current_workflow or snapshot.get('workflow_sha256')!=current_workflow):continue
            for video in session.scalars(select(m.VideoRevision).where(m.VideoRevision.generation_job_id==job.id)):
                if video.metadata_json.get('playable_video') and _file(settings,video.relative_path,video.metadata_json.get('sha256')):
                    legacy_evidence={'kind':'decoded_video','job_id':job.id,'video_id':video.id,'workflow_sha256':current_workflow}
                    break
        return legacy_evidence
    elif slot in {'dialogue_generation','lip_sync'}:
        for task in session.scalars(select(m.MediaProcessTask).where(m.MediaProcessTask.slot==slot,m.MediaProcessTask.status=='succeeded')):
            snapshot=task.result_json.get('actual_service_snapshot') or (task.snapshot_json.get('active_route') or task.snapshot_json.get('route') or {}).get('selected',{}).get('service_snapshot',{})
            result=task.result_json
            if _matches(snapshot,service,model) and _file(settings,result.get('relative_path',''),result.get('sha256')):return {'kind':'decoded_media','task_id':task.id,'sha256':result['sha256']}
    elif slot=='script_planning':
        for item in session.scalars(select(m.WordImport).where(m.WordImport.status=='completed')):
            parser=item.preview_json.get('semantic_parser',{})
            if parser.get('status')=='extracted' and parser.get('output_sha256') and _matches(parser.get('service_snapshot',{}),service,model):return {'kind':'source_checked_language_extraction','import_id':item.id,'sha256':parser['output_sha256']}
    elif slot in {'final_assembly','quality_check'} and service.provider=='ffmpeg':
        for render in session.scalars(select(m.AssemblyRender).where(m.AssemblyRender.status=='generated')):
            if _matches(render.snapshot_json.get('service_snapshot',{}),service,model) and _file(settings,render.relative_path,render.metadata_json.get('sha256')):return {'kind':'decoded_assembly','render_id':render.id}
    return None

def candidate(session,settings,service,slot,model_id=None):
    model=model_id or service.default_model;snapshot=service_snapshot(service,model)
    reasons=[]
    if not service.enabled:reasons.append('Service disabled')
    if service.module not in SLOTS[slot][1]:reasons.append('Service module does not support this slot')
    if service.provider=='cloud_draft':reasons.append('No execution adapter for draft cloud profile')
    if slot=='motion_driving':reasons.append('No verified motion-driving workflow adapter')
    proof=_evidence(session,settings,service,model,slot) if not reasons else None
    if not proof:reasons.append('No matching real output evidence for current service/model/workflow')
    return TaskCandidate(service_id=service.id,model_id=model,name=service.name,provider=service.provider,module=service.module,
        is_cloud=snapshot['is_cloud'],enabled=service.enabled,eligible=not reasons,
        verification='verified' if not reasons else 'unavailable' if service.provider=='cloud_draft' or not service.enabled else 'not_tested',reasons=reasons,evidence=proof or {})

def freeze_route(session,settings,project_id,shot_id,slot,allow_cloud_costs=False,allow_paid_fallback=False):
    profile=route_profile(session,'shot' if shot_id else 'project' if project_id else 'system',shot_id or project_id or 'default',slot)
    config=profile.settings;choices=[]
    for index,choice in enumerate(config.choices):
        service=session.get(m.AIService,choice.service_id)
        if not service:continue
        item=candidate(session,settings,service,slot,choice.model_id)
        choices.append({'original_index':index,'candidate':item.model_dump(),'service_snapshot':service_snapshot(service,item.model_id)})
    if config.mode=='local_only':choices=[v for v in choices if not v['candidate']['is_cloud']]
    elif config.mode=='cloud_only':choices=[v for v in choices if v['candidate']['is_cloud']]
    elif config.mode in {'local_first','cloud_first','auto'}:
        preferred=config.mode=='cloud_first'
        choices.sort(key=lambda v:(v['candidate']['is_cloud']!=preferred,v['original_index']))
    selected=None;rejections=[]
    for index,choice in enumerate(choices):
        if index and config.mode!='auto' and not config.allow_fallback:break
        item=choice['candidate']
        if not item['eligible']:rejections.append({'service_id':item['service_id'],'reasons':item['reasons']});continue
        if item['is_cloud'] and not allow_cloud_costs:rejections.append({'service_id':item['service_id'],'reasons':['Primary cloud costs not authorized']});continue
        if index and item['is_cloud'] and not (config.allow_fallback and config.allow_paid_fallback and allow_paid_fallback):
            rejections.append({'service_id':item['service_id'],'reasons':['Paid fallback requires route AND invocation authorization']});continue
        selected=choice;break
    return {'slot':slot,'profile':profile.model_dump(),'configured':profile.revision>0 or profile.inherited_from not in (None,'legacy_module_default'),
        'choices':choices,'selected':selected,'rejections':rejections,'allow_cloud_costs':allow_cloud_costs,'allow_paid_fallback':allow_paid_fallback}

def route_is_explicit(route):
    profile=(route or {}).get('profile',{})
    return bool(route and (route.get('configured') or profile.get('revision',0)
        or profile.get('inherited_from') not in (None,'legacy_module_default')))


def selected_route(snapshot):
    if not snapshot.get('selected'):raise AppError(409,'task_route_blocked','此任务槽没有满足能力、验证与费用范围的服务',{'slot':snapshot.get('slot'),'rejections':snapshot.get('rejections',[])})
    return snapshot['selected']['service_snapshot']

def assert_frozen(session,snapshot):
    selected=selected_route(snapshot);service=session.get(m.AIService,selected['service_id'])
    if not service or not service.enabled or service.revision!=selected['service_revision']:raise AppError(409,'route_service_changed','固定的服务配置已改变；未派发或改用其他服务')
    return service,selected

def overview(session,settings):
    result=[]
    for slot,(name,modules) in SLOTS.items():
        items=[]
        for service in session.scalars(select(m.AIService).where(m.AIService.module.in_(modules)).order_by(m.AIService.created_at)):
            models=list(dict.fromkeys([service.default_model,*[v.get('id','') if isinstance(v,dict) else str(v) for v in service.models_json]]))
            items.extend(candidate(session,settings,service,slot,model).model_dump() for model in models)
        result.append({'slot':slot,'name':name,'allowed_modules':modules,'default_route':route_profile(session,'system','default',slot).model_dump(),'candidates':items})
    return {'slots':result,'limitations':['HTTP connectivity/model listing is not inference verification.','Unknown submissions are never retried or used to trigger fallback.','Likeness, dialogue and cinematic quality still require human review.']}

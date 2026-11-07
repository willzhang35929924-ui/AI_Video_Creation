"""Production workbench APIs; actual execution delegates to existing services."""
import copy
import hashlib
import json
from datetime import datetime,timezone
from fastapi import APIRouter,Depends,Request,File,UploadFile,Form
from fastapi.responses import FileResponse
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from app import models as m
from app.core.config import PROJECT_ROOT
from app.core.db import get_db
from app.core.errors import AppError
from app.schemas import ApprovalRequest,ReviewRequest
from app.schemas_cinematic import DesignInput,ReviewInput,VisualInput,RunInput,ShotProduction
from app.services.screenplay import row,get,touch_shot
from app.services.cinematic_rules import rules_bundle,check_generated_contract,fingerprint
from app.services.cinematic_quality import inspect_ir,shot_preflight,production_context,visual_inputs,generation_inputs_hash,database_ir
from app.services.cinematic_visuals import build_visual,store_visual
from app.services.production_orchestrator import create_run

router=APIRouter(prefix='/api')


@router.get('/cinematic/rules')
def rules():return {**rules_bundle(),'synchronization':check_generated_contract()}


@router.get('/cinematic/schema')
def schema():return FileResponse(PROJECT_ROOT/'schemas/screenplay-ir.schema.json',media_type='application/schema+json')


@router.get('/cinematic/capabilities')
async def capabilities(request:Request):
    from app.services.providers import ComfyUIH3Provider
    health=await ComfyUIH3Provider(request.app.state.settings).health()
    return {'video':{'provider':'comfyui_h3','health':health.to_dict(),'mode':'reference_to_video','reference_images':{'max':9,'roles':['identity','scene_reference','keyframe'],'exact_first_frame':False,'exact_last_frame':False},'actual_controls':['width','height','seed','length','steps'],'width_height':{'multiple_of':32,'adapter_max':1344},'fps':24,'frames':'17n+5','native_audio':True,'exact_lip_sync':False,'voice_clone':False,'camera_parameters':'visual intent only; no physical aperture/shutter control','evidence':['workflows/h3_r2v_character_v1.json','reports/cinematic/initial_audit.json','reports/product_upgrade_real/result.json']},
        'visual':{'candidate':'configured local Klein text-to-image graph','identity_conditioned':'installed Flux2 Klein ReferenceLatent workflow','max_adapter_references':4,'multi_reference_quality':'candidate, requires actual generation and human review','evidence':'workflows/character_klein_reference_v1.json'},'cloud':{'image_contract':'compatible images/generations and images/edits; explicit single validation before orchestration','video_contract':'video-jobs-v1; fps24; dimensions and durations must match the selected service','configured_is_verified':False,'native_mac_test':'NOT RUN','paid_cloud_test':'NOT RUN: user will configure credentials'},'semantic_parser':{'mode':'source_preserving_compatibility_rules','unrestricted_semantic_model_verified':False},'quality':{'technical':'FFprobe/full FFmpeg decode/specification checks','visual':'human review; no fabricated cinema score'}}


@router.get('/projects/{project_id}/production')
def overview(project_id:str,request:Request,session:Session=Depends(get_db)):
    from app.api.routes import project_shot_statement,latest_script
    project=get(session,m.Project,project_id);shots=list(session.scalars(project_shot_statement(session,project_id).order_by(m.StoryboardShot.order)))
    script=production_context(session,shots[0])[2] if shots else latest_script(session,project_id)
    preflight=inspect_ir(database_ir(session,script,shots)) if script else {'findings':[],'counts':{},'estimate':{}}
    visuals=list(session.scalars(select(m.ProductionVisual).where(m.ProductionVisual.project_id==project_id).order_by(m.ProductionVisual.created_at.desc())))
    result={'project':row(project),'script':row(script) if script else None,'rules':script.parsed_json.get('versions',{}) if script else {},'report':preflight,'designs':[row(e) for e in session.scalars(select(m.ProductionEntity).where(m.ProductionEntity.project_id==project_id).order_by(m.ProductionEntity.kind,m.ProductionEntity.logical_id,m.ProductionEntity.version))],
        'shots':[{'shot':row(s),'scene_id':production_context(session,s)[1].id if production_context(session,s)[1] else None,'production':production_context(session,s)[3],'preflight':shot_preflight(session,request.app.state.settings,s),'cast':[{'id':p.character_id,'name':session.get(m.Character,p.character_id).display_name} for p in session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==s.id))]} for s in shots],
        'scenes':[row(scene) for scene in session.scalars(select(m.Scene).where(m.Scene.script_revision_id==script.id).order_by(m.Scene.order))] if script else [],
        'visuals':[{**row(v),'media_url':'/api/media/'+v.relative_path if v.relative_path else None} for v in visuals],
        'runs':[row(r) for r in session.scalars(select(m.ProductionRun).where(m.ProductionRun.project_id==project_id).order_by(m.ProductionRun.created_at.desc()))],
        'videos':[{**row(v),'checks':[row(c) for c in session.scalars(select(m.QualityCheck).where(m.QualityCheck.video_revision_id==v.id))]} for v in session.scalars(select(m.VideoRevision).where(m.VideoRevision.project_id==project_id))],
        'renders':[row(r) for r in session.scalars(select(m.AssemblyRender).join(m.Assembly,m.Assembly.id==m.AssemblyRender.assembly_id).where(m.Assembly.project_id==project_id))]}
    session.commit();return result


@router.post('/projects/{project_id}/production-designs',status_code=201)
def create_design(project_id:str,payload:DesignInput,session:Session=Depends(get_db)):
    get(session,m.Project,project_id)
    if payload.kind=='wardrobe' and not payload.character_id:raise AppError(422,'wardrobe_owner_required','服装版本必须明确所属人物')
    if payload.kind=='scene_state' and not payload.scene_id:raise AppError(422,'scene_state_owner_required','场景状态必须明确所属场景')
    if payload.character_id:get(session,m.Character,payload.character_id)
    if payload.scene_id and get(session,m.Scene,payload.scene_id).project_id!=project_id:raise AppError(422,'design_scene_mismatch','场景不属于本项目')
    version=(session.scalar(select(func.max(m.ProductionEntity.version)).where(m.ProductionEntity.project_id==project_id,m.ProductionEntity.logical_id==payload.logical_id)) or 0)+1
    item=m.ProductionEntity(project_id=project_id,logical_id=payload.logical_id,version=version,kind=payload.kind,name=payload.name,character_id=payload.character_id,scene_id=payload.scene_id,spec_json=payload.spec,source_json={'state':'reviewed_edit','versions':rules_bundle()['versions']})
    session.add(item);session.commit();return row(item)


@router.patch('/production-designs/{design_id}')
def patch_design(design_id:str,payload:DesignInput,session:Session=Depends(get_db)):
    item=get(session,m.ProductionEntity,design_id)
    if item.status=='approved':raise AppError(409,'design_locked','已批准设定不可覆盖，请创建独立新版本')
    if any(getattr(item,k)!=getattr(payload,k) for k in ['kind','logical_id','character_id','scene_id']):raise AppError(422,'design_identity_immutable','设定ID、用途和所属关系不能改写，请新建版本')
    item.name,item.spec_json=payload.name,payload.spec;item.status='draft';session.commit();return row(item)


@router.post('/production-designs/{design_id}/version',status_code=201)
def version_design(design_id:str,session:Session=Depends(get_db)):
    old=get(session,m.ProductionEntity,design_id)
    return create_design(old.project_id,DesignInput(kind=old.kind,logical_id=old.logical_id,name=old.name,character_id=old.character_id,scene_id=old.scene_id,spec=old.spec_json),session)


@router.post('/production-designs/{design_id}/review')
def review_design(design_id:str,payload:ReviewInput,session:Session=Depends(get_db)):
    item=get(session,m.ProductionEntity,design_id)
    if item.status=='approved' and payload.status!='approved':raise AppError(409,'design_locked','已批准设定保持不可变，新建版本后调整')
    item.status=payload.status;item.source_json={**item.source_json,'review':payload.model_dump(),'reviewed_at':datetime.now(timezone.utc).isoformat()}
    session.add(m.AuditEvent(object_type='production_entity',object_id=item.id,action='reviewed',details_json=payload.model_dump()));session.commit();return row(item)


@router.put('/shots/{shot_id}/production')
def patch_production(shot_id:str,payload:ShotProduction,request:Request,session:Session=Depends(get_db)):
    shot=get(session,m.StoryboardShot,shot_id);detail,scene,script,existing,_=production_context(session,shot)
    if not script or not script.parsed_json.get('versions'):raise AppError(409,'cinematic_revision_required','请先用新规范导入新剧本修订，旧镜头规则不自动改写')
    values=payload.model_dump();ids=[values.get('scene_state_id')]+[v.get('entity_id') for v in values['wardrobe_states'].values()]+[v.get('entity_id') for v in values['prop_states']]
    if 'motion_references' not in payload.model_fields_set and existing.get('motion_references'):
        values['motion_references']=existing['motion_references']
    expected=values.pop('expected_input_hash',None)
    if (values.get('motion_references') or existing.get('motion_references')) and not expected:
        raise AppError(409,'production_edit_conflict','修改动作参考前必须提供当前镜头输入版本，请重新载入')
    if expected and expected!=generation_inputs_hash(session,shot):raise AppError(409,'production_edit_conflict','制作输入已被其他操作修改，请重新载入后保存')
    cast={p.character_id for p in session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot_id))}
    for identifier in ids:
        if identifier and get(session,m.ProductionEntity,identifier).project_id!=shot.project_id:raise AppError(422,'production_design_mismatch','制作设定不属于本项目')
    if values.get('scene_state_id'):
        state=get(session,m.ProductionEntity,values['scene_state_id'])
        if state.kind!='scene_state' or state.scene_id!=scene.id:raise AppError(422,'scene_state_mismatch','场景状态版本不属于本镜场景')
    for state in values['prop_states']:
        if state.get('entity_id') and get(session,m.ProductionEntity,state['entity_id']).kind!='prop':raise AppError(422,'prop_kind_mismatch','道具状态必须绑定道具版本')
    for character_id,state in values['wardrobe_states'].items():
        if character_id not in cast:raise AppError(422,'production_cast_mismatch','服装状态的人物不属于当前分镜')
        entity=session.get(m.ProductionEntity,state.get('entity_id'))
        if entity and (entity.kind!='wardrobe' or entity.character_id and entity.character_id!=character_id):raise AppError(422,'wardrobe_owner_mismatch','服装版本不属于该人物')
    for beat in values['beats']:
        identifier=beat.get('character_id',beat.get('character_key'))
        if identifier not in cast or beat['end_seconds']>shot.duration_seconds:raise AppError(422,'beat_invalid','动作节拍主体或时间无效')
    if values.get('motion_references'):
        from app.services.motion_references import resolve_motion_references,verify_motion_sources
        verify_motion_sources(resolve_motion_references(session,request.app.state.settings,shot,values['motion_references']))
    touch_shot(session,shot_id);detail.source_json={**detail.source_json,'production':values,'production_reviewed_edit':True};session.commit();return row(detail)


@router.post('/projects/{project_id}/production-visuals',status_code=202)
def queue_visual(project_id:str,payload:VisualInput,request:Request,session:Session=Depends(get_db)):
    visual=build_visual(session,request.app.state.settings,project_id,payload);session.commit();return row(visual)


@router.post('/projects/{project_id}/production-visuals/upload',status_code=201)
async def upload_visual(project_id:str,request:Request,payload:str=Form(...),file:UploadFile=File(...),session:Session=Depends(get_db)):
    try:values=VisualInput.model_validate_json(payload)
    except ValueError as exc:raise AppError(422,'visual_upload_context_invalid','请提供有效的场景、镜头或造型关联') from exc
    data=await file.read(20*1024*1024+1)
    if not data or len(data)>20*1024*1024:raise AppError(413,'visual_upload_size','上传图像需在20 MiB以内')
    visual=build_visual(session,request.app.state.settings,project_id,values,uploaded=True)
    store_visual(session,request.app.state.settings,visual,data)
    visual.snapshot_json={**visual.snapshot_json,'original_filename':(file.filename or '').replace('\\','/').split('/')[-1],'source_sha256':hashlib.sha256(data).hexdigest()}
    session.commit();return row(visual)


@router.get('/production-visuals/{visual_id}')
def get_visual(visual_id:str,session:Session=Depends(get_db)):return row(get(session,m.ProductionVisual,visual_id))


@router.post('/production-visuals/{visual_id}/reconcile')
def reconcile_visual(visual_id:str,session:Session=Depends(get_db)):
    visual=get(session,m.ProductionVisual,visual_id)
    if visual.status!='unknown' or not visual.provider_job_id:raise AppError(409,'visual_reconcile_unavailable','仅有真实服务任务ID的未知状态可重查，不重新提交')
    if visual.snapshot_json.get('service_snapshot',{}).get('provider')!='comfyui':raise AppError(409,'cloud_image_reconcile_unavailable','此云端同步图像契约没有任务状态查询接口；请先到供应商核对原请求，不会重复提交')
    visual.status='submitted';visual.error_message=None;visual.snapshot_json={k:v for k,v in visual.snapshot_json.items() if k!='status_missing_since'};session.commit();return row(visual)


@router.post('/production-visuals/{visual_id}/review')
def review_visual(visual_id:str,payload:ReviewInput,request:Request,session:Session=Depends(get_db)):
    visual=get(session,m.ProductionVisual,visual_id)
    if visual.status not in ['needs_review','rejected','approved']:raise AppError(409,'visual_not_reviewable','实际图像尚未完成，不能批准')
    if payload.status=='approved':
        from app.core.paths import safe_media_path
        path=safe_media_path(request.app.state.settings.media_root,visual.relative_path,must_exist=True)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=visual.sha256:raise AppError(409,'visual_changed','图像哈希变化，拒绝批准')
        if visual.shot_id and visual.snapshot_json.get('inputs_hash')!=fingerprint(visual_inputs(session,get(session,m.StoryboardShot,visual.shot_id))):raise AppError(409,'visual_stale','镜头设定已变化，不能将旧关键画面当成本次结果批准')
        if visual.kind=='scene_reference' and visual.scene_id and visual.snapshot_json.get('visual_inputs',{}).get('environment')!=get(session,m.Scene,visual.scene_id).environment_json:raise AppError(409,'scene_visual_stale','场景环境已变化，请审核当前状态的新参考')
        if visual.kind=='scene_reference' and visual.entity_id:
            entity=get(session,m.ProductionEntity,visual.entity_id)
            if visual.snapshot_json.get('visual_inputs',{}).get('entity')!={'id':entity.id,'version':entity.version,'spec':entity.spec_json}:raise AppError(409,'scene_design_stale','场景状态设定已变化，请使用当前设定的新参考')
    visual.status=payload.status;visual.review_json={**payload.model_dump(),'reviewed_at':datetime.now(timezone.utc).isoformat()}
    session.add(m.AuditEvent(object_type='production_visual',object_id=visual.id,action='reviewed',details_json=visual.review_json));session.commit();return row(visual)


@router.post('/production-visuals/{visual_id}/retry',status_code=202)
def retry_visual(visual_id:str,payload:ReviewInput,request:Request,session:Session=Depends(get_db)):
    old=get(session,m.ProductionVisual,visual_id)
    if old.status not in ['failed','rejected','needs_review']:raise AppError(409,'visual_retry_unavailable','仅明确失败或已完成待审视觉可新建重试，未知状态须先核对')
    rules=old.snapshot_json['rules_snapshot'];parameters=old.snapshot_json['parameters']
    item=build_visual(session,request.app.state.settings,old.project_id,VisualInput(kind=old.kind,scene_id=old.scene_id,shot_id=old.shot_id,entity_id=old.entity_id,service_id=old.snapshot_json['service_snapshot']['service_id'],width=parameters['width'],height=parameters['height'],seed=old.snapshot_json['seed']+1,instructions=rules['repair_categories'][payload.category]+' '+payload.reason))
    item.snapshot_json={**item.snapshot_json,'retry_of':old.id};session.commit();return row(item)


@router.post('/projects/{project_id}/productions',status_code=201)
def start_run(project_id:str,payload:RunInput,request:Request,session:Session=Depends(get_db)):
    sync=check_generated_contract()
    if sync['status']!='passed':raise AppError(409,'production_rules_out_of_sync','制作规则、Schema或模板不同步，请先运行构建检查',sync)
    run=create_run(session,request.app.state.settings,project_id,payload);session.commit();return row(run)


@router.get('/productions/{run_id}')
def read_run(run_id:str,session:Session=Depends(get_db)):return row(get(session,m.ProductionRun,run_id))


@router.post('/productions/{run_id}/approve-inputs')
def approve_inputs(run_id:str,payload:ApprovalRequest,request:Request,session:Session=Depends(get_db)):
    from app.api.routes import approve_record,approve_shot
    run=get(session,m.ProductionRun,run_id);script=get(session,m.ScriptRevision,run.script_revision_id)
    if run.status in ['completed','cancelled']:raise AppError(409,'production_closed','此编排已结束')
    for step in run.steps_json:
        context_run=step['shot_id'] in run.snapshot_json.get('h3_context_by_shot',{})
        shot=get(session,m.StoryboardShot,step['shot_id']);pre=shot_preflight(session,request.app.state.settings,shot,require_visual=not context_run,context_inputs=context_run)
        if not pre['ready']:raise AppError(409,'production_inputs_not_ready','请先确认本镜绑定的制作设定及参考素材' if context_run else '请先确认设定并审核关键画面',pre)
    script.status='approved';script.approved_at=datetime.now(timezone.utc);script.approved_by=payload.operator
    approve_record(session,'script',script.id,script.revision,'approved',payload);session.commit()
    for step in run.steps_json:approve_shot(step['shot_id'],payload,session)
    run.status='ready';session.commit();return row(run)


@router.post('/productions/{run_id}/pause')
def pause_run(run_id:str,session:Session=Depends(get_db)):
    run=get(session,m.ProductionRun,run_id)
    if run.status in ['completed','cancelled']:raise AppError(409,'production_closed','编排已经结束')
    run.status='paused';run.error_message='只暂停后续编排；已创建的实际图像/视频/渲染任务继续显示各自真实状态';session.commit();return row(run)


@router.post('/productions/{run_id}/resume')
def resume_run(run_id:str,session:Session=Depends(get_db)):
    run=get(session,m.ProductionRun,run_id)
    if run.status!='paused':raise AppError(409,'production_not_paused','仅已暂停编排可继续；失败需定向修复')
    run.status='waiting_review';run.error_message=None;session.commit();return row(run)


@router.post('/productions/{run_id}/repair')
def repair_run(run_id:str,payload:dict,session:Session=Depends(get_db)):
    run=get(session,m.ProductionRun,run_id)
    if run.status not in ['quality_failed','failed']:raise AppError(409,'production_repair_unavailable','仅失败或质检未通过可定向重试')
    category=payload.get('category');reason=payload.get('reason');shot_id=payload.get('shot_id')
    rules=run.snapshot_json['rules_snapshot']
    if category not in rules['repair_categories'] or not isinstance(reason,str) or not reason.strip() or len(reason)>4000:raise AppError(422,'repair_reason_required','请选择实际问题分类并说明修复方向')
    steps=copy.deepcopy(run.steps_json);step=next((s for s in steps if s['shot_id']==shot_id),None)
    if not step:raise AppError(422,'repair_shot_mismatch','镜头不属于此编排')
    child=session.get(m.GenerationJob,step['video_job_id']) if step.get('video_job_id') else None
    if child and child.status in {'unknown','closed_unknown','submitting','queued','submitted','running','cancelling','reconciling'}:
        raise AppError(409,'production_repair_submission_unresolved','原任务仍执行或提交结果未知，请先核对原任务，系统未重复提交')
    history=list(run.result_json.get('attempt_history',[]))
    def phase(value):return value.get('repair_phase') or ('video' if value.get('video_job_id') else 'keyframe' if value.get('keyframe_id') else 'scene')
    current_phase=phase(step)
    if sum(h['shot_id']==shot_id and h['category']==category and phase(h)==current_phase for h in history)>=2:raise AppError(409,'production_retry_limit','本镜同一阶段的同一问题已定向修复2次，请检查输入后新建编排，禁止无限重复生成')
    history.append({**step,'category':category,'reason':reason,'repair_phase':current_phase})
    step.update(attempt=step['attempt']+1,video_job_id=None,video_id=None,stage='waiting_review',repair_category=category,repair_instruction=rules['repair_categories'][category]+' '+reason)
    if current_phase!='video' and category in ['identity','wardrobe','action','scene','prop']:
        step['keyframe_id']=None
    if current_phase!='video' and category=='scene':
        scene=production_context(session,get(session,m.StoryboardShot,shot_id))[1]
        scene_steps=copy.deepcopy(run.result_json.get('scene_steps',{}))
        if scene:scene_steps.pop(scene.id,None)
        run.result_json={**run.result_json,'scene_steps':scene_steps}
    run.result_json={**run.result_json,'attempt_history':history};run.steps_json=steps;run.status='waiting_review';run.error_message=None;session.commit();return row(run)


@router.post('/production-videos/{video_id}/review')
def review_production_video(video_id:str,payload:ReviewInput,request:Request,session:Session=Depends(get_db)):
    from app.api.routes import approve_video,reject_video
    video=get(session,m.VideoRevision,video_id)
    # Commit the review event with the delegated decision. Failed quality or
    # stale-input checks must not leave an "approved" audit event behind.
    session.add(m.AuditEvent(object_type='cinematic_video_review',object_id=video.id,action=payload.status,details_json=payload.model_dump()))
    if payload.status=='approved':return approve_video(video_id,ReviewRequest(reason=payload.reason),request=request,session=session)
    if payload.status=='rejected':return reject_video(video_id,ReviewRequest(reason=payload.reason),session)
    video.review_status='needs_review';video.review_reason=payload.reason;session.commit();return row(video)

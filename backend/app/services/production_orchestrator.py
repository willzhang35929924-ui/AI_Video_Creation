"""Durable, review-gated DAG over existing image, H3 and FFmpeg services."""
import asyncio
import copy
from sqlalchemy import select
from starlette.requests import Request
from app import models as m
from app.core.errors import AppError
from app.schemas import GenerationRequest
from app.schemas_cinematic import VisualInput
from app.schemas_product import AssemblyCreate,AssemblySubmit,TimelineRequest
from app.services.screenplay import get
from app.services.cinematic_quality import production_context,shot_preflight,generation_inputs_hash
from app.services.cinematic_visuals import build_visual


def create_run(session,settings,project_id,payload):
    if not payload.confirmed:raise AppError(409,'production_confirmation_required','先审核本次任务范围与资源需求，再明确确认编排')
    if settings.h3_demo_mode:raise AppError(409,'real_production_required','电影生产编排需要真实本地或云端服务')
    shots=[get(session,m.StoryboardShot,k) for k in dict.fromkeys(payload.shot_ids)]
    if any(s.project_id!=project_id for s in shots):raise AppError(422,'production_project_mismatch','镜头不属于本项目')
    if len(shots)>20:raise AppError(422,'assembly_clip_limit','现有正式合成最多20片段，请分批编排')
    scripts={production_context(session,s)[2].id if production_context(session,s)[2] else None for s in shots}
    if len(scripts)!=1 or None in scripts:raise AppError(422,'production_script_mismatch','一次编排只能使用同一剧本版本')
    script=get(session,m.ScriptRevision,next(iter(scripts)))
    if not script.parsed_json.get('versions'):raise AppError(409,'cinematic_revision_required','历史镜头保持原规则；先主动用新规范导入新剧本修订')
    active=session.scalar(select(m.ProductionRun).where(m.ProductionRun.project_id==project_id,m.ProductionRun.status.not_in(['completed','cancelled','failed','quality_failed','paused'])))
    if active:raise AppError(409,'production_already_active','当前项目已有待审核或执行中的编排',{'run_id':active.id})
    states=[]
    for shot in sorted(shots,key=lambda s:s.order):
        states.append({'shot_id':shot.id,'inputs_hash':generation_inputs_hash(session,shot),'stage':'waiting_design_review','attempt':0,'video_job_id':None,'video_id':None,'keyframe_id':None})
    from app.services.ai_services import resolve_service
    visual_service,visual_snapshot=resolve_service(session,'character_generation',payload.visual_service_id,project_id=project_id)
    video_service,video_snapshot=resolve_service(session,'video_generation',payload.video_service_id,project_id=project_id)
    if video_snapshot['provider']=='comfyui' and (payload.width%32 or payload.height%32 or max(payload.width,payload.height)>1344):raise AppError(422,'local_video_dimensions','本地H3宽高需32的倍数且不超过1344')
    if video_snapshot['provider']=='video_api' and video_snapshot['settings'].get('video_contract') not in {'video-jobs-v1','wan26-i2v','lk888-h3-v2'}:raise AppError(409,'video_contract_required','请选择已实现的视频任务协议；聊天接口不能代替视频生成')
    options=payload.model_dump();options.update(visual_service_id=visual_service.id,video_service_id=video_service.id)
    if visual_snapshot['provider']=='openai_compatible':options.update(visual_width=int(visual_snapshot['settings'].get('width',1024)),visual_height=int(visual_snapshot['settings'].get('height',1024)))
    run=m.ProductionRun(project_id=project_id,script_revision_id=script.id,status='waiting_design_review',snapshot_json={'versions':script.parsed_json['versions'],'rules_snapshot':script.parsed_json['rules_snapshot'],'options':options,'selected_service_snapshots':{'visual':visual_snapshot,'video':video_snapshot},'input_scope':[s.id for s in shots]},steps_json=states)
    session.add(run);session.flush();return run


def current_keyframe(session,shot,settings):
    pre=shot_preflight(session,settings,shot)
    visuals=list(session.scalars(select(m.ProductionVisual).where(m.ProductionVisual.shot_id==shot.id,m.ProductionVisual.kind=='keyframe').order_by(m.ProductionVisual.created_at.desc())))
    return next((v for v in visuals if v.snapshot_json.get('inputs_hash')==pre['inputs_hash'] and v.status in ['approved','needs_review','queued','submitting','submitted','running','unknown']),None)


async def advance_run(app,settings,session,run):
    if run.status in ['completed','cancelled','paused','failed','quality_failed']:return
    for snapshot in run.snapshot_json.get('selected_service_snapshots',{}).values():
        service=session.get(m.AIService,snapshot['service_id'])
        if not service or not service.enabled or service.revision!=snapshot['service_revision']:
            run.status='paused';run.error_message='服务配置已变化，原编排保留快照；请核对设置并新建编排';session.commit();return
    steps=copy.deepcopy(run.steps_json);options=run.snapshot_json['options'];script=get(session,m.ScriptRevision,run.script_revision_id)
    request=Request({'type':'http','app':app})
    # Existing committed child IDs are reconciled rather than resubmitted after
    # restart. A step never treats a queued job as finished.
    for step in steps:
        shot=get(session,m.StoryboardShot,step['shot_id'])
        if generation_inputs_hash(session,shot)!=step['inputs_hash']:
            run.status='paused';run.error_message='镜头内容已修改，旧编排保持原快照。请为受影响镜头新建编排';run.steps_json=copy.deepcopy(steps);session.commit();return
        pre=shot_preflight(session,settings,shot,False)
        blocking=[f for f in pre['findings'] if f['severity']=='blocker']
        if blocking:run.status='waiting_review';step['stage']='waiting_review';step['findings']=blocking;run.steps_json=copy.deepcopy(steps);session.commit();return
        if any(f['code']=='design_unreviewed' for f in pre['findings']):
            run.status='waiting_design_review';step['stage']='waiting_design_review';run.steps_json=copy.deepcopy(steps);session.commit();return
        reviews=[f for f in pre['findings'] if f['severity']=='review' and f['code']!='keyframe_review_required']
        if reviews:
            run.status='waiting_review';step['stage']='waiting_review';step['findings']=reviews;run.steps_json=copy.deepcopy(steps);session.commit();return
    # Prepare each location before keyframes. Scene refs are genuine images and
    # require review; they are not automatically certified as stable geometry.
    scene_steps=copy.deepcopy(run.result_json.get('scene_steps',{}))
    waiting_scenes=False
    for step in steps:
        shot=get(session,m.StoryboardShot,step['shot_id']);detail,scene,_,prod,entities=production_context(session,shot)
        if not scene:continue
        record=scene_steps.get(scene.id)
        if not record:
            candidates=list(session.scalars(select(m.ProductionVisual).where(m.ProductionVisual.scene_id==scene.id,m.ProductionVisual.kind=='scene_reference').order_by(m.ProductionVisual.created_at.desc())))
            visual=next((v for v in candidates if v.status in ['approved','needs_review','queued','submitting','submitted','running'] and v.entity_id==prod.get('scene_state_id') and v.snapshot_json.get('visual_inputs',{}).get('environment')==scene.environment_json),None)
            if not visual:
                visual=build_visual(session,settings,run.project_id,VisualInput(kind='scene_reference',scene_id=scene.id,entity_id=prod.get('scene_state_id'),service_id=options.get('visual_service_id'),width=options.get('visual_width',options['width']),height=options.get('visual_height',options['height']),seed=options['seed']))
            record={'visual_id':visual.id};scene_steps[scene.id]=record
        visual=get(session,m.ProductionVisual,record['visual_id'])
        run.result_json={**run.result_json,'scene_steps':copy.deepcopy(scene_steps)};session.commit()
        if visual.status in ['failed','rejected','unknown']:
            run.status='quality_failed' if visual.status=='rejected' else 'failed';run.error_message=visual.error_message or '场景参考未通过，请定向修复';session.commit();return
        if visual.status!='approved':waiting_scenes=True
    if waiting_scenes:run.status='waiting_visual_review';session.commit();return
    waiting_keyframes=False
    waiting_videos=False
    for step in steps:
        shot=get(session,m.StoryboardShot,step['shot_id'])
        visual=get(session,m.ProductionVisual,step['keyframe_id']) if step.get('keyframe_id') else current_keyframe(session,shot,settings)
        if not visual:visual=build_visual(session,settings,run.project_id,VisualInput(kind='keyframe',shot_id=shot.id,service_id=options.get('visual_service_id'),width=options.get('visual_width',options['width']),height=options.get('visual_height',options['height']),seed=options['seed']+shot.order+step['attempt'],instructions='' if step.get('repair_category')=='scene' else step.get('repair_instruction','')))
        step['keyframe_id']=visual.id
        if visual.status in ['failed','unknown','rejected']:
            run.status='quality_failed' if visual.status=='rejected' else 'failed';run.error_message=visual.error_message or '关键画面未通过，需要定向修复';run.steps_json=copy.deepcopy(steps);session.commit();return
        if visual.status!='approved':waiting_keyframes=True;step['stage']='waiting_visual_review'
        else:
            detail,scene,_,prod,_=production_context(session,shot);needed=[visual.id]+([scene_steps[scene.id]['visual_id']] if scene and scene.id in scene_steps else [])
            if not set(needed).issubset(prod.get('visual_ids',[])):
                from app.services.screenplay import touch_shot
                touch_shot(session,shot.id);detail.source_json={**detail.source_json,'production':{**prod,'visual_ids':list(dict.fromkeys(prod.get('visual_ids',[])+needed))}}
        run.steps_json=copy.deepcopy(steps);session.commit()
    if waiting_keyframes:run.status='waiting_visual_review';session.commit();return
    for step in steps:
        shot=get(session,m.StoryboardShot,step['shot_id'])
        visual=get(session,m.ProductionVisual,step['keyframe_id']) if step.get('keyframe_id') else current_keyframe(session,shot,settings)
        if not visual:
            visual=build_visual(session,settings,run.project_id,VisualInput(kind='keyframe',shot_id=shot.id,service_id=options.get('visual_service_id'),width=options.get('visual_width',options['width']),height=options.get('visual_height',options['height']),seed=options['seed']+shot.order))
        step['keyframe_id']=visual.id
        if visual.status in ['failed','unknown','rejected']:
            step['stage']='quality_failed' if visual.status=='rejected' else 'failed';run.status=step['stage'];run.error_message=visual.error_message or '关键画面被否决，按问题分类修复';run.steps_json=copy.deepcopy(steps);session.commit();return
        if visual.status!='approved':
            step['stage']='waiting_visual_review';run.status='waiting_visual_review';run.steps_json=copy.deepcopy(steps);session.commit();return
        detail,scene,_,prod,_=production_context(session,shot)
        needed=[visual.id]+([scene_steps[scene.id]['visual_id']] if scene and scene.id in scene_steps else [])
        if not set(needed).issubset(prod.get('visual_ids',[])):
            # Explicitly approved visuals can be bound automatically; this changes
            # reviewed shot inputs, so normal shot approval is invalidated.
            from app.services.screenplay import touch_shot
            touch_shot(session,shot.id);prod={**prod,'visual_ids':list(dict.fromkeys(prod.get('visual_ids',[])+needed))};detail.source_json={**detail.source_json,'production':prod};session.commit()
        pre=shot_preflight(session,settings,shot)
        if not pre['ready'] or script.status!='approved' or shot.status!='approved':
            step['stage']='waiting_review';run.status='waiting_review';run.steps_json=copy.deepcopy(steps);session.commit();return
        if not step.get('video_job_id'):
            # Reuse only actually approved same-input versions, never stale shots.
            reused=None
            for video in session.scalars(select(m.VideoRevision).where(m.VideoRevision.shot_id==shot.id,m.VideoRevision.review_status=='approved').order_by(m.VideoRevision.revision.desc())):
                old=session.get(m.GenerationJob,video.generation_job_id)
                if old.parameters_json.get('screenplay_snapshot',{}).get('cinematic',{}).get('preflight',{}).get('generation_inputs_hash')==step['inputs_hash']:reused=video;break
            if reused:step.update(video_id=reused.id,video_job_id=reused.generation_job_id,stage='approved_reused')
            else:
                from app.api.routes import generate_for_shot
                job,_=await generate_for_shot(request,session,shot,GenerationRequest(service_id=options.get('video_service_id'),seed=options['seed']+shot.order+step['attempt'],parameters={'width':options['width'],'height':options['height'],'requested_mode':'real'}),instruction=step.get('repair_instruction'))
                step.update(video_job_id=job.id,stage='running');run.steps_json=copy.deepcopy(steps);run.status='running';session.commit();return
        job=get(session,m.GenerationJob,step['video_job_id'])
        if job.status in ['failed','unknown','closed_unknown','cancelled']:
            step['stage']='failed';run.status='failed';run.error_message=job.error_message or job.status;run.steps_json=copy.deepcopy(steps);session.commit();return
        video=session.scalar(select(m.VideoRevision).where(m.VideoRevision.generation_job_id==job.id))
        if not video:run.status='running';run.steps_json=copy.deepcopy(steps);session.commit();return
        step['video_id']=video.id
        if session.scalar(select(m.QualityCheck.id).where(m.QualityCheck.video_revision_id==video.id,m.QualityCheck.status=='failed').limit(1)):
            run.status='quality_failed';step['stage']='quality_failed';run.error_message='实际视频技术检查未通过，先修复该镜；不会继续批量生成或合成';run.steps_json=copy.deepcopy(steps);session.commit();return
        if video.review_status!='approved':
            if video.review_status=='rejected':
                run.status='quality_failed';step['stage']=run.status;run.steps_json=copy.deepcopy(steps);session.commit();return
            waiting_videos=True;step['stage']='waiting_video_review';run.steps_json=copy.deepcopy(steps);session.commit();continue
        step['stage']='approved';run.steps_json=copy.deepcopy(steps);session.commit()
    if waiting_videos:
        run.status='waiting_video_review';session.commit();return
    assembly_id=run.result_json.get('assembly_id')
    if not assembly_id:
        from app.api.assemblies import create
        existing=session.scalar(select(m.Assembly).where(m.Assembly.project_id==run.project_id,m.Assembly.name=='电影编排 '+run.id))
        if existing:assembly_id=existing.id
        else:
            timeline=TimelineRequest.model_validate({'clips':[{'video_id':s['video_id']} for s in steps],'transitions':[{'type':'cut','duration':0,'audio':'cut'} for _ in steps[1:]],'output':{'width':options['assembly_width'],'height':options['assembly_height'],'fps':24,'codec':'libx264','audio_policy':'preserve'}})
            assembly=create(AssemblyCreate(project_id=run.project_id,name='电影编排 '+run.id,timeline=timeline),request,session);assembly_id=assembly['id']
        run.result_json={**run.result_json,'assembly_id':assembly_id};session.commit()
    assembly=get(session,m.Assembly,assembly_id)
    render=session.scalar(select(m.AssemblyRender).where(m.AssemblyRender.assembly_id==assembly_id,m.AssemblyRender.kind=='render').order_by(m.AssemblyRender.version.desc()))
    if not render:
        from app.api.assemblies import submit
        submitted=submit(assembly_id,AssemblySubmit(revision=assembly.revision),request,session,'render');render=get(session,m.AssemblyRender,submitted['id'])
    run.result_json={**run.result_json,'render_id':render.id,'relative_path':render.relative_path};run.status='completed' if render.status=='generated' else 'failed' if render.status in ['failed','cancelled'] else 'rendering';run.error_message=render.error_message;session.commit()


async def production_worker(app,settings,session_factory):
    while True:
        try:
            with session_factory() as session:
                for run in session.scalars(select(m.ProductionRun).where(m.ProductionRun.status.not_in(['completed','cancelled','paused','failed','quality_failed'])).order_by(m.ProductionRun.created_at)):
                    try:await advance_run(app,settings,session,run)
                    except AppError as exc:run.status='waiting_service_verification' if exc.code=='cloud_visual_single_validation_required' else 'failed';run.error_message=exc.message;session.commit()
                    except Exception as exc:
                        session.rollback();run=session.get(m.ProductionRun,run.id);run.status='failed';run.error_message=type(exc).__name__+'：编排失败，请检查日志后定向重试';session.commit()
                        import logging
                        logging.getLogger(__name__).exception('Production orchestration failed')
        except asyncio.CancelledError:raise
        await asyncio.sleep(3)

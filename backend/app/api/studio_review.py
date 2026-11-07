"""One persisted review decision per shot; a single confirmed real composition."""
from fastapi import APIRouter,Depends,Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import models as m
from app.core.db import get_db
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.schemas import ReviewRequest
from app.schemas_product import AssemblyCreate,AssemblySubmit,TimelineRequest
from app.schemas_studio import DecisionInput,ComposeInput,ComposeResult,StudioDecision as DecisionDTO
from app.services.screenplay import get,row

router=APIRouter(prefix='/api/studio')

def current_shots(session,project_id):
    from app.api.routes import project_shot_statement
    get(session,m.Project,project_id)
    return list(session.scalars(project_shot_statement(session,project_id).order_by(m.StoryboardShot.order)))

def decisions(session,project_id,shots):
    result={d.shot_id:d for d in session.scalars(select(m.StudioDecision).where(m.StudioDecision.project_id==project_id))}
    for n,shot in enumerate(shots,1):
        if shot.id not in result:
            d=m.StudioDecision(project_id=project_id,shot_id=shot.id,position=n);session.add(d);session.flush();result[shot.id]=d
    return result

@router.post('/projects/{project_id}/shots/{shot_id}/decision',response_model=DecisionDTO)
async def decide(project_id:str,shot_id:str,payload:DecisionInput,request:Request,session:Session=Depends(get_db)):
    shots=current_shots(session,project_id);ids=[s.id for s in shots]
    if shot_id not in ids:raise AppError(422,'studio_shot_mismatch','镜头不属于当前项目的当前剧本版本')
    all_rows=decisions(session,project_id,shots);decision=all_rows[shot_id]
    if payload.expected_revision is not None and payload.expected_revision!=decision.revision:raise AppError(409,'studio_review_changed','审片决定已在其他窗口修改，请刷新')
    video=session.get(m.VideoRevision,payload.video_id) if payload.video_id else session.scalar(select(m.VideoRevision).where(m.VideoRevision.shot_id==shot_id).order_by(m.VideoRevision.revision.desc()))
    if video and (video.project_id!=project_id or video.shot_id!=shot_id):raise AppError(422,'studio_video_mismatch','所选视频不是当前镜头的版本')
    if payload.video_id and not video:raise AppError(404,'video_not_found','所选视频不存在')
    if payload.position is not None and payload.position>len(shots):raise AppError(422,'studio_position_out_of_range','排序不能超过当前镜头数量')
    if payload.action=='keep':
        if not video:raise AppError(409,'studio_video_not_ready','此镜头还没有真实视频可保留')
        safe_media_path(request.app.state.settings.media_root,video.relative_path,must_exist=True)
        from app.services.assembly import probe
        metadata=probe(safe_media_path(request.app.state.settings.media_root,video.relative_path,must_exist=True),request.app.state.settings)
        if payload.trim_start>=(payload.trim_end or metadata['duration']) or (payload.trim_end and payload.trim_end>metadata['duration']+.02):raise AppError(422,'invalid_clip_trim','裁剪范围超出实际视频')
    if payload.action in ['modify','regenerate']:
        # Validate/create the scoped run before clearing an existing decision.
        # A conflict leaves both the kept version and its order intact.
        from app.services.studio_pipeline import create_pipeline
        previous=session.scalar(select(m.StudioRun).where(m.StudioRun.project_id==project_id).order_by(m.StudioRun.created_at.desc()))
        options=dict(previous.snapshot_json.get('options',{})) if previous else {}
        prior_job=session.get(m.GenerationJob,video.generation_job_id) if video else None
        original=prior_job.parameters_json if prior_job else {}
        context=original.get('context_inputs') or {}
        prior_quality=options.get('quality_by_shot',{}).get(shot_id) or original.get('quality_request')
        prompt=payload.prompt_override if payload.prompt_override is not None else context.get('prompt_override', options.get('prompt_overrides',{}).get(shot_id))
        images=context.get('explicit_visual_ids',options.get('reference_image_ids',{}).get(shot_id))
        motion=context.get('explicit_motion',options.get('motion_reference_overrides',{}).get(shot_id))
        options.update(shot_ids=[shot_id],force_shot_ids=[shot_id],instructions={shot_id:payload.instruction},
            prompt_overrides={shot_id:prompt} if prompt is not None else {},
            reference_image_ids={shot_id:images} if images is not None else {},
            motion_reference_overrides={shot_id:motion} if motion is not None else {})
        create_pipeline(session,request.app.state.settings,project_id,options,
                        prior_quality={shot_id:prior_quality} if prior_quality else None)
    if payload.position is not None:
        ordered=sorted((all_rows[s.id] for s in shots),key=lambda d:(d.position,ids.index(d.shot_id)))
        ordered.remove(decision);ordered.insert(payload.position-1,decision)
        for n,d in enumerate(ordered,1):
            if d.position!=n:d.position=n;d.revision+=1
    decision.action=payload.action if payload.action in ['keep','exclude','pending'] else 'pending'
    decision.video_id=video.id if video else None;decision.trim_start=payload.trim_start;decision.trim_end=payload.trim_end;decision.instruction=payload.instruction;decision.revision+=1
    if payload.action in ['modify','regenerate']:
        decision.video_id=None
    if payload.action=='keep':
        from app.api.routes import approve_video
        approve_video(video.id,ReviewRequest(reason='统一审片：'+('验收测试保留，不代表用户视觉批准' if payload.operator=='acceptance-test' else '用户保留该真实视频版本'),operator=payload.operator),request=request,session=session)
    session.commit()
    return DecisionDTO.model_validate(row(decision))

@router.post('/projects/{project_id}/compose',response_model=ComposeResult,status_code=202)
def compose(project_id:str,payload:ComposeInput,request:Request,session:Session=Depends(get_db)):
    run=session.scalar(select(m.StudioRun).where(m.StudioRun.project_id==project_id).order_by(m.StudioRun.created_at.desc()))
    route=run.snapshot_json.get('routes',{}).get('final_assembly') if run else None
    from app.services.task_routing import route_is_explicit
    explicit=route_is_explicit(route)
    service_id=None
    if explicit:
        from app.services.task_routing import assert_frozen
        selected,frozen=assert_frozen(session,route);service_id=selected.id
        if frozen.get('provider')!='ffmpeg':raise AppError(409,'assembly_route_adapter','此合成路由尚无已实现的适配器')
        if route.get('profile',{}).get('settings',{}).get('parameters'):raise AppError(422,'assembly_route_parameters_unsupported','合成输出参数使用已确认的时间线，不静默覆盖')
    shots=current_shots(session,project_id);rows=decisions(session,project_id,shots)
    if payload.expected_decisions is not None and payload.expected_decisions!={s.id:rows[s.id].revision for s in shots}:raise AppError(409,'studio_review_changed','审片决定或镜头范围已变化，请刷新最终合成摘要后重新确认')
    if not shots or any(rows[s.id].action=='pending' for s in shots):raise AppError(409,'studio_review_pending','请先对当前每个镜头选择保留或排除')
    kept=sorted((rows[s.id] for s in shots if rows[s.id].action=='keep'),key=lambda d:d.position)
    if not kept:raise AppError(422,'studio_no_kept_shots','至少保留一个真实镜头')
    clips=[]
    for d in kept:
        video=get(session,m.VideoRevision,d.video_id)
        if video.shot_id!=d.shot_id or video.project_id!=project_id or video.review_status!='approved':raise AppError(409,'studio_review_stale','已保留的视频版本与审片决定不一致，请刷新')
        clips.append({'video_id':d.video_id,'trim_start':d.trim_start,'trim_end':d.trim_end})
    mode = payload.resolution_mode or ('custom' if {'width', 'height'} & payload.model_fields_set else 'source')
    timeline=TimelineRequest.model_validate({'clips':clips,'transitions':[{'type':payload.transition,'duration':payload.transition_duration,'audio':'crossfade' if payload.transition=='crossfade' else 'cut'} for _ in kept[1:]],'output':{'width':payload.width,'height':payload.height,'resolution_mode':mode,'allow_downscale':payload.allow_downscale,'fps':payload.fps,'codec':'libx264','audio_policy':payload.audio_policy,'subtitles':payload.subtitles},'service_id':service_id})
    from app.api.assemblies import create,submit
    assembly=create(AssemblyCreate(project_id=project_id,name='统一审片成片',timeline=timeline),request,session)
    snapshot_decisions=jsonable_encoder([row(rows[s.id]) for s in shots])
    a=get(session,m.Assembly,assembly['id']);a.timeline_json={**a.timeline_json,'studio_decisions':snapshot_decisions};session.commit()
    render=submit(a.id,AssemblySubmit(revision=a.revision),request,session,'render',route_snapshot=route if explicit else None)
    r=get(session,m.AssemblyRender,render['id']);r.snapshot_json={**r.snapshot_json,'studio_decisions':snapshot_decisions}
    if run:
        run.result_json={**run.result_json,'assembly_id':a.id,'render_id':r.id}
        run.status='rendering';run.error_message=None
    session.commit()
    return ComposeResult(assembly_id=a.id,render_id=r.id,status=r.status)

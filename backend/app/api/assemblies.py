from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.routes import audit, required, row_dict
from app.core.db import get_db
from app.core.errors import AppError
from app.models import Assembly, AssemblyRender, Project
from app.schemas_product import AssemblyCreate, AssemblyPatch, AssemblySubmit, TimelineRequest
from app.services.assembly import TRANSITIONS, preview_snapshot, validate_timeline

router = APIRouter(prefix='/api')


def render_dict(row):
    return {**row_dict(row), 'media_url':'/api/media/'+row.relative_path if row.relative_path else None}


def draft_timeline(session, settings, assembly, payload):
    """Revalidate edits to an existing automatic draft without granting approval."""
    marker = (assembly.timeline_json or {}).get('automatic_draft')
    if not marker:
        return validate_timeline(session, settings, assembly.project_id, payload)
    from app.models import GenerationJob, StoryboardShot, VideoRevision
    from app.services.cinematic_quality import generation_inputs_hash
    from app.services.studio_pipeline import _valid_video
    original_ids = {clip['video_id'] for clip in assembly.timeline_json.get('clips', [])}
    # Keep previously removed draft clips selectable from immutable render
    # history, without admitting unrelated unreviewed project videos.
    for prior in session.scalars(select(AssemblyRender).where(AssemblyRender.assembly_id == assembly.id)):
        saved = prior.snapshot_json or {}
        if saved.get('automatic_draft'):
            original_ids.update(clip['video_id'] for clip in saved.get('clips', []))
    for item in payload.clips:
        video = session.get(VideoRevision, item.video_id)
        if not video or video.project_id != assembly.project_id or video.review_status not in {'approved', 'pending'}:
            raise AppError(422, 'draft_input_invalid', '草稿片段已被驳回、失效或不属于当前项目')
        if video.review_status == 'pending' and video.id not in original_ids:
            raise AppError(422, 'draft_input_not_in_timeline', '待审核片段须来自该草稿原时间线；新增片段请先审核')
        job = session.get(GenerationJob, video.generation_job_id)
        shot = session.get(StoryboardShot, video.shot_id)
        expected = (job.parameters_json.get('screenplay_snapshot', {}).get('cinematic', {}).get('preflight', {}).get('generation_inputs_hash')) if job else None
        if not shot or not expected or expected != generation_inputs_hash(session, shot):
            raise AppError(409, 'draft_video_changed', '草稿片段的镜头输入已变化，请重新生成或审核')
        _valid_video(session, settings, video)
    timeline = validate_timeline(session, settings, assembly.project_id, payload, allow_unreviewed_draft=True)
    timeline['automatic_draft'] = {**marker, 'human_review_required': True}
    return timeline


@router.get('/assembly-effects')
def effects():
    return {'effects': TRANSITIONS, 'directions':['left','right','up','down'], 'easing':'FFmpeg 原生算法；没有未经实现的缓动参数'}


@router.get('/projects/{project_id}/assemblies')
def list_assemblies(project_id: str, session: Session = Depends(get_db)):
    return [row_dict(row) for row in session.scalars(select(Assembly).where(Assembly.project_id == project_id).order_by(Assembly.created_at.desc()))]


@router.post('/assemblies', status_code=201)
def create(payload: AssemblyCreate, request: Request, session: Session = Depends(get_db)):
    required(session, Project, payload.project_id, '项目')
    timeline = validate_timeline(session, request.app.state.settings, payload.project_id, payload.timeline)
    row = Assembly(project_id=payload.project_id, name=payload.name, timeline_json=timeline)
    session.add(row)
    session.flush()
    audit(session, 'assembly', row.id, 'created', {'revision':1})
    session.commit()
    return row_dict(row)


@router.get('/assemblies/{assembly_id}')
def detail(assembly_id: str, session: Session = Depends(get_db)):
    return row_dict(required(session, Assembly, assembly_id, '时间线'))


@router.patch('/assemblies/{assembly_id}')
def patch(assembly_id: str, payload: AssemblyPatch, request: Request, session: Session = Depends(get_db)):
    row = required(session, Assembly, assembly_id, '时间线')
    if payload.revision != row.revision:
        raise AppError(409, 'timeline_changed', '时间线已由其他窗口修改，请刷新后重试')
    row.timeline_json = draft_timeline(session, request.app.state.settings, row, payload.timeline)
    row.name = payload.name
    row.revision += 1
    audit(session, 'assembly', row.id, 'updated', {'revision':row.revision})
    session.commit()
    return row_dict(row)


def submit(assembly_id, payload, request, session, kind,route_snapshot=None):
    row = required(session, Assembly, assembly_id, '时间线')
    if payload.revision != row.revision:
        raise AppError(409, 'timeline_changed', '请先保存并刷新时间线，再提交当前版本')
    # Revalidate source approvals, files and service; the render owns an immutable copy.
    original = row.timeline_json
    from app.models import AIService
    if route_snapshot:
        from app.services.task_routing import assert_frozen
        selected_service,frozen=assert_frozen(session,route_snapshot)
        if route_snapshot.get('slot')!='final_assembly' or frozen.get('provider')!='ffmpeg':raise AppError(422,'assembly_route_adapter','此合成入口只支持已验证FFmpeg路由')
    else:
        selected_service = required(session,AIService,original['service_snapshot']['service_id'],'处理服务')
        if selected_service.revision != original['service_snapshot']['service_revision']:
            raise AppError(409,'assembly_service_changed','处理服务已更改，请明确重新保存时间线后再渲染；历史快照未改变')
    # Historical snapshots created before full-clip intent was preserved can
    # contain a numeric derived trim_end even though no edit was requested.
    # Revalidate the actual media; never turn this derived value into a cut.
    clips = []
    for clip in original['clips']:
        spec = {k:clip[k] for k in ['video_id','trim_start','trim_end'] if k in clip}
        if clip.get('full_stream_timing',{}):
            spec['trim_end'] = None
        clips.append(spec)
    config = TimelineRequest.model_validate({'clips':clips, 'transitions':original['transitions'], 'output':original['output'], 'service_id':selected_service.id})
    snapshot = draft_timeline(session, request.app.state.settings, row, config)
    if kind == 'preview':
        snapshot = preview_snapshot(snapshot, payload.transition_index)
    snapshot.update(project_id=row.project_id, assembly_revision=row.revision, assembly_name=row.name)
    if route_snapshot:snapshot['task_route_snapshot']=route_snapshot
    version = (session.scalar(select(func.max(AssemblyRender.version)).where(AssemblyRender.assembly_id == row.id)) or 0) + 1
    result = AssemblyRender(assembly_id=row.id, version=version, kind=kind, snapshot_json=snapshot)
    session.add(result)
    session.flush()
    audit(session, 'assembly_render', result.id, 'queued', {'version':version,'kind':kind})
    session.commit()
    return render_dict(result)


@router.post('/assemblies/{assembly_id}/preview', status_code=202)
def preview(assembly_id: str, payload: AssemblySubmit, request: Request, session: Session = Depends(get_db)):
    return submit(assembly_id, payload, request, session, 'preview')


@router.post('/assemblies/{assembly_id}/render', status_code=202)
def render(assembly_id: str, payload: AssemblySubmit, request: Request, session: Session = Depends(get_db)):
    return submit(assembly_id, payload, request, session, 'render')


@router.get('/assemblies/{assembly_id}/versions')
def versions(assembly_id: str, session: Session = Depends(get_db)):
    required(session, Assembly, assembly_id, '时间线')
    return [render_dict(row) for row in session.scalars(select(AssemblyRender).where(AssemblyRender.assembly_id == assembly_id).order_by(AssemblyRender.version.desc()))]


@router.get('/assemblies/{assembly_id}/jobs/{job_id}')
def job(assembly_id: str, job_id: str, session: Session = Depends(get_db)):
    row = required(session, AssemblyRender, job_id, '渲染任务')
    if row.assembly_id != assembly_id:
        raise AppError(404, 'render_not_found', '此任务不属于当前时间线')
    return render_dict(row)


@router.post('/assemblies/{assembly_id}/jobs/{job_id}/cancel')
def cancel(assembly_id: str, job_id: str, session: Session = Depends(get_db)):
    row = required(session, AssemblyRender, job_id, '渲染任务')
    if row.assembly_id != assembly_id:
        raise AppError(404, 'render_not_found', '此任务不属于当前时间线')
    if row.status == 'queued':
        row.status = 'cancelled'
    elif row.status in ('running','cancelling'):
        row.cancel_requested = True
        row.status = 'cancelling'
    else:
        raise AppError(409, 'render_finished', '任务已经结束')
    session.commit()
    return render_dict(row)

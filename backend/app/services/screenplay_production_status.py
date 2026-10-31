"""Link real Studio evidence to the originating screenplay's fourth stage.

This is a transactional projection, not a model/asset generator or approval.
"""
import copy
from sqlalchemy import select
from app import models as m


def sync_production_status(session, run):
    project = session.get(m.Project, run.project_id)
    uri = project.source_uri if project else None
    if not isinstance(uri, str) or not uri.startswith('screenplay_job:'):
        return
    job = session.get(m.ScreenplayGenerationJob, uri[len('screenplay_job:'):])
    if not job or job.project_id != project.id:
        return
    # A late poll of an older run must not overwrite the latest run's status.
    latest = session.scalar(select(m.StudioRun.id).where(m.StudioRun.project_id == project.id)
        .order_by(m.StudioRun.created_at.desc(), m.StudioRun.id.desc()).limit(1))
    if latest != run.id:
        return
    nodes = run.nodes_json or []
    selected_shots = set((run.snapshot_json or {}).get('shot_ids', []))
    identifiers = {n.get('video_id') for n in nodes if n.get('video_id')}
    identifiers.update(((run.result_json or {}).get('videos') or {}).values())
    videos = []
    for identifier in sorted(identifier for identifier in identifiers if isinstance(identifier, str)):
        video = session.get(m.VideoRevision, identifier)
        if (video and video.project_id == project.id and video.shot_id in selected_shots
                and video.relative_path):
            videos.append({'video_id': video.id, 'shot_id': video.shot_id,
                'relative_path': video.relative_path, 'review_status': video.review_status})
    state = 'generating'
    errors = {n.get('status') for n in nodes}
    if 'unknown' in errors:
        state = 'unknown'
    elif run.status in {'completed_with_errors', 'exceptions'} or errors & {'failed', 'exception'}:
        state = 'failed'
    elif run.status == 'cancelled':
        state = 'cancelled'
    elif run.status == 'paused':
        state = 'paused'
    elif run.status in {'waiting_review', 'completed'}:
        state = 'awaiting_review' if videos else 'failed'
    render_id = (run.result_json or {}).get('render_id')
    render_evidence = None
    if render_id:
        render = session.get(m.AssemblyRender, render_id)
        assembly = session.get(m.Assembly, render.assembly_id) if render else None
        if not render or not assembly or assembly.project_id != project.id:
            state = 'failed'
        else:
            render_evidence = {'render_id': render.id, 'status': render.status,
                'relative_path': render.relative_path}
            if render.status == 'generated' and render.relative_path:
                state = 'succeeded'
            elif render.status in {'failed', 'cancelled', 'unknown'}:
                state = render.status
            elif render.status in {'queued', 'running', 'rendering'}:
                state = 'generating'
            else:
                state = 'unknown'
    receipt = copy.deepcopy(job.receipt_json or {})
    source = receipt.setdefault('source', {})
    stages = source.setdefault('four_stage_status', {})
    stages['new_video'] = state
    source['video_production'] = {'run_id': run.id, 'run_status': run.status,
        'state': state, 'videos': videos, 'render_id': render_id,
        'render': render_evidence, 'human_visual_approval_inferred': False}
    if receipt != job.receipt_json:
        job.receipt_json = receipt


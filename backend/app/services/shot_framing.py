"""Explicit current-shot framing changes, without dispatching models or altering frozen runs."""
from __future__ import annotations

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import aliased

from app import models as m
from app.core.errors import AppError
from app.schemas_studio import ShotFramingUpdate

ASPECTS=['16:9','9:16','1:1']
ACTIVE={'queued','submitting','submitted','running','postprocessing','cancelling','reconciling','unknown','deferred'}
EDITABLE_RUNS={'paused','exceptions','completed','completed_with_errors','cancelled','failed'}
BLOCK_MESSAGES={
    'run':'当前制作批次尚未暂停，请先暂停，再修改画幅并新建制作；原冻结任务保留。',
    'generation':'本镜生成任务正在执行或结果未知，请先等待或核对原任务，不能修改画幅。',
    'visual':'本镜关联的参考图或关键画面正在处理或结果未知，不能修改画幅。',
    'media':'本镜关联的配音、混音或口型任务尚未结束，不能修改画幅。',
    'quality':'本镜关联的视频画质任务尚未结束或结果未知，不能修改画幅。',
}

def _shots(session,project_id):
    if session.get(m.Project,project_id) is None:
        raise AppError(404,'project_missing','项目不存在')
    from app.services.studio_pipeline import _shots as current_shots
    return current_shots(session,project_id)

def _conditions():
    """Correlated SQL guards are rechecked by the conditional UPDATE itself."""
    shot=m.StoryboardShot
    run=aliased(m.StudioRun)
    latest_run=select(m.StudioRun.id).where(m.StudioRun.project_id==shot.project_id).order_by(
        m.StudioRun.created_at.desc(),m.StudioRun.id.desc()).limit(1).correlate(shot).scalar_subquery()
    run_busy=select(run.id).where(run.project_id==shot.project_id,or_(
        run.status.in_(ACTIVE),and_(run.id==latest_run,run.status.not_in(EDITABLE_RUNS)))).exists().correlate(shot)
    generation=select(m.GenerationJob.id).where(m.GenerationJob.shot_id==shot.id,
        m.GenerationJob.status.in_(ACTIVE)).exists().correlate(shot)
    scene_ids=select(m.ShotDetail.scene_id).where(m.ShotDetail.shot_id==shot.id).correlate(shot)
    visual=select(m.ProductionVisual.id).where(m.ProductionVisual.project_id==shot.project_id,
        m.ProductionVisual.status.in_(ACTIVE),or_(m.ProductionVisual.shot_id==shot.id,
            and_(m.ProductionVisual.shot_id.is_(None),or_(m.ProductionVisual.scene_id.is_(None),
                m.ProductionVisual.scene_id.in_(scene_ids))))).exists().correlate(shot)
    media=select(m.MediaProcessTask.id).where(m.MediaProcessTask.project_id==shot.project_id,
        m.MediaProcessTask.status.in_(ACTIVE),or_(m.MediaProcessTask.shot_id==shot.id,
            m.MediaProcessTask.shot_id.is_(None))).exists().correlate(shot)
    quality=select(m.QualityRender.id).join(m.VideoRevision,m.VideoRevision.id==m.QualityRender.source_video_id).where(
        m.VideoRevision.shot_id==shot.id,m.QualityRender.status.in_(ACTIVE)).exists().correlate(shot)
    return {'run':run_busy,'generation':generation,'visual':visual,'media':media,'quality':quality}

def _blocked(session,shots):
    if not shots:return {}
    conditions=_conditions()
    rows=session.execute(select(m.StoryboardShot.id,*[value.label(key) for key,value in conditions.items()]).where(
        m.StoryboardShot.id.in_([shot.id for shot in shots])))
    return {row.id:next((BLOCK_MESSAGES[key] for key in conditions if row._mapping[key]),None) for row in rows}

def overview(session,project_id):
    shots=_shots(session,project_id);blocked=_blocked(session,shots)
    return {'project_id':project_id,'supported_aspect_ratios':list(ASPECTS),'requires_new_run':requires_new_run(session,project_id),'shots':[
        {'id':shot.id,'order':shot.order,'title':shot.scene_title,'aspect_ratio':shot.aspect_ratio,
         'revision':shot.revision,'editable':not bool(blocked.get(shot.id)),'block_reason':blocked.get(shot.id)}
        for shot in shots]}

def requires_new_run(session,project_id,run=None):
    from app.services.studio_pipeline import latest_run
    run=run or latest_run(session,project_id)
    if run is None:return False
    return session.scalar(select(m.AuditEvent.id).where(m.AuditEvent.object_type=='shot',
        m.AuditEvent.action=='aspect_ratio_changed',
        m.AuditEvent.details_json['project_id'].as_string()==project_id,
        m.AuditEvent.details_json['run_id'].as_string()==run.id).limit(1)) is not None

def video_framing_unchanged():
    """SQL guard shared by the approval read and its final atomic update."""
    return ~select(m.AuditEvent.id).where(m.AuditEvent.object_type=='shot',
        m.AuditEvent.object_id==m.VideoRevision.shot_id,
        m.AuditEvent.action=='aspect_ratio_changed',
        m.AuditEvent.created_at>=m.VideoRevision.created_at).exists().correlate(m.VideoRevision)

def assert_video_framing_current(session,video):
    # The persistent marker also follows quality-derived copies of old media.
    # Audit fallback protects legacy videos and the approval/framing race.
    stale=(video.metadata_json or {}).get('framing_invalidated') or session.scalar(
        select(m.VideoRevision.id).where(m.VideoRevision.id==video.id,~video_framing_unchanged()))
    if stale:
        raise AppError(409,'video_needs_regeneration','该视频生成后镜头画幅已变化，旧画幅不能重新批准；请生成并审核新版本。')

def _stable_script(session,project_id):
    from app.api.routes import latest_script
    original=latest_script(session,project_id)
    current=select(m.ScriptRevision.id).where(m.ScriptRevision.project_id==project_id).order_by(
        m.ScriptRevision.revision.desc()).limit(1).scalar_subquery()
    return current==original.id if original else current.is_(None)

def save(session,project_id,payload:ShotFramingUpdate):
    payload=ShotFramingUpdate.model_validate(payload)
    current=_shots(session,project_id);by_id={shot.id:shot for shot in current}
    if any(item.id not in by_id for item in payload.shots):
        raise AppError(422,'shot_framing_scope','请选择当前项目当前剧本内未删除的镜头；整批未保存。')
    selected=[by_id[item.id] for item in payload.shots]
    for item in payload.shots:
        if by_id[item.id].revision!=item.expected_revision:
            raise AppError(409,'shot_framing_revision_changed','镜头已被其他操作更新，请刷新后重试；整批未保存。',
                           {'shot_id':item.id,'expected_revision':item.expected_revision,'actual_revision':by_id[item.id].revision})
    blocked=_blocked(session,selected)
    if any(blocked.values()):
        raise AppError(409,'shot_framing_blocked','；'.join(dict.fromkeys(reason for reason in blocked.values() if reason)),
                       {'shots':[{'id':identifier,'block_reason':reason} for identifier,reason in blocked.items() if reason]})
    stable_script=_stable_script(session,project_id)
    from app.services.studio_pipeline import latest_run
    current_run=latest_run(session,project_id)
    run_id=current_run.id if current_run else None
    updated=[]
    try:
        with session.begin_nested():
            for item in payload.shots:
                shot=by_id[item.id];old_ratio=shot.aspect_ratio;was_approved=shot.status=='approved'
                changed=old_ratio!=payload.aspect_ratio
                conditions=_conditions()
                statement=update(m.StoryboardShot).where(m.StoryboardShot.id==item.id,
                    m.StoryboardShot.project_id==project_id,m.StoryboardShot.revision==item.expected_revision,
                    m.StoryboardShot.status==shot.status,m.StoryboardShot.status!='deleted',
                    m.StoryboardShot.aspect_ratio==old_ratio,stable_script,*[~value for value in conditions.values()])
                if changed:
                    statement=statement.values(aspect_ratio=payload.aspect_ratio,revision=m.StoryboardShot.revision+1,
                        status=case((m.StoryboardShot.status=='approved','draft'),else_=m.StoryboardShot.status),
                        approved_at=case((m.StoryboardShot.status=='approved',None),else_=m.StoryboardShot.approved_at))
                else:
                    # Compare-and-set even a no-op member, without changing its revision/timestamp.
                    statement=statement.values(revision=m.StoryboardShot.revision,updated_at=m.StoryboardShot.updated_at)
                result=session.execute(statement.execution_options(synchronize_session=False))
                if result.rowcount!=1:
                    raise AppError(409,'shot_framing_conflict','镜头、剧本或活动任务状态已变化，请刷新后重试；整批未保存。')
                if not changed:continue
                from app.api.routes import invalidate_shot_videos
                # Video approval is independent of storyboard approval. Every
                # actual framing change invalidates its existing usable videos.
                invalidated=invalidate_shot_videos(session,shot.id)
                # Preserve rejected history while preventing reject -> approve
                # from reviving an old framing, including context-less jobs.
                for video in session.scalars(select(m.VideoRevision).where(m.VideoRevision.shot_id==shot.id)):
                    video.metadata_json={**(video.metadata_json or {}),'framing_invalidated':{
                        'shot_revision':item.expected_revision+1,'before':old_ratio,'after':payload.aspect_ratio}}
                session.add(m.AuditEvent(object_type='shot',object_id=shot.id,action='aspect_ratio_changed',details_json={
                    'project_id':project_id,'run_id':run_id,'before':old_ratio,'after':payload.aspect_ratio,
                    'expected_revision':item.expected_revision,'revision':item.expected_revision+1,
                    'approval_invalidated':was_approved,'invalidated_videos':invalidated,
                    'original_files_preserved':True,'frozen_runs_preserved':True}))
                updated.append(shot.id)
            session.flush()
    except OperationalError as error:
        raise AppError(409,'shot_framing_conflict','镜头正在被其他操作更新，请刷新后重试；整批未保存。') from error
    session.expire_all()
    return {**overview(session,project_id),'updated_shot_ids':updated,
            'notice':'画幅已保存；请点击开始／增量续作创建新制作。已有冻结任务和原片保留，不能用继续旧编排替代。' if updated else '镜头画幅没有变化，未增加修订。'}

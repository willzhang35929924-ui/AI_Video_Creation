"""Explicitly apply saved quality to undispatched post-processing nodes only.

The HTTP caller owns the Studio project guard and commits this transaction.
This service never dispatches inference, rewrites accepted jobs, or deletes media.
"""
import copy
import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.orm.attributes import set_committed_value

from app import models as m
from app.core.errors import AppError
from app.schemas_quality import QualitySettings


PREFLIGHT_CODES = {
    'quality_route_processing_mismatch', 'quality_method_unverified',
    'quality_route_adapter_unavailable', 'quality_route_slot',
    'seedvr_endpoint_unverified', 'task_route_blocked', 'route_service_changed',
}
READY_STATES = {'pending', 'waiting_dependencies'}
PROTECTED_STATES = {'running', 'dispatching', 'submitting', 'submitted',
                    'unknown', 'succeeded', 'completed', 'cancelled'}


def _run_state_token(run):
    state = {name: getattr(run, name) for name in (
        'id', 'status', 'nodes_json', 'snapshot_json', 'result_json', 'error_message')}
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _same_route(left, right):
    def semantic(value):
        if isinstance(value, dict):
            return {key: semantic(item) for key, item in value.items() if key != 'captured_at'}
        if isinstance(value, list):
            return [semantic(item) for item in value]
        return value
    return semantic(left) == semantic(right)


def _quality_route(session, settings, project_id, shot_id, quality, options):
    if quality.processing == 'lanczos':
        # A nonempty per-shot entry shadows any inherited SeedVR2 snapshot.
        # This explicit operation selects ordinary FFmpeg, never an AI substitute.
        return {'slot': 'video_upscale', 'configured': False, 'selected': None,
                'profile': {'revision': 0, 'inherited_from': None,
                            'settings': {'parameters': {}}},
                'source': 'explicit_saved_lanczos_settings'}
    if quality.processing != 'seedvr2':
        raise AppError(409, 'quality_apply_adapter_unavailable',
                       '此处理方式尚无可明确应用的生产路由，未换用其他超分服务')
    from app.services.task_routing import freeze_route
    from app.services.video_quality import _route_for_quality
    route = freeze_route(session, settings, project_id, shot_id, 'video_upscale',
                         bool(options.get('allow_cloud_costs')),
                         bool(options.get('allow_paid_fallback')))
    _route_for_quality(session, route, quality)
    selected = route['selected']['service_snapshot']
    if selected.get('is_cloud'):
        raise AppError(409, 'quality_apply_cloud_not_supported',
                       '当前只接入已验证的本机 SeedVR2；未提交云端超分或扩大费用授权')
    if route.get('profile', {}).get('settings', {}).get('parameters'):
        raise AppError(422, 'quality_apply_parameters_unsupported',
                       '当前 SeedVR2 使用已验证固定参数，请移除未实现的额外超分参数')
    return route


def _raw_video(session, settings, nodes, shot):
    from app.services.studio_pipeline import _valid_video
    entry = next((n for n in nodes if n.get('kind') == 'video'
                  and n.get('target_id') == shot.id), None)
    if not entry or entry.get('status') != 'succeeded' or not entry.get('video_id'):
        raise AppError(409, 'quality_apply_source_not_ready',
                       '本镜原始视频尚未完成；未改写仍在生成的视频参数')
    video = session.get(m.VideoRevision, entry['video_id'])
    job = session.get(m.GenerationJob, video.generation_job_id) if video else None
    if (not video or video.project_id != shot.project_id or video.shot_id != shot.id
            or not job or job.project_id != shot.project_id or job.shot_id != shot.id
            or job.status not in {'generated', 'approved', 'revised'}):
        raise AppError(409, 'quality_apply_source_mismatch',
                       '已保存的视频不属于本镜已完成生成任务，未修改')
    _valid_video(session, settings, video)
    return video


def _downstream(nodes, identifier):
    descendants = set()
    while True:
        found = {n['id'] for n in nodes
                 if identifier in n.get('dependencies', [])
                 or descendants.intersection(n.get('dependencies', []))}
        if found <= descendants:
            return [n for n in nodes if n['id'] in descendants]
        descendants.update(found)


def apply_saved_quality(session, settings, project_id, expected_profile_revision=None):
    from app.services import video_quality as quality_service
    from app.services.screenplay_production_status import sync_production_status

    project = session.get(m.Project, project_id)
    if not project or project.status == 'deleted':
        raise AppError(404, 'project_not_found', '项目不存在')
    run = session.scalar(select(m.StudioRun).where(m.StudioRun.project_id == project_id)
                         .order_by(m.StudioRun.created_at.desc(), m.StudioRun.id.desc())
                         .limit(1).execution_options(populate_existing=True))
    if not run:
        raise AppError(409, 'quality_apply_run_missing', '项目尚无自动制作任务，请先开始制作')
    original_run_token = _run_state_token(run)
    original_run_state = {name: copy.deepcopy(getattr(run, name)) for name in (
        'status', 'nodes_json', 'snapshot_json', 'result_json', 'error_message')}
    if run.snapshot_json.get('product_references_changed'):
        raise AppError(409, 'studio_product_inputs_changed',
                       '产品参考已变化，请开始增量续作；未继续旧输入的画质处理')
    if run.status in {'completed', 'cancelled', 'waiting_review', 'rendering'} or run.result_json.get('render_id'):
        raise AppError(409, 'quality_apply_run_protected',
                       '此制作已完成、结束或进入成片渲染，请保留原快照并开始新的制作')
    own = quality_service.find_profile(session, 'project', project_id)
    revision = own.revision if own else 0
    if expected_profile_revision is not None and expected_profile_revision != revision:
        raise AppError(409, 'quality_profile_changed', '项目画质设置已变化，请刷新后再应用')
    project_settings = QualitySettings.model_validate(
        quality_service.inherited_settings(session, project_id) or {}).model_dump()
    snapshot = copy.deepcopy(run.snapshot_json)
    options = snapshot.setdefault('options', {})
    nodes = copy.deepcopy(run.nodes_json or [])
    proposals = []
    skipped = []
    seed_status = None
    sr_status = None
    for entry in nodes:
        if entry.get('kind') != 'quality':
            continue
        shot_id = entry.get('target_id')
        try:
            if shot_id not in snapshot.get('shot_ids', []):
                raise AppError(409, 'quality_apply_shot_scope',
                               '此镜头不在本轮冻结制作范围，未修改')
            if entry.get('child_id') or entry.get('status') in PROTECTED_STATES:
                raise AppError(409, 'quality_apply_child_protected',
                               '画质节点已有子任务或不可重排状态，保留原快照')
            if (entry.get('status') not in READY_STATES
                    and not (entry.get('status') in {'failed', 'exception'}
                             and entry.get('code') in PREFLIGHT_CODES)):
                raise AppError(409, 'quality_apply_not_preflight_failure',
                               '此异常不是可恢复的派发前画质配置错误，原任务保留')
            shot = session.get(m.StoryboardShot, shot_id)
            if not shot or shot.project_id != project_id or shot.status == 'deleted':
                raise AppError(409, 'quality_apply_shot_missing', '镜头已删除或不属于当前项目')
            descendants = _downstream(nodes, entry['id'])
            if any(n.get('kind') != 'quality_check' or n.get('target_id') != shot_id
                   for n in descendants):
                raise AppError(409, 'quality_apply_downstream_scope',
                               '画质之后存在其他业务或跨镜头依赖，未改写其任务')
            if any(n.get('child_id') or n.get('status') in PROTECTED_STATES for n in descendants):
                raise AppError(409, 'quality_apply_downstream_protected',
                               '后续任务已有结果、正在运行或结果未知，未替换其输入')
            if any(n.get('kind') in {'audio_mix', 'lip_sync'}
                   and (n.get('target_id') == shot_id or n.get('shot_id') == shot_id)
                   and n.get('status') in {'running', 'dispatching', 'submitting', 'submitted', 'unknown'}
                   for n in nodes):
                raise AppError(409, 'quality_apply_media_busy',
                               '本镜配音混合或口型任务尚在处理，保留其输入和画质快照')
            override = quality_service.find_profile(session, 'shot', shot_id)
            new_quality = QualitySettings.model_validate(
                override.settings_json if override else project_settings)
            if new_quality.aspect_ratio != shot.aspect_ratio:
                raise AppError(409, 'quality_apply_aspect_mismatch',
                               '保存的画质画幅与本镜不一致，请明确保存正确画幅，未拉伸或裁切')
            if new_quality.processing == 'seedvr2' and seed_status is None:
                seed_status = quality_service.seedvr_capabilities(session, settings)
            if new_quality.processing == 'ai_super_resolution' and sr_status is None:
                sr_status = quality_service._sr_status(settings)
            proposal = quality_service.plan(new_quality, sr_status, seed_status)
            if not proposal['supported']:
                raise AppError(409, 'quality_method_unverified',
                               '此分辨率与画幅组合尚无已验证处理能力，未自动降级')
            source = _raw_video(session, settings, nodes, shot)
            route = _quality_route(session, settings, project_id, shot_id, new_quality, options)
            current_route = (snapshot.get('routes_by_shot', {}).get(shot.id, {}).get('video_upscale')
                             or snapshot.get('routes', {}).get('video_upscale'))
            if (entry.get('status') in READY_STATES and not entry.get('code')
                    and options.get('quality_by_shot', {}).get(shot.id) == new_quality.model_dump()
                    and _same_route(current_route, route)):
                skipped.append({'shot_id': shot.id, 'code': 'quality_settings_already_current',
                                'reason': '未派发画质任务已使用此保存配置，无需重复重排'})
                continue
            proposals.append({'entry': entry, 'shot': shot, 'source': source,
                              'quality': new_quality.model_dump(), 'route': route,
                              'descendants': descendants})
        except AppError as exc:
            skipped.append({'shot_id': shot_id, 'code': exc.code, 'reason': exc.message})

    current_profile = session.scalar(select(m.QualityProfile).where(
        m.QualityProfile.scope == 'project', m.QualityProfile.target_id == project_id)
        .execution_options(populate_existing=True))
    if (current_profile.revision if current_profile else 0) != revision:
        raise AppError(409, 'quality_profile_changed', '应用期间项目画质设置已变化，请刷新后重试')
    # File/capability preflight may run while a thread-pool control request
    # pauses the run, changes products, or persists a newly accepted child.
    # Read the durable graph again, not this Session's earlier ORM snapshot.
    current_run = session.scalar(select(m.StudioRun).where(m.StudioRun.project_id == project_id)
        .order_by(m.StudioRun.created_at.desc(), m.StudioRun.id.desc()).limit(1)
        .execution_options(populate_existing=True))
    if not current_run or _run_state_token(current_run) != original_run_token:
        raise AppError(409, 'studio_quality_run_changed',
                       '应用期间自动制作状态或输入已变化；已保留最新暂停、产品与子任务记录，请刷新后重试')
    run = current_run
    changes = []
    for proposal in proposals:
        entry = proposal['entry']; shot = proposal['shot']; route = proposal['route']
        previous = {'settings': copy.deepcopy(options.get('quality_by_shot', {}).get(shot.id)),
                    'route': copy.deepcopy(snapshot.get('routes_by_shot', {}).get(shot.id, {}).get('video_upscale')
                                           or snapshot.get('routes', {}).get('video_upscale'))}
        options.setdefault('quality_by_shot', {})[shot.id] = proposal['quality']
        snapshot.setdefault('routes_by_shot', {}).setdefault(shot.id, {})['video_upscale'] = copy.deepcopy(route)
        for item in [entry, *proposal['descendants']]:
            item['history'] = [*(item.get('history') or []), {
                'action': 'explicit_saved_quality_applied',
                'previous': copy.deepcopy({k: v for k, v in item.items() if k != 'history'}),
                'project_profile_revision': revision}]
            item.update(status='pending', error=None, code=None)
            for name in ('video_id', 'source_video_id', 'evidence', 'retry_after'):
                item.pop(name, None)
        entry['route'] = copy.deepcopy(route)
        changes.append({'shot_id': shot.id, 'source_video_id': proposal['source'].id,
                        'previous': previous, 'settings': proposal['quality'], 'route': route})
    if proposals:
        options['quality'] = project_settings
        values = {'snapshot_json': snapshot, 'nodes_json': nodes,
                  'status': 'running' if run.status == 'completed_with_errors' else run.status,
                  'error_message': '；'.join(n.get('error') or '' for n in nodes
                      if n.get('status') in {'failed', 'exception', 'unknown'})[:3000] or None}
        statement = update(m.StudioRun).where(m.StudioRun.id == run.id)
        for name, expected in original_run_state.items():
            column = getattr(m.StudioRun, name)
            statement = statement.where(column.is_(None) if expected is None else column == expected)
        # Compare and swap in one DB write. A control request may still commit
        # after the final read above; never overwrite its pause/input/child state.
        with session.no_autoflush:
            written = session.execute(statement.values(**values).execution_options(synchronize_session=False))
        if written.rowcount != 1:
            raise AppError(409, 'studio_quality_run_changed',
                           '应用提交前自动制作状态已变化；已保留最新记录，请刷新后重试')
        # Avoid a later unconditional ORM flush repeating this graph update.
        for name, value in values.items():
            set_committed_value(run, name, value)
        sync_production_status(session, run)
        session.add(m.AuditEvent(object_type='studio_run', object_id=run.id,
            action='explicit_quality_applied_to_undispatched_nodes', details_json={
                'project_profile_revision': revision, 'changes': changes, 'skipped': skipped,
                'accepted_generation_jobs_unchanged': True, 'source_files_preserved': True,
                'allow_cloud_costs': options.get('allow_cloud_costs', False),
                'allow_paid_fallback': options.get('allow_paid_fallback', False)}))
        session.flush()
    return {'project_id': project_id, 'run_id': run.id, 'run_status': run.status,
            'project_profile_revision': revision,
            'applied': [proposal['shot'].id for proposal in proposals], 'skipped': skipped}

"""Unified workstation API; no UI progress is inferred from elapsed time."""
import copy
import asyncio
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import models as m
from app.models.studio import StudioRun
from app.core.db import get_db
from app.core.errors import AppError
from app.schemas_studio import (PipelineStart, StudioOverview, StudioProject,
    ShotFramingOverview,ShotFramingUpdate,ShotFramingResult)
from app.services.studio_pipeline import create_pipeline, latest_run, overview, project_list, _capture_services
from app.services.screenplay_production_status import sync_production_status

router = APIRouter(prefix='/api/studio')

@router.get('/projects/{project_id}/shot-framing',response_model=ShotFramingOverview)
def shot_framing(project_id:str,session:Session=Depends(get_db)):
    from app.services.shot_framing import overview as framing_overview
    return framing_overview(session,project_id)

@router.put('/projects/{project_id}/shot-framing',response_model=ShotFramingResult)
async def save_shot_framing(project_id:str,payload:ShotFramingUpdate,request:Request,session:Session=Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    from app.services.shot_framing import save as save_framing
    async with project_guard(request.app,project_id):
        try:
            session.expire_all()
            result=save_framing(session,project_id,payload)
            session.commit();return result
        except Exception:
            session.rollback();raise

@router.get('/projects/{project_id}/production-preferences')
def production_preferences(project_id:str,session:Session=Depends(get_db)):
    from app.services.studio_audio_policy import get_preferences
    return get_preferences(session,project_id)

from app.schemas_product_library import ProductionPreferenceEdit
@router.put('/projects/{project_id}/production-preferences')
def edit_production_preferences(project_id:str,payload:ProductionPreferenceEdit,session:Session=Depends(get_db)):
    from app.services.studio_audio_policy import save_preferences
    return save_preferences(session,project_id,payload)


@router.get('/projects', response_model=list[StudioProject])
def projects(session: Session = Depends(get_db)):
    return project_list(session)


@router.get('/projects/{project_id}', response_model=StudioOverview)
def project(project_id: str, session: Session = Depends(get_db)):
    return overview(session, project_id)


@router.post('/projects/{project_id}/start', response_model=StudioOverview, status_code=202)
async def start(project_id: str, payload: PipelineStart, request: Request, session: Session = Depends(get_db)):
    from app.services.studio_voice import assert_voice_preparation
    from app.services.studio_pipeline import RUN_TERMINAL
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app,project_id):
        session.expire_all()
        existing = latest_run(session, project_id)
        if not existing or existing.status in RUN_TERMINAL | {'paused', 'exceptions'}:
            if payload.dialogue_audio_policy is not None:
                from app.services.studio_audio_policy import get_preferences,save_preferences
                save_preferences(session,project_id,ProductionPreferenceEdit(expected_revision=get_preferences(session,project_id)['revision'],dialogue_audio_policy=payload.dialogue_audio_policy),commit=False)
            assert_voice_preparation(session, request.app.state.settings, project_id, payload.allow_cloud_costs, payload.allow_paid_fallback)
        create_pipeline(session, request.app.state.settings, project_id, payload)
        session.commit()
        return overview(session, project_id)


def _run(session, project_id):
    run = latest_run(session, project_id)
    if not run: raise AppError(409, 'studio_run_missing', '本项目尚无自动制作任务')
    return run


@router.post('/projects/{project_id}/pause', response_model=StudioOverview)
def pause(project_id: str, session: Session = Depends(get_db)):
    run = _run(session, project_id); nodes = copy.deepcopy(run.nodes_json)
    run.status = 'paused'
    for n in nodes:
        if n['kind'] == 'character' and n.get('child_id'):
            batch = session.get(m.CharacterBatch, n['child_id'])
            if batch: batch.paused = True
        if n['kind'] in {'scene', 'keyframe'} and n.get('child_id'):
            visual = session.get(m.ProductionVisual, n['child_id'])
            if visual and visual.status == 'queued': visual.status = 'studio_paused'
    session.add(m.AuditEvent(object_type='studio_run', object_id=run.id, action='paused_pending_dispatch', details_json={'accepted_provider_tasks_continue': True}))
    sync_production_status(session, run)
    session.commit(); return overview(session, project_id)


@router.post('/projects/{project_id}/resume', response_model=StudioOverview)
async def resume(project_id: str, request:Request, session: Session = Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    from app.services.shot_framing import requires_new_run
    async with project_guard(request.app,project_id):
        session.expire_all()
        run = _run(session, project_id)
        if run.status != 'paused': raise AppError(409, 'studio_not_paused', '只有已暂停的任务可以继续')
        if requires_new_run(session,project_id,run):
            raise AppError(409,'studio_framing_changed','镜头实际画幅已更改，请点击「开始／增量续作」创建新制作；原冻结编排和视频保留。',{'run_id':run.id})
        if run.snapshot_json.get('product_references_changed'):
            raise AppError(409,'studio_product_inputs_changed','暂停期间产品参考已变化，请点击「开始／增量续作」重新核对输入；旧视频保留，未继续旧输入')
        other = session.scalar(select(StudioRun).where(StudioRun.project_id == project_id, StudioRun.id != run.id, StudioRun.status.in_(['queued', 'running'])))
        if other: raise AppError(409, 'studio_already_active', '项目已有另一条执行中的任务')
        for n in run.nodes_json:
            if n['kind'] == 'character' and n.get('child_id'):
                batch = session.get(m.CharacterBatch, n['child_id'])
                if batch: batch.paused = False
            if n['kind'] in {'scene', 'keyframe'} and n.get('child_id'):
                visual = session.get(m.ProductionVisual, n['child_id'])
                if visual and visual.status == 'studio_paused': visual.status = 'queued'
        run.status = 'queued'; sync_production_status(session, run)
        session.commit(); return overview(session, project_id)


@router.post('/projects/{project_id}/retry', response_model=StudioOverview, name='retry')
async def retry_project(project_id: str, request: Request, payload: dict | None = None, session: Session = Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app, project_id) as nested:
        if not nested and not session.dirty and not session.new:
            session.expire_all()
        return retry(project_id, payload, session, settings=request.app.state.settings)


def retry(project_id: str, payload: dict | None = None, session: Session = Depends(get_db), *, settings=None):
    run = _run(session, project_id); nodes = copy.deepcopy(run.nodes_json)
    if run.snapshot_json.get('product_references_changed'):
        raise AppError(409,'studio_product_inputs_changed','产品参考已变化，请点击「开始／增量续作」创建使用新输入的制作')
    options = payload or {}
    if options.get('expected_run_id', run.id) != run.id:
        raise AppError(409, 'studio_retry_scope', '当前制作批次已变化，请刷新后重试')
    target = options.get('node_id')
    targets = options.get('node_ids')
    if target is not None and (not isinstance(target, str) or not target):
        raise AppError(422, 'studio_retry_scope', '请选择本轮的一个失败节点')
    if targets is not None and (target or not isinstance(targets, list) or not targets or len(targets) > len(nodes)
                                or not all(isinstance(value, str) for value in targets) or len(set(targets)) != len(targets)):
        raise AppError(422, 'studio_retry_scope', '请提供一组互不重复的本轮失败节点')
    targets = set(targets) if targets is not None else {target} if isinstance(target, str) and target else None
    selected = [n for n in nodes if (targets is None or n['id'] in targets) and n['status'] in {'exception', 'failed', 'unknown'}]
    if targets is not None and {n['id'] for n in selected} != targets:
        raise AppError(409, 'studio_retry_scope', '指定节点中有节点已经恢复或不在本轮失败列表')
    if any(n['status'] == 'unknown' for n in selected):
        raise AppError(409, 'studio_unknown_not_retryable', '含结果未知的请求；请先核对原服务任务，系统未重复提交以避免重复费用')
    refresh = options.get('refresh_context_for', [])
    if (not isinstance(refresh, list) or not all(isinstance(value, str) for value in refresh)
            or set(refresh) - {n['id'] for n in selected if n['kind'] == 'video' and n.get('code') != 'output_quality_failed'}):
        raise AppError(422, 'studio_retry_context_scope', '只可重新核对本次所选的失败视频输入')
    refreshed = {}
    if refresh:
        from app.services.context_production_inputs import collect_context_inputs

        if not settings or not run.snapshot_json.get('context_production'):
            raise AppError(409, 'studio_retry_context_unavailable', '仅本地 Context-IR 制作可以重新核对失败镜头的素材')
        by_shot = copy.deepcopy(run.snapshot_json.get('h3_context_by_shot', {}))
        for node in selected:
            if node['id'] not in refresh:
                continue
            shot = session.get(m.StoryboardShot, node['target_id'])
            frozen = by_shot.get(node['target_id'], {})
            before = frozen.get('inputs')
            if not shot or shot.project_id != project_id or not before or not frozen.get('service', {}).get('h3_workflow'):
                raise AppError(409, 'studio_retry_context_unavailable', '失败镜头缺少本项目的原始输入快照')
            if shot.revision != before.get('shot_revision'):
                raise AppError(409, 'studio_retry_context_changed', '分镜已经修改，请新建增量制作以应用新的内容')
            current = collect_context_inputs(session, settings, shot, prompt_override=before.get('prompt_override'),
                instruction=before.get('instruction'), image_ids=before.get('explicit_visual_ids'),
                motion_values=before.get('explicit_motion'), audio_policy=before.get('audio_policy'))
            if current.get('generation_inputs_hash') != before.get('generation_inputs_hash'):
                raise AppError(409, 'studio_retry_context_changed', '原分镜素材或制作内容已经修改，请使用增量制作')
            if current['fingerprint'] != before['fingerprint']:
                refreshed[node['id']] = {'previous': before['fingerprint'], 'current': current['fingerprint']}
                frozen['inputs'] = current
        run.snapshot_json = {**run.snapshot_json, 'h3_context_by_shot': by_shot}
    context_ids = set(run.snapshot_json.get('h3_context_by_shot', {}))
    if not run.snapshot_json.get('context_production') or any(
            shot_id not in context_ids for shot_id in run.snapshot_json.get('shot_ids', [])):
        snapshots, errors = _capture_services(session, project_id)
        changed_cloud = any(v['is_cloud'] for k, v in snapshots.items() if run.snapshot_json.get('services', {}).get(k, {}).get('service_id') != v['service_id'] or run.snapshot_json.get('services', {}).get(k, {}).get('service_revision') != v['service_revision'])
        if changed_cloud:
            raise AppError(409, 'studio_cloud_scope_changed', '云端服务或密钥设置已改变，请通过重新启动确认新服务的本轮费用范围')
        run.snapshot_json = {**run.snapshot_json, 'services': snapshots, 'service_errors': errors}
    for n in selected:
        # A known definitive failure may be retried; preserve its real child ID
        # in history, leaving other successful nodes and shot decisions intact.
        n['history'] = [*n['history'], {'child_id': n.get('child_id'), 'code': n.get('code'), 'error': n.get('error'), 'actor': 'local-user'}]
        # A paid cloud output failed locally: keep its ID for safe recheck.
        retained_child = n.get('child_id') if n.get('code') == 'output_quality_failed' else None
        n.pop('context_dispatch_wait', None)
        n.pop('retry_after', None)
        n.update(status='pending', child_id=retained_child, error=None, code=None)
    run.nodes_json = nodes; run.status = 'queued'; run.error_message = None
    session.add(m.AuditEvent(object_type='studio_run', object_id=run.id, action='failed_nodes_retry_requested',
                             details_json={'node_ids': [n['id'] for n in selected], 'refreshed_context': refreshed}))
    sync_production_status(session, run)
    session.commit(); return overview(session, project_id)


@router.post('/projects/{project_id}/repair')
async def repair(project_id: str, request: Request, session: Session = Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app, project_id) as nested:
        if not nested and not session.dirty and not session.new:
            session.expire_all()
        return await _repair(project_id, request, session)


@router.post('/projects/{project_id}/quality/apply')
async def apply_quality(project_id: str, request: Request, payload: dict | None = None, session: Session = Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    from app.services.studio_quality_apply import apply_saved_quality
    expected=(payload or {}).get('expected_profile_revision')
    if expected is not None and (type(expected) is not int or expected<0):
        raise AppError(422,'quality_profile_revision','画质修订号须为非负整数')
    async with project_guard(request.app,project_id):
        session.expire_all()
        result=apply_saved_quality(session,request.app.state.settings,project_id,expected_profile_revision=expected)
        session.commit();return result


async def _repair(project_id: str, request: Request, session: Session):
    """Diagnose a stuck run and resume only tasks with a known-safe next step."""
    run = _run(session, project_id)
    if run.snapshot_json.get('product_references_changed'):
        raise AppError(409,'studio_product_inputs_changed','产品参考已变化，请点击「开始／增量续作」重新核对输入；一键修复不会继续旧输入')
    from app.services.gpu_gate import owned_model_status
    from app.services.studio_pipeline import RUN_TERMINAL, advance_pipeline

    gpu = await owned_model_status()
    # Health checks yield while the worker or another request may persist a
    # child, pause, or input change. Never overwrite that newer durable state.
    session.expire_all()
    run = _run(session, project_id)
    if run.snapshot_json.get('product_references_changed'):
        raise AppError(409,'studio_product_inputs_changed','产品参考已变化，请点击「开始／增量续作」重新核对输入；一键修复不会继续旧输入')
    blockers = []
    actions = []
    queued_visual = None
    queued_node = None
    recoverable_characters = []
    nodes = copy.deepcopy(run.nodes_json or [])
    from app.services.reference_budget_repair import inspect_reference_budget_repairs
    recoverable_keyframes,keyframe_diagnostics,keyframe_blockers=inspect_reference_budget_repairs(
        session,request.app.state.settings,run,nodes)
    keyframe_candidates={item['id'] for item in recoverable_keyframes}
    keyframe_reported={item['node_id'] for item in keyframe_blockers}
    blockers.extend(keyframe_blockers)

    def character_capability_ready(item):
        """Match the current image adapter to the run's frozen route/snapshot.

        build_batch_plan accepts detected native ComfyUI node/model capability
        records. It does not accept arbitrary reachability as proof, so require
        one of those concrete capability records and the same model/service.
        """
        route = item.get('route') or {}
        selected = (route.get('selected') or {}).get('service_snapshot') or {}
        captured = selected or run.snapshot_json.get('services', {}).get('image') or {}
        service_id = captured.get('service_id')
        service = session.get(m.AIService, service_id) if service_id else None
        if (not service or not service.enabled or service.module != 'character_generation' or
                service.revision != captured.get('service_revision')):
            return False
        # This failure is raised by the native image adapter's model/workflow
        # preflight. Cloud adapters have a separate real-probe contract.
        if service.provider != 'comfyui':
            return False
        model_id = captured.get('model_id')
        if service.provider == 'comfyui':
            model_id = ((captured.get('settings') or {}).get('candidate_model') or
                        service.settings_json.get('candidate_model') or model_id or service.default_model)
        from app.models.character_reference import CharacterReferenceAsset
        has_project_references = session.scalar(select(CharacterReferenceAsset.id).where(
            CharacterReferenceAsset.project_id == project_id,
            CharacterReferenceAsset.character_id == item.get('target_id'),
            CharacterReferenceAsset.state == 'saved').limit(1)) is not None
        capability_name = 'reference_image' if has_project_references else 'text_to_image'
        capability = (service.capabilities_json or {}).get(capability_name) or {}
        if capability.get('state') not in {'detected', 'verified'}:
            return False
        capability_model = capability.get('model_id')
        if capability_model and capability_model != model_id:
            return False
        if has_project_references and model_id != 'flux-2-klein-4b-fp8.safetensors':
            return False
        return bool(model_id)

    for item in nodes:
        if item.get('status') in {'exception', 'failed', 'unknown'}:
            code = item.get('code') or 'task_failed'
            if item.get('id') in keyframe_candidates|keyframe_reported:
                continue
            if (item.get('kind') == 'character' and code == 'capability_unverified' and
                    item.get('status') in {'exception', 'failed'} and not item.get('child_id') and
                    character_capability_ready(item)):
                recoverable_characters.append(item)
                continue
            messages = {
                'capability_unverified': '图片模型或参考图工作流尚无匹配的真实输出验证；不会伪造验证结果。',
                'unknown': '服务请求结果未知；必须核对原服务任务，不能自动重提。',
            }
            blockers.append({'node_id': item.get('id'), 'kind': item.get('kind'),
                             'status': item.get('status'), 'code': code,
                             'message': messages.get(code, '存在明确失败节点；保留原记录，未自动重复提交。')})
        if item.get('kind') not in {'scene', 'keyframe'} or not item.get('child_id'):
            continue
        visual = session.get(m.ProductionVisual, item['child_id'])
        if visual and visual.status == 'queued' and queued_visual is None:
            queued_visual, queued_node = visual, item

    def dispatch_control(current):
        unchanged = not current.snapshot_json.get('product_references_changed')
        return (unchanged and current.status not in RUN_TERMINAL | {'paused', 'cancelled'},
                unchanged and current.status == 'completed_with_errors')

    run_can_dispatch, can_reopen = dispatch_control(run)
    if recoverable_keyframes:
        if not (run_can_dispatch or can_reopen) or gpu['state']!='idle':
            state='skipped' if not (run_can_dispatch or can_reopen) else 'waiting' if gpu['state']=='busy' else 'blocked'
            message='任务已暂停或结束，未越过运行控制。' if state=='skipped' else '参考图和原生链预检已通过；GPU尚未确认空闲，原节点保留，未提交生成。'
            actions.append({'code':'keyframe_reference_repair_waiting','status':state,'message':message,
                'node_ids':[item['id'] for item in recoverable_keyframes],'references':keyframe_diagnostics})
            blockers.extend({'node_id':item['id'],'kind':'keyframe','status':item['status'],'code':item['code'],'message':message} for item in recoverable_keyframes)
        else:
            for item in recoverable_keyframes:
                item['history']=[*(item.get('history') or []),{key:copy.deepcopy(item.get(key)) for key in ('status','code','error','child_id')}|
                    {'action':'one_click_reference_budget_repair'}]
                item.update(status='pending',error=None,code=None)
            run.nodes_json=nodes;run.status='running';run.error_message=None
            session.add(m.AuditEvent(object_type='studio_run',object_id=run.id,action='one_click_reference_budget_repair',
                details_json={'nodes':keyframe_diagnostics,'no_reference_removed':True,'human_visual_approval':False}))
            session.commit()
            worker_error=None
            try:
                await advance_pipeline(request.app,request.app.state.settings,session,run)
            except Exception as exc:worker_error=type(exc).__name__
            session.expire_all();run=_run(session,project_id)
            run_can_dispatch, can_reopen = dispatch_control(run)
            actions.append({'code':'keyframe_reference_nodes_requeued','status':'applied',
                'message':'已恢复参考预算误拦截的关键画面；全部人物、场景和产品原图保留。后台按原配置执行，实际输出解码后才记录多图验证。',
                'node_ids':[item['id'] for item in recoverable_keyframes],'references':keyframe_diagnostics,
                **({'worker_error':worker_error} if worker_error else {})})
            # The advance may have updated other nodes; do not overwrite them
            # with the diagnosis snapshot when repairing characters below.
            nodes=copy.deepcopy(run.nodes_json or [])
            recoverable_characters=[next((current for current in nodes if current['id']==old['id']),old) for old in recoverable_characters]
    if recoverable_characters and gpu['state'] != 'idle':
        actions.append({'code': 'character_repair_gpu_blocked',
                        'status': 'waiting' if gpu['state'] == 'busy' else 'blocked',
                        'message': '人物节点已有可用的本机模型/工作流能力记录；GPU 未确认空闲，保留原节点状态，稍后再次修复。',
                        'node_ids': [item['id'] for item in recoverable_characters]})
        for item in recoverable_characters:
            blockers.append({'node_id': item.get('id'), 'kind': item.get('kind'),
                             'status': item.get('status'), 'code': item.get('code'),
                             'message': 'GPU 尚未确认空闲，人物失败节点未重排。'})
        recoverable_characters = []
    if recoverable_characters and (run_can_dispatch or can_reopen):
        for item in recoverable_characters:
            item['history'] = [*(item.get('history') or []), {
                'status': item.get('status'), 'code': item.get('code'),
                'error': item.get('error'), 'child_id': item.get('child_id'),
                'action': 'one_click_repair_requeued'}]
            item.update(status='pending', error=None, code=None)
        run.nodes_json = nodes
        run.status = 'running'
        run.error_message = None
        session.add(m.AuditEvent(object_type='studio_run', object_id=run.id,
            action='one_click_repair_requeued_characters', details_json={
                'node_ids': [item['id'] for item in recoverable_characters],
                'gpu_gate': gpu['state'], 'gpu_reason': gpu['reason']}))
        session.commit()
        worker_error = None
        try:
            await advance_pipeline(request.app, request.app.state.settings, session, run)
        except Exception as exc:
            worker_error = type(exc).__name__
        session.expire_all()
        run = _run(session, project_id)
        run_can_dispatch, can_reopen = dispatch_control(run)
        if worker_error:
            actions.append({'code': 'character_repair_worker_error', 'status': 'error',
                            'message': '人物节点已重新排队；本轮 DAG 推进失败（'+worker_error+'），后台工作器将继续处理。',
                            'node_ids': [item['id'] for item in recoverable_characters]})
        else:
            actions.append({'code': 'character_nodes_requeued', 'status': 'applied',
                            'message': '已恢复具备当前服务能力的确定失败人物节点；旧错误保留在历史中，原 unknown 请求不会重提。',
                            'node_ids': [item['id'] for item in recoverable_characters]})
    elif recoverable_characters:
        for item in recoverable_characters:
            blockers.append({'node_id': item.get('id'), 'kind': item.get('kind'),
                             'status': item.get('status'), 'code': item.get('code'),
                             'message': '任务已结束且不是可恢复的 completed_with_errors 状态；人物节点未重排。'})
    if not queued_visual:
        actions.append({'code': 'no_queued_visual', 'status': 'not_needed',
                        'message': '没有可安全续派的已排队场景或关键帧任务。'})
    elif not run_can_dispatch:
        actions.append({'code': 'run_not_active', 'status': 'skipped',
                        'message': '任务当前已暂停或结束；保留队列，不越过运行控制。',
                        'visual_id': queued_visual.id})
    elif gpu['state'] == 'busy':
        actions.append({'code': 'gpu_in_use', 'status': 'waiting',
                        'message': '本地Qwen正占用GPU；保留原视觉队列，后台会在GPU空闲后继续。',
                        'visual_id': queued_visual.id})
    elif gpu['state'] == 'unknown':
        actions.append({'code': 'gpu_health_unknown', 'status': 'blocked',
                        'message': '本地模型进程仍无法核实；为避免两个模型同时抢占GPU，没有派发。',
                        'visual_id': queued_visual.id})
    elif queued_visual.snapshot_json.get('service_snapshot', {}).get('is_cloud') and not run.snapshot_json.get('options', {}).get('allow_cloud_costs'):
        actions.append({'code': 'cloud_cost_not_authorized', 'status': 'blocked',
                        'message': '此任务使用云端服务，但本轮记录没有云端费用授权；没有提交请求。',
                        'visual_id': queued_visual.id})
    else:
        # Only tick the selected durable child. The shared GPU lock and child
        # status make this safe alongside the normal worker; this never builds
        # a new task or retries a provider request with an unknown outcome.
        session.add(m.AuditEvent(object_type='studio_run', object_id=run.id,
            action='one_click_repair_requested', details_json={
                'visual_id': queued_visual.id, 'node_id': queued_node.get('id'),
                'gpu_gate': gpu['state'], 'gpu_reason': gpu['reason']}))
        session.commit()
        timed_out = False
        worker_error = None
        try:
            from app.services.cinematic_visuals import visual_tick
            await asyncio.wait_for(visual_tick(request.app.state.settings,
                request.app.state.session_factory, visual_id=queued_visual.id), timeout=20)
        except asyncio.TimeoutError:
            # The POST may have reached ComfyUI before the local wait expired.
            # Leave correlation/state to the worker and never auto-submit again.
            timed_out = True
        except Exception as exc:
            worker_error = type(exc).__name__
        session.expire_all()
        refreshed = session.get(m.ProductionVisual, queued_visual.id)
        current_status = refreshed.status if refreshed else 'missing'
        if worker_error:
            actions.append({'code': 'visual_repair_worker_error', 'status': 'error',
                            'message': '本地视觉工作器诊断失败（'+worker_error+'）；原任务仍保留，后台工作器会继续核对。',
                            'visual_id': queued_visual.id})
        elif timed_out or current_status in {'submitting', 'unknown'}:
            actions.append({'code': 'visual_submission_outcome_unknown', 'status': 'unknown',
                            'message': '提交核对超时或结果未知；保留原任务，禁止重复提交。',
                            'visual_id': queued_visual.id})
        elif current_status in {'submitted', 'running'}:
            actions.append({'code': 'visual_dispatched', 'status': 'applied',
                            'message': '已把已有视觉任务派发给本机ComfyUI；原任务ID与输入快照保留。',
                            'visual_id': queued_visual.id, 'provider_job_id': refreshed.provider_job_id})
        elif current_status == 'queued':
            actions.append({'code': 'visual_still_queued', 'status': 'waiting',
                            'message': '任务仍在本机队列；检查实际GPU/ComfyUI队列后由后台工作器继续，不会创建重复任务。',
                            'visual_id': queued_visual.id})
        else:
            actions.append({'code': 'visual_dispatch_failed', 'status': current_status,
                            'message': refreshed.error_message if refreshed else '视觉记录已不存在。',
                            'visual_id': queued_visual.id})

    session.add(m.AuditEvent(object_type='studio_run', object_id=run.id,
        action='one_click_repair_diagnosed', details_json={
            'gpu_gate': gpu['state'], 'gpu_reason': gpu['reason'],
            'action_codes': [item['code'] for item in actions],
            'blocker_codes': [item['code'] for item in blockers]}))
    session.commit()
    return {'project_id': project_id, 'run_id': run.id, 'run_status': run.status,
            'gpu_gate': gpu, 'actions': actions, 'blockers': blockers}

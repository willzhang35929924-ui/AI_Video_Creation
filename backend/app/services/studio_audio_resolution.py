"""User-selected duration repairs on the existing durable Studio DAG.

No inference is performed by these commands. They enqueue only the selected
shot's necessary nodes for the normal workers, retaining every source/child.
"""
from __future__ import annotations

import copy
import math

from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models.studio import StudioRun
from app.services.cinematic_rules import fingerprint
from app.services.runtime_registry import file_sha
from app.services.screenplay import get
from app.services.screenplay_production_status import sync_production_status

VERSION = 'studio-audio-duration-v1'
ACTIVE = {'queued', 'submitting', 'submitted', 'running', 'cancelling'}


def _error(code, message):
    raise AppError(409, code, message)


def _shot_nodes(run, shot_id):
    return [n for n in run.nodes_json if (n.get('shot_id') or n.get('target_id')) == shot_id]


def _descendants(nodes, roots):
    selected = set(roots)
    while True:
        more = {n['id'] for n in nodes if set(n.get('dependencies', [])) & selected}
        if more <= selected:
            return [n['id'] for n in nodes if n['id'] in selected]
        selected |= more


def _current(session, settings, project_id, node_id, expected_run_id=None):
    from app.services.media_processing import _probe
    from app.services.studio_pipeline import latest_run
    from app.services.cinematic_quality import generation_inputs_hash

    get(session, m.Project, project_id)
    run = latest_run(session, project_id)
    if not run or expected_run_id and run.id != expected_run_id:
        _error('audio_resolution_run_changed', '制作运行已变化，请刷新异常后重新选择')
    node = next((n for n in run.nodes_json if n['id'] == node_id), None)
    if not node or node['kind'] != 'audio_mix':
        _error('audio_resolution_node', '请选择当前制作内的对白混音异常')
    task = session.get(m.MediaProcessTask, node.get('child_id')) if node.get('child_id') else None
    if node.get('status') not in {'failed', 'exception'} or not task or task.status != 'failed':
        _error('audio_resolution_not_failed', '此混音不是可处理的已确认失败；运行中或结果未知时不会重复派发')
    if (node.get('code') != 'audio_longer_than_video'
            and task.result_json.get('error_code') != 'audio_longer_than_video'):
        _error('audio_resolution_wrong_error', '此操作仅处理对白音频超过原片长度的异常')
    shot = get(session, m.StoryboardShot, node['target_id'])
    if shot.project_id != project_id or shot.status == 'deleted':
        _error('audio_resolution_shot', '镜头不属于当前项目或已删除')
    sources = task.snapshot_json.get('sources', {})
    source = sources.get('video_path', {})
    video = get(session, m.VideoRevision, source.get('id'))
    if video.project_id != project_id or video.shot_id != shot.id:
        _error('audio_resolution_source_scope', '原片不属于当前镜头')
    video_path = safe_media_path(settings.media_root, video.relative_path, must_exist=True)
    digest = file_sha(video_path)
    if digest != source.get('sha256'):
        _error('audio_resolution_source_changed', '原片文件已变化，请重新核对素材')
    duration = float(_probe(video_path, settings)['duration'])
    if not math.isfinite(duration) or duration <= 0:
        _error('audio_resolution_duration', '原片没有有效的实际时长')

    dialogues = []
    tts_evidence = []
    cursor = 0.0
    for key, item in sources.items():
        if not key.startswith('audio_'):
            continue
        asset = session.get(m.AudioAsset, item.get('id'))
        tts_node = next((n for n in run.nodes_json if n.get('kind') == 'tts'
                         and n.get('audio_id') == item.get('id')), None)
        line_id = asset.dialogue_line_id if asset else None
        line_id = line_id or (tts_node or {}).get('target_id')
        line = session.get(m.DialogueLine, line_id) if line_id else None
        if not asset or asset.project_id != project_id or asset.shot_id != shot.id or not line or not tts_node:
            _error('audio_resolution_dialogue_source', '音频缺少明确的本镜对白来源，不能猜测替换对象')
        audio_path = safe_media_path(settings.media_root, asset.relative_path, must_exist=True)
        audio_sha = file_sha(audio_path)
        if audio_sha != item.get('sha256') or audio_sha != asset.sha256:
            _error('audio_resolution_source_changed', '配音文件已变化，请重新核对素材')
        actual_duration = float(_probe(audio_path, settings, True)['duration'])
        if not math.isfinite(actual_duration) or actual_duration <= 0:
            _error('audio_resolution_duration', '配音没有有效的实际时长')
        tts = get(session, m.MediaProcessTask, asset.task_id or tts_node.get('child_id'))
        if tts.status != 'succeeded' or tts.project_id != project_id or tts.shot_id != shot.id:
            _error('audio_resolution_tts_source', '原配音没有已确认完成的任务来源')
        actual_route = tts.snapshot_json.get('active_route') or tts.snapshot_json.get('route', {})
        selected = (actual_route.get('selected') or {}).get('service_snapshot', {})
        parameters = tts.snapshot_json.get('parameters', {})
        actual = (tts.result_json.get('runtime_receipt') or {}).get('actual', {})
        actual = actual or (asset.metadata_json.get('runtime_receipt') or {}).get('actual', {})
        family = selected.get('settings', {}).get('runtime_family')
        speed = actual.get('speed', parameters.get('speed', 1.0))
        language = actual.get('language', parameters.get('language', 'ZH' if family == 'indextts25' else None))
        start = float(item.get('metadata', {}).get('start_seconds', cursor))
        end = float(item.get('metadata', {}).get('end_seconds', start + actual_duration))
        cursor = end
        dialogues.append({'id': line.id, 'original_text': line.text,
            'text': tts.snapshot_json.get('inputs', {}).get('text', line.text),
            'audio_id': asset.id, 'sha256': audio_sha, 'duration_seconds': actual_duration,
            'start_seconds': start, 'end_seconds': end, 'speed': speed, 'language': language,
            'provider': selected.get('provider'), 'runtime_family': family,
            'tts_node_id': tts_node['id'], 'tts_task_id': tts.id})
        tts_evidence.append({'snapshot': tts.snapshot_json, 'actual': actual})
    if not dialogues:
        _error('audio_resolution_dialogue_source', '混音没有可修复的本镜配音')
    context = {'project_id': project_id, 'run_id': run.id, 'node_id': node_id,
        'shot': {'id': shot.id, 'title': shot.scene_title, 'order': shot.order, 'revision': shot.revision},
        'video': {'id': video.id, 'duration_seconds': duration, 'sha256': digest},
        'audio_duration_seconds': cursor, 'dialogues': dialogues}
    for dialogue in dialogues:
        dialogue['capabilities'] = {'adjust_speed': _speed_capability(context, dialogue)}
    decision = session.scalar(select(m.StudioDecision).where(
        m.StudioDecision.project_id == project_id, m.StudioDecision.shot_id == shot.id))
    context['source_fingerprint'] = fingerprint({'version': VERSION, **context, 'mix_task': task.id,
        'generation_inputs_hash': generation_inputs_hash(session, shot),
        'frozen_context': run.snapshot_json.get('h3_context_by_shot', {}).get(shot.id),
        'frozen_services': run.snapshot_json.get('services'),
        'frozen_routes': run.snapshot_json.get('routes'),
        'frozen_shot_routes': run.snapshot_json.get('routes_by_shot', {}).get(shot.id),
        'frozen_options': run.snapshot_json.get('options'), 'tts_evidence': tts_evidence,
        'decision': {key: getattr(decision, key) for key in (
            'id', 'action', 'video_id', 'revision', 'position', 'trim_start', 'trim_end', 'instruction')} if decision else None,
        'nodes': [{k: n.get(k) for k in ('id', 'status', 'child_id', 'audio_id', 'video_id',
                                         'tts_text_override', 'tts_parameters_override', 'tts_route_override')}
                  for n in _shot_nodes(run, shot.id)]})
    context['capabilities'] = _capabilities(context)
    return run, node, shot, context


def _speed_capability(context, line):
    supported = (line['provider'] == 'openai_tts' or
                 line['provider'] == 'local_runtime' and line['runtime_family'] in {'indextts25', 'cosyvoice3'})
    low, high = (.25, 4) if line['provider'] == 'openai_tts' else (.5, 2)
    video_duration = context['video']['duration_seconds']
    remaining = video_duration - (context['audio_duration_seconds'] - line['duration_seconds'])
    recommended = math.ceil(float(line['speed']) * line['duration_seconds'] / max(.01, remaining) * 1.1 * 20) / 20
    return {'available': supported, 'min': low if supported else None,
            'max': high if supported else None,
            'languages': ['ZH', 'EN', 'JA', 'ES', 'AR'] if line['runtime_family'] == 'indextts25' else [],
            'recommended_speed': min(high, recommended) if supported else None,
            **({'reason': '此配音适配器未声明可用语速参数，请选择其他处理方式'} if not supported else {})}


def _capabilities(context):
    return {'extend_video': {'available': True,
                'recommended_duration_seconds': math.ceil((context['audio_duration_seconds'] + .15) * 10) / 10},
            'shorten_dialogue': {'available': True},
            'adjust_speed': _speed_capability(context, context['dialogues'][0])}


def inspect(session, settings, project_id, node_id):
    return _current(session, settings, project_id, node_id)[3]


def _plan(session, settings, project_id, node_id, payload):
    run, node, shot, context = _current(session, settings, project_id, node_id, payload.expected_run_id)
    if context['source_fingerprint'] != payload.source_fingerprint:
        _error('audio_resolution_stale', '原片、配音或任务状态已变化，请刷新后重新预览')
    from app.services.cinematic_quality import generation_inputs_hash
    source_video = get(session, m.VideoRevision, context['video']['id'])
    source_job = get(session, m.GenerationJob, source_video.generation_job_id)
    original_hash = (source_job.parameters_json.get('screenplay_snapshot', {}).get('cinematic', {})
                     .get('preflight', {}).get('generation_inputs_hash'))
    if not original_hash or original_hash != generation_inputs_hash(session, shot):
        _error('audio_resolution_source_stale', '镜头资料与原片冻结输入不一致，请明确新建制作；音频处理不会悄悄使用修改后的剧本或素材')
    parameters, warnings = {}, ['原片、原音频和历史任务完整保留；新结果仍需人工审核。']
    if payload.action == 'extend_video':
        target = payload.target_duration_seconds
        if target is None or target < context['audio_duration_seconds'] or target <= context['video']['duration_seconds']:
            raise AppError(422, 'audio_resolution_target_duration', '新镜头时长必须长于原片且容纳全部配音')
        context_shot = run.snapshot_json.get('h3_context_by_shot', {}).get(shot.id)
        if context_shot:
            from app.services.context_production_inputs import restore_context_inputs
            # Validate before changing duration/revision. Collecting a new
            # snapshot alone would silently absorb changed identity/SKU inputs.
            restore_context_inputs(session, settings, shot, context_shot['inputs'])
        selected = ((run.snapshot_json.get('routes_by_shot', {}).get(shot.id, {}).get('video_generation', {}).get('selected')
                    or run.snapshot_json.get('routes', {}).get('video_generation', {}).get('selected') or {})
                    .get('service_snapshot') or run.snapshot_json.get('services', {}).get('video', {}))
        contract = selected.get('settings', {}).get('video_contract')
        if contract in {'lk888-h3-v2', 'wan26-i2v'}:
            lower = 4 if contract == 'lk888-h3-v2' else 2
            if target != int(target) or not lower <= target <= 15:
                raise AppError(422, 'audio_resolution_target_duration', f'当前视频接口要求 {lower}～15 秒整数，请调整时长')
        parameters = {'target_duration_seconds': target}
        roots = ['video:' + shot.id]
        summary = f'仅重新生成本镜 {target:g} 秒视频，再执行混音和后处理；保留已完成配音。'
        warnings.append('延长镜头会提交新的真实视频任务；沿用本轮已确认的服务和费用授权。')
    else:
        line = next((line for line in context['dialogues'] if line['id'] == payload.dialogue_line_id), None)
        if not line:
            raise AppError(422, 'audio_resolution_line', '请选择本次异常里的具体对白')
        source_tts = get(session, m.MediaProcessTask, line['tts_task_id'])
        actual_route = source_tts.snapshot_json.get('active_route') or source_tts.snapshot_json.get('route', {})
        selected = (actual_route.get('selected') or {}).get('service_snapshot', {})
        if (selected.get('is_cloud') or selected.get('provider') == 'openai_tts') and not run.snapshot_json.get('options', {}).get('allow_cloud_costs'):
            raise AppError(403, 'audio_resolution_cloud_costs', '此配音实际使用云端服务，本轮尚未授权新的云端生成费用')
        parameters = {'dialogue_line_id': line['id']}
        if payload.action == 'shorten_dialogue':
            text = (payload.text or '').strip()
            if not text or text == line['text'].strip():
                raise AppError(422, 'audio_resolution_shorter_text', '请填写与当前实际配音不同的有效台词；时长以新配音实测为准')
            parameters['text'] = text
            summary = '仅用新台词重新生成本句配音，再执行混音和后处理；原剧本保留。'
            warnings.append('时长须以重新生成的音频实测为准；仍超长会再次提示选择，不会截断声音。')
        else:
            capability = line['capabilities']['adjust_speed']
            speed = payload.speed
            if not capability['available']:
                raise AppError(422, 'audio_resolution_speed_unsupported', capability['reason'])
            if speed is None or not capability['min'] <= speed <= capability['max']:
                raise AppError(422, 'audio_resolution_speed', '语速超出当前配音适配器的明确支持范围')
            parameters['speed'] = speed
            summary = f'仅以 {speed:g} 倍语速重新生成本句配音，再执行混音和后处理。'
            warnings.append('模型语速与时长并非精确比例；仍超长会再次提示选择，不会截断声音。')
        if payload.language is not None:
            if line['runtime_family'] != 'indextts25' or payload.language not in {'ZH', 'EN', 'JA', 'ES', 'AR'}:
                raise AppError(422, 'audio_resolution_language', '此适配器不支持所选语言参数')
            parameters['language'] = payload.language
        elif line['runtime_family'] == 'indextts25' and line['language'] == 'ZH' and any('a' <= c.lower() <= 'z' for c in line['text']):
            warnings.append('当前实际语言为 ZH；英文台词建议明确选择 EN，系统不会自动改写语言。')
        roots = [line['tts_node_id']]
    affected = _descendants(run.nodes_json, roots)
    nodes = [n for n in run.nodes_json if n['id'] in affected]
    if any((n.get('shot_id') or n.get('target_id')) != shot.id for n in nodes):
        _error('audio_resolution_graph_scope', '处理链包含其他镜头，未越权修改')
    _assert_idle(session, run, shot, nodes)
    result = {'action': payload.action, 'summary': summary, 'affected_nodes': affected,
              'warnings': warnings, 'parameters': parameters}
    result['preview_token'] = fingerprint({'version': VERSION, 'source': context['source_fingerprint'],
                                          'plan': result, 'client_request_id': payload.client_request_id})
    return run, node, shot, context, result


def _assert_idle(session, run, shot, affected):
    for n in affected:
        if n.get('status') in {'unknown', 'dispatching', 'running'}:
            _error('audio_resolution_busy', '本镜仍有运行中或结果未知的任务，请先等待或核实原任务')
        model = (m.GenerationJob if n['kind'] == 'video' else m.QualityRender if n['kind'] == 'quality'
                 else m.MediaProcessTask if n['kind'] in {'tts', 'audio_mix', 'lip_sync'} else None)
        child = session.get(model, n['child_id']) if model and n.get('child_id') else None
        if child and child.status in ACTIVE | {'unknown'}:
            _error('audio_resolution_busy', '本镜子任务仍在运行或结果未知，没有重新提交')
    for model in (m.GenerationJob, m.MediaProcessTask):
        query = select(model.id).where(model.shot_id == shot.id, model.status.in_(list(ACTIVE | {'unknown'})))
        if session.scalar(query.limit(1)):
            _error('audio_resolution_busy', '本镜另有尚未结束的任务，请等待原任务完成')
    quality_query = select(m.QualityRender.id).join(m.VideoRevision,
        m.QualityRender.source_video_id == m.VideoRevision.id).where(
        m.VideoRevision.shot_id == shot.id, m.QualityRender.status.in_(list(ACTIVE | {'unknown'})))
    if session.scalar(quality_query.limit(1)):
        _error('audio_resolution_busy', '本镜画质任务尚未结束，请等待原任务完成')


def preview(session, settings, project_id, node_id, payload):
    return _plan(session, settings, project_id, node_id, payload)[4]


def apply(session, settings, project_id, node_id, payload):
    # The route owns the project guard and DB write transaction. A durable receipt
    # is checked before freshness so a network retry cannot enqueue twice.
    run = session.get(StudioRun, payload.expected_run_id)
    if not run or run.project_id != project_id:
        _error('audio_resolution_run_changed', '制作运行不属于当前项目')
    command = payload.model_dump(exclude={'preview_token'})
    command_hash = fingerprint({'node_id': node_id, **command})
    receipts = copy.deepcopy(run.result_json.get('audio_duration_resolutions', {}))
    previous = receipts.get(payload.client_request_id)
    if previous:
        if previous['command_hash'] != command_hash:
            _error('audio_resolution_request_reused', '此请求编号已经用于其他修改，请重新预览')
        return {**previous['result'], 'replayed': True}
    run, mix_node, shot, context, plan = _plan(session, settings, project_id, node_id, payload)
    if payload.preview_token != plan['preview_token']:
        _error('audio_resolution_preview_changed', '处理预览已变化，请重新预览后确认')
    nodes = copy.deepcopy(run.nodes_json)
    snapshot_before = copy.deepcopy(run.snapshot_json)
    affected = set(plan['affected_nodes'])
    parameters = plan['parameters']
    if payload.action == 'extend_video':
        original_duration, original_revision = shot.duration_seconds, shot.revision
        shot.duration_seconds = parameters['target_duration_seconds']
        shot.revision += 1
        session.flush()
        frozen = copy.deepcopy(run.snapshot_json)
        context_shot = frozen.get('h3_context_by_shot', {}).get(shot.id)
        if context_shot:
            from app.services.context_production_inputs import collect_context_inputs
            old = context_shot['inputs']
            context_shot['inputs'] = collect_context_inputs(session, settings, shot,
                prompt_override=old.get('prompt_override'), instruction=old.get('instruction'),
                image_ids=old.get('explicit_visual_ids'), motion_values=old.get('explicit_motion'),
                audio_policy=old.get('audio_policy'))
        options = frozen.setdefault('options', {})
        options['force_shot_ids'] = sorted(set(options.get('force_shot_ids', [])) | {shot.id})
        run.snapshot_json = frozen
        lineage_change = {'duration_before': original_duration, 'duration_after': shot.duration_seconds,
                          'revision_before': original_revision, 'revision_after': shot.revision}
    else:
        line = next(line for line in context['dialogues'] if line['id'] == parameters['dialogue_line_id'])
        tts = next(n for n in nodes if n['id'] == line['tts_node_id'])
        source_tts = get(session, m.MediaProcessTask, line['tts_task_id'])
        actual_route = source_tts.snapshot_json.get('active_route') or source_tts.snapshot_json.get('route', {})
        if source_tts.snapshot_json.get('active_route'):
            # A previous fallback can have a different adapter. Freeze that
            # already-authorized real service for this node only; a language or
            # speed chosen for it must not be sent to the original primary.
            tts['tts_route_override'] = copy.deepcopy(actual_route)
            tts['tts_parameters_override'] = {**source_tts.snapshot_json.get('parameters', {}),
                                              **tts.get('tts_parameters_override', {})}
        if payload.action == 'shorten_dialogue':
            tts['tts_text_override'] = parameters['text']
        overrides = {**tts.get('tts_parameters_override', {}),
                     **{k: parameters[k] for k in ('speed', 'language') if k in parameters}}
        if overrides:
            tts['tts_parameters_override'] = overrides
        tts['audio_resolution_lineage'] = {'source_video': context['video'], 'original_text': line['original_text'],
            'previous_effective_text': line['text'], 'effective_text': tts.get('tts_text_override', line['original_text']),
            'choice': payload.action, 'parameters': parameters, 'request_id': payload.client_request_id}
        if tts.get('tts_route_override'):
            tts['audio_resolution_lineage']['actual_route'] = copy.deepcopy(tts['tts_route_override'])
        lineage_change = copy.deepcopy(tts['audio_resolution_lineage'])
    for entry in nodes:
        if entry['id'] not in affected:
            continue
        entry['history'] = [*entry.get('history', []), {'reason': 'audio_duration_resolution',
            'request_id': payload.client_request_id, 'action': payload.action,
            'previous': copy.deepcopy({k: v for k, v in entry.items() if k != 'history'})}]
        for key in ('video_id', 'audio_id', 'source_video_id', 'retry_after', 'context_dispatch_wait', 'reused'):
            entry.pop(key, None)
        entry.update(status='pending', child_id=None, attempts=0, error=None, code=None)
    run.nodes_json = nodes
    run.status = 'queued'
    run.error_message = None
    decision = session.scalar(select(m.StudioDecision).where(
        m.StudioDecision.project_id == project_id, m.StudioDecision.shot_id == shot.id))
    prior_decision = None
    if decision:
        prior_decision = {key: getattr(decision, key) for key in (
            'id', 'action', 'video_id', 'revision', 'position', 'trim_start', 'trim_end', 'instruction')}
        decision.action, decision.video_id = 'pending', None
        decision.trim_start, decision.trim_end = 0, None
        decision.instruction = ''
        decision.revision += 1
    # An old render remains a history item; no stale render can claim completion
    # of this new per-shot processing request.
    result_json = copy.deepcopy(run.result_json)
    render = {k: result_json.pop(k) for k in ('render_id', 'assembly_id', 'relative_path', 'automatic_draft') if k in result_json}
    if render:
        result_json['previous_renders'] = [*result_json.get('previous_renders', []), render]
    result = {'run_id': run.id, 'node_id': node_id, 'status': run.status,
        'affected_nodes': plan['affected_nodes'], 'replayed': False,
        'notice': '已提交所选处理，程序将接续本镜任务；原片和原音频保留。'}
    receipts[payload.client_request_id] = {'command_hash': command_hash, 'result': result,
        'source': context, 'plan': plan, 'change': lineage_change, 'snapshot_before': snapshot_before,
        'previous_decision': prior_decision}
    run.result_json = {**result_json, 'audio_duration_resolutions': receipts}
    session.add(m.AuditEvent(object_type='studio_run', object_id=run.id, action='audio_duration_resolution_applied',
        details_json={'request_id': payload.client_request_id, 'node_id': node_id, 'choice': payload.action,
                      'source_fingerprint': context['source_fingerprint'], 'affected_nodes': plan['affected_nodes'],
                      'source_video': context['video'], 'change': lineage_change, 'parameters': parameters,
                      'previous_decision': prior_decision}))
    sync_production_status(session, run)
    session.flush()
    return result

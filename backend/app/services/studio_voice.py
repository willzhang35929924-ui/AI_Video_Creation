"""Prepare character voice references before creating any model task.

References are applied only to this project's current script membership and the
explicit identity version. Existing provider task snapshots remain immutable.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.services.screenplay import get
from app.services.task_routing import freeze_route, route_is_explicit
from app.services.runtime_registry import REFERENCE_TTS_FAMILIES

AUDIO_SUFFIXES = {'.wav', '.flac', '.mp3', '.m4a', '.aac', '.ogg'}
ACTIVE_RUNS = {'queued', 'running', 'rendering'}
ACTIVE_CHILDREN = {'queued', 'running','postprocessing', 'submitting', 'submitted', 'cancelling', 'unknown'}


def _current(session, project_id):
    from app.api.routes import project_shot_statement
    get(session, m.Project, project_id)
    shots = list(session.scalars(project_shot_statement(session, project_id).order_by(m.StoryboardShot.order, m.StoryboardShot.id)))
    shot_ids = [s.id for s in shots]
    people = list(session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id.in_(shot_ids))
                                 .order_by(m.ShotCharacter.character_id, m.ShotCharacter.character_version_id, m.ShotCharacter.id))) if shot_ids else []
    lines = list(session.scalars(select(m.DialogueLine).where(m.DialogueLine.shot_id.in_(shot_ids)))) if shot_ids else []
    return shots, people, lines


def _token(project_id, people, shots):
    snapshot = {'project_id': project_id, 'participants': [
        {'id': p.id, 'shot_id': p.shot_id, 'character_id': p.character_id,
         'version_id': p.character_version_id, 'performance': p.performance_json,
         'shot_revision': shots[p.shot_id].revision} for p in people]}
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _audio_error(session, settings, project_id, version_id, audio_id, require_authorization=True):
    if not audio_id:
        return '尚未绑定参考音频。'
    asset = session.get(m.AudioAsset, audio_id)
    if not asset or asset.project_id != project_id or asset.character_version_id != version_id:
        return '参考音频不属于当前项目与人物版本，请重新选择。'
    try:
        path = safe_media_path(settings.media_root, asset.relative_path, must_exist=True)
        if path.suffix.lower() not in AUDIO_SUFFIXES or not 0 < path.stat().st_size <= 100 * 1024 * 1024:
            return '参考文件不是受支持的音频，或超过上传大小范围。'
        registry = session.scalar(select(m.ManagedFile).where(m.ManagedFile.relative_path == asset.relative_path))
        if registry and registry.state != 'active':
            return '参考音频正在删除或已失效，请重新选择。'
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != asset.sha256:
            return '参考音频文件已变化，SHA256 与已上传版本不符。'
        actual = (asset.metadata_json or {}).get('actual', {})
        duration = float(actual.get('duration', -1))
        if actual.get('full_decode') is not True or not math.isfinite(duration) or not .1 <= duration <= 30:
            return '参考音频缺少有效解码记录，或时长不在0.1至30秒内，请通过上传入口重新校验。'
        if require_authorization:
            from app.services.voice_library import assert_reference_authorized
            try:
                assert_reference_authorized(session, asset)
            except AppError as error:
                return error.message
    except (AppError, OSError, TypeError, ValueError):
        return '参考音频文件不可读，请重新上传。'
    return None


def voice_preparation(session, settings, project_id, allow_cloud_costs=False, allow_paid_fallback=False):
    try:
        from app.services.studio_audio_policy import get_audio_policy
        audio_policy = get_audio_policy(session, project_id)
    except ImportError:
        audio_policy = 'auto'
    shots, people, lines = _current(session, project_id)
    shot_map = {s.id: s for s in shots}
    dialogue_by_person = defaultdict(list)
    for line in lines:
        if line.text.strip():
            dialogue_by_person[line.participant_id].append(line)
    speaking_shots = {line.shot_id for values in dialogue_by_person.values() for line in values}
    routes, services, blockers = {}, [], []
    for shot in shots:
        if shot.id not in speaking_shots:
            continue
        route = freeze_route(session, settings, project_id, shot.id, 'dialogue_generation', allow_cloud_costs, allow_paid_fallback)
        routes[shot.id] = route
        selected = (route.get('selected') or {}).get('service_snapshot') or {}
        enabled = bool(selected) and audio_policy not in {'native', 'skip'}
        required = selected.get('provider') == 'local_runtime' and selected.get('settings', {}).get('runtime_family') in REFERENCE_TTS_FAMILIES
        explicit_blocked = not enabled and audio_policy not in {'native', 'skip'}
        issues = ([] if enabled else ['本镜已指定配音策略，但没有满足能力与调用许可的服务，请检查系统设置。']
                  if explicit_blocked else ['用户选择沿用视频模型原声。' if audio_policy == 'native' else '用户明确选择跳过独立配音。'])
        if explicit_blocked:
            blockers.append(f'镜头 {shot.order}：{issues[0]}')
        services.append({'shot_id': shot.id, 'enabled': enabled, 'service_id': selected.get('service_id'),
                         'name': selected.get('name') or '沿用视频原声', 'provider': selected.get('provider'),
                         'model_id': selected.get('model_id'), 'requires_reference_audio': required, 'issues': issues})
    by_identity = defaultdict(list)
    for person in people:
        by_identity[(person.character_id, person.character_version_id)].append(person)
    groups, audio_errors = [], {}
    for (character_id, version_id), members in by_identity.items():
        character = session.get(m.Character, character_id)
        version = session.get(m.CharacterVersion, version_id) if version_id else None
        audio_ids = {p.performance_json.get('reference_audio_asset_id') or None for p in members}
        mixed = len(audio_ids) > 1
        common_audio = next(iter(audio_ids)) if len(audio_ids) == 1 else None
        speaking = [p for p in members if dialogue_by_person[p.id]]
        chosen = [(p, (routes[p.shot_id].get('selected') or {}).get('service_snapshot') or {}) for p in speaking] if audio_policy not in {'native', 'skip'} else []
        tts_enabled = any(bool(service) for _, service in chosen)
        requires_reference = any(service.get('provider') == 'local_runtime' and service.get('settings', {}).get('runtime_family') in REFERENCE_TTS_FAMILIES
                                 for _, service in chosen)
        issues, required_issues = [], []
        if speaking and not tts_enabled and audio_policy not in {'native', 'skip'}:
            required_issues.append('有对白但尚未配置可用的独立配音服务；请选择配音服务、视频原声或明确跳过。')
        if not version or version.character_id != character_id:
            issues.append('先为此人物选择身份版本，再上传参考音频。')
            if tts_enabled:
                required_issues.append(issues[-1])
        if mixed:
            issues.append('同一人物版本在不同镜头使用不同参考音频；保存后会统一当前剧本中的这些镜头。')
        for person, service in chosen:
            if service.get('provider') == 'local_runtime' and service.get('settings', {}).get('runtime_family') in REFERENCE_TTS_FAMILIES:
                audio_id = person.performance_json.get('reference_audio_asset_id')
                key = (version_id, audio_id)
                if key not in audio_errors:
                    audio_errors[key] = _audio_error(session, settings, project_id, version_id, audio_id)
                issue = audio_errors[key]
                if issue:
                    required_issues.append(issue)
            elif service.get('provider') == 'openai_tts':
                voice = routes[person.shot_id].get('profile', {}).get('settings', {}).get('parameters', {}).get('voice') or person.performance_json.get('voice_profile')
                if not isinstance(voice, str) or not voice.strip():
                    required_issues.append('云端配音还需为人物设置供应商音色ID；上传参考音频不会自动启用声音克隆。')
        issues = list(dict.fromkeys(issues + required_issues))
        ready = not required_issues
        name = character.display_name if character else '人物已删除'
        if not ready:
            blockers.extend(f'{name}：{issue}' for issue in dict.fromkeys(required_issues))
        groups.append({'character_id': character_id, 'name': name, 'version_id': version_id,
                       'version_number': version.version if version else None, 'version_state': version.approval_state if version else 'missing',
                       'participant_ids': [p.id for p in members], 'shot_ids': list(dict.fromkeys(p.shot_id for p in members)),
                       'dialogue_count': sum(len(dialogue_by_person[p.id]) for p in members), 'audio_id': common_audio,
                       'mixed': mixed, 'expected_token': _token(project_id, members, shot_map), 'tts_enabled': tts_enabled,
                       'requires_reference_audio': requires_reference, 'ready': ready, 'issues': issues,
                       **_voice_status(session, project_id, members, speaking, common_audio, audio_policy, tts_enabled, ready, routes)})
    enabled = any(s['enabled'] for s in services)
    return {'project_id': project_id, 'groups': groups, 'services': services, 'tts_enabled': enabled,
            'ready_to_start': not blockers, 'blockers': list(dict.fromkeys(blockers)),
            'audio_policy': audio_policy,
            'summary': ('角色配音准备尚未完成，请处理下方提示后开始制作。' if blockers else
                        '角色配音设置已准备，可开始制作。' if enabled else
                        '已选择沿用视频模型原声。' if audio_policy == 'native' else
                        '已明确选择跳过独立配音。' if audio_policy == 'skip' else '当前剧本没有需要独立配音的对白。')}


def _voice_status(session, project_id, members, speaking, common_audio, policy, enabled, ready, routes):
    audio = session.get(m.AudioAsset, common_audio) if common_audio else None
    library_id = (audio.metadata_json or {}).get('voice_library_asset_id') if audio else None
    configuration = ('not_required' if not speaking else 'native' if policy == 'native' else
                     'skipped' if policy == 'skip' else 'unconfigured' if not enabled else 'ready' if ready else 'blocked')
    generation = {'ready': 'pending'}.get(configuration, configuration)
    if configuration == 'ready':
        participant_ids = {member.id for member in members}
        # Only current selected-reference outputs count; earlier voice tasks remain history.
        current = []
        for task in session.scalars(select(m.MediaProcessTask).where(m.MediaProcessTask.kind == 'tts', m.MediaProcessTask.project_id == project_id)
                                    .order_by(m.MediaProcessTask.created_at.desc())):
            if task.snapshot_json.get('participant_id') not in participant_ids:
                continue
            if ((task.snapshot_json.get('sources', {}).get('reference_audio_path') or {}).get('id') or None) != common_audio:
                continue
            route = routes.get(task.shot_id, {})
            selected = (route.get('selected') or {}).get('service_snapshot') or {}
            previous = (task.snapshot_json.get('route', {}).get('selected') or {}).get('service_snapshot') or {}
            if any(previous.get(key) != selected.get(key) for key in ('service_id', 'service_revision', 'model_id')):
                continue
            if any(task.snapshot_json.get('parameters', {}).get(key) != value for key, value in route.get('profile', {}).get('settings', {}).get('parameters', {}).items()):
                continue
            current.append(task)
        if current:
            expected = {line.id: line.text for line in session.scalars(select(m.DialogueLine).where(m.DialogueLine.participant_id.in_(participant_ids))) if line.text.strip()}
            latest = {}
            for task in current:
                line_id = task.snapshot_json.get('dialogue_line_id')
                if line_id in expected and task.snapshot_json.get('inputs', {}).get('text') == expected[line_id]:
                    latest.setdefault(line_id, task)
            states = {task.status for task in latest.values()}
            if len(latest) < len(expected):
                states.add('queued')
            generation = ('generating' if states & {'running', 'submitting', 'submitted'} else
                          'pending' if 'queued' in states else 'failed' if 'failed' in states else
                          'blocked' if states & {'blocked', 'unknown', 'cancelled'} else
                          'succeeded' if states == {'succeeded'} else 'pending')
    return {'library_asset_id': library_id, 'configuration_status': configuration, 'generation_status': generation}


def assert_voice_preparation(session, settings, project_id, allow_cloud_costs=False, allow_paid_fallback=False):
    result = voice_preparation(session, settings, project_id, allow_cloud_costs, allow_paid_fallback)
    if not result['ready_to_start']:
        raise AppError(409, 'studio_voice_preparation_required', '请先完成项目角色配音设置，再开始制作', {'blockers': result['blockers']})
    return result


def save_voice(session, settings, project_id, character_id, payload, allow_cloud_costs=False, allow_paid_fallback=False):
    shots, people, _ = _current(session, project_id)
    targets = [p for p in people if p.character_id == character_id and p.character_version_id == payload.version_id]
    version = session.get(m.CharacterVersion, payload.version_id)
    if not targets or not version or version.character_id != character_id:
        raise AppError(422, 'studio_voice_version_scope', '所选人物版本不属于当前项目的当前剧本，未修改身份绑定')
    shot_map = {s.id: s for s in shots}
    if payload.expected_token != _token(project_id, targets, shot_map):
        raise AppError(409, 'studio_voice_changed', '人物配音设置或镜头已在其他窗口变化，请刷新后保存')
    if payload.audio_id:
        issue = _audio_error(session, settings, project_id, payload.version_id, payload.audio_id, require_authorization=False)
        if issue:
            raise AppError(422, 'studio_voice_audio_invalid', issue)
    changed = [p for p in targets if (p.performance_json.get('reference_audio_asset_id') or None) != payload.audio_id]
    changed_shots = sorted({p.shot_id for p in changed})
    if changed:
        active = session.scalar(select(m.StudioRun.id).where(m.StudioRun.project_id == project_id, m.StudioRun.status.in_(ACTIVE_RUNS)).limit(1))
        pending = session.scalar(select(m.GenerationJob.id).where(m.GenerationJob.shot_id.in_(changed_shots), m.GenerationJob.status.in_(ACTIVE_CHILDREN)).limit(1))
        media = session.scalar(select(m.MediaProcessTask.id).where(m.MediaProcessTask.shot_id.in_(changed_shots), m.MediaProcessTask.status.in_(ACTIVE_CHILDREN)).limit(1))
        if active or pending or media:
            raise AppError(409, 'studio_voice_generation_active', '此项目仍有已接收的制作任务，请先等待完成或暂停并核对任务，再修改配音参考')
        # Validate all affected shots before the first mutation so a stale/active
        # member cannot leave a partially saved project voice selection.
        for shot_id in changed_shots:
            if shot_map[shot_id].status == 'deleted':
                raise AppError(409, 'shot_deleted', '当前剧本包含已删除镜头，请刷新')
        for shot_id in changed_shots:
            # A new voice reference changes future inputs and the current edit
            # decision. Historical video approvals and accepted task snapshots
            # continue to describe the old immutable output they reviewed.
            shot = shot_map[shot_id]
            shot.revision += 1
            shot.approved_at = None
            if shot.status == 'approved':
                shot.status = 'draft'
            decision = session.scalar(select(m.StudioDecision).where(m.StudioDecision.project_id == project_id,
                                                                    m.StudioDecision.shot_id == shot_id))
            if decision:
                decision.action = 'pending'
                decision.video_id = None
                decision.trim_start, decision.trim_end = 0, None
                decision.revision += 1
        for person in changed:
            person.performance_json = {**person.performance_json, 'reference_audio_asset_id': payload.audio_id or ''}
        session.add(m.AuditEvent(object_type='project_voice', object_id=project_id, action='character_reference_saved',
                                details_json={'character_id': character_id, 'character_version_id': payload.version_id,
                                              'audio_id': payload.audio_id, 'participant_ids': [p.id for p in changed], 'shot_ids': changed_shots}))
        session.flush()
    return voice_preparation(session, settings, project_id, allow_cloud_costs, allow_paid_fallback)

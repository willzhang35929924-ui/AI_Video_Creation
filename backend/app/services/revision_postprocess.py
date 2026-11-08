"""Durable postprocessing for review revisions, without replaying video inference.

Only independently generated, text/identity-bound dialogue can be reused. Native
audio is never lifted from an old visual take. Existing media/quality workers own
the actual processing, their durable IDs remain authoritative across restarts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select, update

from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path

KEY = 'revision_postprocess'
STATUS = 'postprocessing'
ACTIVE = ('queued', 'submitting', 'submitted', 'running', 'postprocessing', 'cancelling', 'reconciling', 'unknown')
_AUDIO_EDIT = re.compile(r'对白|台词|配音|音频|声音|旁白|口型|朗读|说话|不说|改说|念出|静音|无声|小声|大声|低语|耳语|喊叫|唱歌|\b(?:audio|dialogue|dialog|voice|speech|narration|speak|say|lipsync|mute|silence|whisper|shout|sing|yell)\b', re.I)
_PRESERVE_AUDIO = re.compile(
    r'(?<!不要)(?<!不)(?:保留|沿用|保持)(?:(?:原有|原|现有|其他|其它)的?)*'
    r'(?:配音|声音|音频|对白|台词|旁白|口型)(?:不变|原样|保持不变)?'
    r'|(?:(?:原有|原|现有|其他|其它)的?)*(?:配音|声音|音频|对白|台词|旁白|口型)'
    r'(?:保持|继续)?(?:不变|不修改|原样)'
    r'|\b(?:keep|preserve|retain)\s+(?:the\s+)?(?:(?:original|existing|old)\s+)?'
    r'(?:audio|dialogue|voice|speech|narration)(?:\s+unchanged)?\b'
    r'|\b(?:(?:original|existing|old)\s+)?(?:audio|dialogue|voice|speech|narration)\s+(?:remains?\s+)?unchanged\b', re.I)


def requests_audio_change(instruction):
    # Remove only positive, explicit preservation clauses. Negative directives
    # ("do not keep", "不要保留") and any additional requested speech still
    # remain visible to the change detector.
    text = instruction or ''
    if re.search(r'\b(?:do\s+not|don.t|never)\s+(?:keep|preserve|retain)\b', text, re.I):
        return True
    return bool(_AUDIO_EDIT.search(_PRESERVE_AUDIO.sub('', text)))


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _video(session, identifier, project_id, shot_id):
    row = session.get(m.VideoRevision, identifier)
    if not row or row.project_id != project_id or row.shot_id != shot_id:
        raise AppError(409, 'revision_processing_scope', '修订后处理来源必须属于同一项目和镜头，原文件保留')
    return row


def _ancestry(session, current, *, include_audio_scope=False):
    pending = [(current.id, True)]
    seen = set()
    while pending:
        identifier, audio_allowed = pending.pop(0)
        if (identifier, audio_allowed) in seen:
            continue
        if len(seen) >= 100:
            raise AppError(409, 'revision_processing_lineage', '修订来源谱系过长，请先核对源版本')
        seen.add((identifier, audio_allowed))
        video = _video(session, identifier, current.project_id, current.shot_id)
        yield (video, audio_allowed) if include_audio_scope else video
        metadata = video.metadata_json or {}
        source = metadata.get('source_video_id')
        if source:
            pending.append((source, audio_allowed))
        job = session.get(m.GenerationJob, video.generation_job_id)
        if not job or job.project_id != current.project_id or job.shot_id != current.shot_id:
            raise AppError(409, 'revision_processing_scope', '原版本生成任务归属不匹配，未猜测配音来源')
        based = (job.parameters_json or {}).get('based_on_video_id')
        if based:
            context = (job.parameters_json or {}).get('context_inputs') or {}
            original_plan = (job.parameters_json or {}).get(KEY)
            boundary = (requests_audio_change((job.parameters_json or {}).get('instruction'))
                        or context.get('audio_policy') in {'skip', 'native'}
                        or bool(original_plan and original_plan.get('inputs', {}).get('audio') is None))
            # A new explicit mix belonging to this generation can be reused;
            # only crossing into an older take is blocked by its audio intent.
            pending.append((based, audio_allowed and not boundary))


def _spoken_source(session, settings, source, shot):
    asset = session.get(m.AudioAsset, source.get('id'))
    if (not asset or asset.project_id != shot.project_id or asset.shot_id != shot.id
            or source.get('sha256') != asset.sha256):
        raise AppError(409, 'revision_dialogue_source', '原配音来源或归属变化，请在项目配音中确认后重新制作')
    task = session.get(m.MediaProcessTask, asset.task_id) if asset.task_id else None
    line = session.get(m.DialogueLine, asset.dialogue_line_id) if asset.dialogue_line_id else None
    participant = session.get(m.ShotCharacter, asset.participant_id) if asset.participant_id else None
    if (not task or task.kind != 'tts' or task.status != 'succeeded' or not line or not participant
            or task.project_id != shot.project_id or task.shot_id != shot.id
            or line.shot_id != shot.id or participant.shot_id != shot.id or line.participant_id != participant.id
            or participant.character_version_id != asset.character_version_id
            or task.result_json.get('audio_id') != asset.id):
        raise AppError(409, 'revision_dialogue_source', '原音轨缺少可验证的独立台词与说话人绑定，请通过项目配音流程重新制作；未复制旧片整条声音')
    snapshot = task.snapshot_json
    text = snapshot.get('inputs', {}).get('text')
    expected_line = snapshot.get('dialogue_text_override', {}).get('original_text', text)
    if not isinstance(text, str) or line.text != expected_line or asset.metadata_json.get('text_sha256') != hashlib.sha256(text.encode()).hexdigest():
        raise AppError(409, 'revision_dialogue_changed', '台词已经变化，不能沿用旧配音。请先在脚本与分镜更新台词，再通过项目工作台重新生成配音')
    if (snapshot.get('participant_id') != participant.id or snapshot.get('character_version_id') != participant.character_version_id
            or snapshot.get('dialogue_line_id') != line.id):
        raise AppError(409, 'revision_dialogue_source', '原配音角色绑定不完整，未把其他角色声音接到本镜')
    path = safe_media_path(settings.media_root, asset.relative_path, must_exist=True)
    if _sha(path) != asset.sha256:
        raise AppError(409, 'revision_dialogue_file_changed', '原配音文件校验失败，未提交新视频；请恢复文件或重新生成配音')
    return {'audio_id': asset.id, 'sha256': asset.sha256, 'line_id': line.id,
            'line_text': line.text, 'spoken_text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'participant_id': participant.id, 'character_version_id': participant.character_version_id,
            'delivery': copy.deepcopy(line.delivery_json), 'task_id': task.id,
            'timing': {key: copy.deepcopy(source.get('metadata', {}).get(key))
                       for key in ('start_seconds', 'end_seconds', 'volume') if key in source.get('metadata', {})}}


def freeze_plan(session, settings, shot, current, instruction, *, quality=None):
    """Resolve explicit ancestors, not the most recent arbitrary audio/video."""
    audio = None
    quality_source = None
    lip_source = None
    lineage = []
    for video, audio_allowed in _ancestry(session, current, include_audio_scope=True):
        lineage.append(video.id)
        metadata = video.metadata_json or {}
        task_id = metadata.get('processing_task_id') if metadata.get('processing_kind') in {'audio_mix', 'lip_sync'} else None
        task = session.get(m.MediaProcessTask, task_id) if task_id else None
        if task:
            if task.project_id != shot.project_id or task.shot_id != shot.id or task.status != 'succeeded' or task.result_json.get('video_id') != video.id:
                raise AppError(409, 'revision_processing_source', '原后处理任务与视频不一致，未猜测处理来源')
            if task.kind == 'audio_mix' and audio is None and audio_allowed:
                sources = [v for k, v in task.snapshot_json.get('sources', {}).items() if k.startswith('audio_')]
                if not sources:
                    raise AppError(409, 'revision_dialogue_source', '原混音没有独立配音来源，请通过项目工作台重新制作')
                audio = {'mode': task.snapshot_json.get('inputs', {}).get('mode', 'replace_dialogue'),
                         'sources': [_spoken_source(session, settings, source, shot) for source in sources],
                         'source_mix_task_id': task.id}
            elif task.kind == 'lip_sync' and lip_source is None and audio_allowed:
                lip_source = {'route': copy.deepcopy(task.snapshot_json['route']),
                              'parameters': copy.deepcopy(task.snapshot_json.get('parameters', {})),
                              'participant_id': task.snapshot_json.get('participant_id')}
        render_id = metadata.get('quality_render_id')
        if render_id and quality_source is None:
            render = session.get(m.QualityRender, render_id)
            if not render or render.status != 'generated' or render.video_id != video.id:
                raise AppError(409, 'revision_quality_source', '原画质处理回执不完整，请先确认源版本的画质处理结果')
            quality_source = {'settings': copy.deepcopy(render.snapshot_json['settings']),
                              'route': copy.deepcopy(render.snapshot_json.get('route')),
                              'source_render_id': render.id}
    if audio:
        previous = session.get(m.GenerationJob, current.generation_job_id)
        expected_duration = (previous.parameters_json or {}).get('duration_seconds')
        if expected_duration is None or abs(float(expected_duration) - float(shot.duration_seconds)) > .001:
            raise AppError(409, 'revision_dialogue_timing_changed', '镜头时长已变化，不能自动套用旧配音。请通过项目工作台重新处理配音与时长，再制作此修订')
        if requests_audio_change(instruction):
            raise AppError(409, 'revision_dialogue_edit_requires_script', '本次指令涉及声音或台词，不能自动沿用旧配音。请先在脚本与分镜修改对白并通过项目工作台生成；仅修改画面时可直接重试')
    if lip_source and not audio:
        raise AppError(409, 'revision_lip_source', '原版本含口型处理但缺少独立配音来源，请通过项目工作台重新制作')
    if quality_source is None:
        previous = session.get(m.GenerationJob, current.generation_job_id)
        declared = (previous.parameters_json or {}).get('quality_request')
        if declared and declared.get('processing') not in {'native', 'reconstruction'}:
            quality_source = {'settings': copy.deepcopy(quality or declared), 'route': None, 'source_render_id': None}
    if not audio and not quality_source and not lip_source:
        return None
    if quality_source:
        from app.schemas_quality import QualitySettings
        quality_source['settings'] = QualitySettings.model_validate({**quality_source['settings'], 'aspect_ratio': shot.aspect_ratio}).model_dump()
    inputs = {'project_id': shot.project_id, 'shot_id': shot.id, 'source_video_id': current.id,
              'lineage': lineage, 'duration_seconds': float(shot.duration_seconds), 'audio': audio,
              'quality': quality_source, 'lip_sync': lip_source}
    return {'version': 1, 'inputs': inputs, 'fingerprint': _digest(inputs), 'state': 'waiting_generation',
            'raw_video_id': None, 'final_video_id': None, 'children': {}, 'history': []}


def recovery_status(session, settings, video):
    """Read-only diagnosis of older revision raws produced before this feature."""
    job = session.get(m.GenerationJob, video.generation_job_id)
    plan = (job.parameters_json or {}).get(KEY) if job else None
    if plan:
        return {'available': False, 'required': True, 'state': plan.get('state'),
                'raw_video_id': plan.get('raw_video_id'), 'final_video_id': plan.get('final_video_id'),
                'reason': job.error_message or ('修订后处理已完成' if plan.get('state') == 'ready' else '修订后处理计划已保存，未重发视频模型')}
    if (not job or not job.parameters_json.get('based_on_video_id')
            or (video.metadata_json or {}).get('source_video_id')
            or (video.metadata_json or {}).get('processing_kind')):
        return {'available': False, 'required': False, 'state': 'not_required'}
    # An invalid raw revision lineage is not proof that no processing is
    # required. Fail closed until the complete ancestry can establish otherwise.
    required = True
    try:
        required = any((v.metadata_json or {}).get('processing_kind') in {'audio_mix', 'lip_sync', 'video_upscale'}
                       or (v.metadata_json or {}).get('quality_render_id') for v in _ancestry(session, video))
        if not required:
            return {'available': False, 'required': False, 'state': 'not_required'}
        busy = session.scalar(select(m.GenerationJob.id).where(m.GenerationJob.shot_id == video.shot_id,
            m.GenerationJob.status.in_(ACTIVE), m.GenerationJob.id != job.id).limit(1))
        if busy or job.status in ACTIVE:
            raise AppError(409, 'revision_processing_busy', '本镜有正在处理或状态未知的任务，请等待或核对原任务后恢复；未重发模型')
        if job.status not in {'generated', 'revised', 'approved', 'reviewing', 'rejected'} or video.review_status == 'needs_regeneration':
            raise AppError(409, 'revision_processing_raw_ineligible', '原片技术状态不允许恢复，请先处理该原任务的异常')
        check = session.scalar(select(m.QualityCheck).where(m.QualityCheck.video_revision_id == video.id,
            m.QualityCheck.check_type == 'media_decode').order_by(m.QualityCheck.created_at.desc(), m.QualityCheck.id.desc()).limit(1))
        if not check or check.status != 'passed':
            raise AppError(409, 'revision_processing_decode_required', '请先对原片运行完整技术检查，通过后才能恢复配音与画质')
        path = safe_media_path(settings.media_root, video.relative_path, must_exist=True)
        if not video.metadata_json.get('sha256') or _sha(path) != video.metadata_json['sha256']:
            raise AppError(409, 'revision_processing_raw_changed', '原片文件校验失败，未恢复或覆盖历史版本')
        shot = session.get(m.StoryboardShot, video.shot_id)
        frozen = freeze_plan(session, settings, shot, video, job.parameters_json.get('instruction'))
        if not frozen:
            return {'available': False, 'required': False, 'state': 'not_required'}
        return {'available': True, 'required': True, 'state': 'available',
                'reason': '此修订原片尚未接回原配音与画质，可恢复后处理，无需重新生成视频',
                'reuse_dialogue': bool(frozen['inputs']['audio']), 'quality_settings': (frozen['inputs']['quality'] or {}).get('settings')}
    except AppError as error:
        return {'available': False, 'required': required, 'state': 'blocked', 'code': error.code, 'reason': error.message}


def resume_existing(session, settings, video):
    job = session.get(m.GenerationJob, video.generation_job_id)
    plan = (job.parameters_json or {}).get(KEY) if job else None
    if plan:
        if plan.get('raw_video_id') != video.id:
            raise AppError(409, 'revision_processing_already_exists', '该生成任务已有后处理计划，请打开其原片或最终版本，未创建重复任务')
        return job, True
    diagnosis = recovery_status(session, settings, video)
    if not diagnosis.get('available'):
        raise AppError(409, diagnosis.get('code', 'revision_processing_unavailable'),
                       diagnosis.get('reason', '当前版本不需要恢复修订后处理'))
    shot = session.get(m.StoryboardShot, video.shot_id)
    plan = freeze_plan(session, settings, shot, video, job.parameters_json.get('instruction'))
    old_review = video.review_status
    # Competing clicks/restarts may read before the first writer commits. Claim
    # the original generation atomically; a loser returns its already-saved plan.
    claimed = session.execute(update(m.GenerationJob).where(m.GenerationJob.id == job.id,
        m.GenerationJob.parameters_json[KEY].as_string().is_(None),
        m.GenerationJob.status.not_in(ACTIVE)).values(
            parameters_json={**job.parameters_json, KEY: plan}, status=STATUS))
    if claimed.rowcount != 1:
        session.refresh(job)
        existing = (job.parameters_json or {}).get(KEY) or {}
        if existing.get('raw_video_id') == video.id:
            return job, True
        raise AppError(409, 'revision_processing_busy', '任务状态已变化，请刷新后查看已有后处理，未重复创建')
    session.refresh(job)
    attach_raw(job, video)
    video.review_status = 'pending'
    session.add(m.AuditEvent(object_type='generation_job', object_id=job.id, action='revision_processing_resumed',
        details_json={'raw_video_id': video.id, 'old_review_status': old_review, 'provider_resubmitted': False}))
    return job, False


def attach_raw(job, video):
    plan = copy.deepcopy((job.parameters_json or {}).get(KEY))
    if not plan:
        return False
    if plan.get('raw_video_id') and plan['raw_video_id'] != video.id:
        raise AppError(409, 'revision_raw_duplicate', '修订已经保存原片，未重复导入或提交模型')
    plan.update(state='processing', raw_video_id=video.id)
    job.parameters_json = {**job.parameters_json, KEY: plan}
    job.status = STATUS
    job.progress = 1.0
    job.finished_at = None
    video.metadata_json = {**video.metadata_json, KEY: {'job_id': job.id, 'state': 'processing', 'raw_video_id': video.id}}
    video.review_reason = '原始修订已保存，配音／画质处理完成后才能作为最终版本批准'
    return True


def _assert_inputs(session, settings, job, plan):
    inputs = plan.get('inputs') or {}
    if plan.get('version') != 1 or plan.get('fingerprint') != _digest(inputs):
        raise AppError(409, 'revision_processing_snapshot', '修订后处理快照不完整，未猜测原配音或画质')
    if inputs.get('project_id') != job.project_id or inputs.get('shot_id') != job.shot_id:
        raise AppError(409, 'revision_processing_scope', '后处理计划归属不匹配')
    shot = session.get(m.StoryboardShot, job.shot_id)
    if not shot or shot.status == 'deleted' or abs(float(shot.duration_seconds) - inputs['duration_seconds']) > .001:
        raise AppError(409, 'revision_processing_inputs_changed', '后处理期间镜头时长或状态已变化，原片保留，请按当前镜头重新制作')
    for frozen in (inputs.get('audio') or {}).get('sources', []):
        current = _spoken_source(session, settings, {'id': frozen['audio_id'], 'sha256': frozen['sha256'],
                                                    'metadata': frozen.get('timing', {})}, shot)
        if current != frozen:
            raise AppError(409, 'revision_dialogue_changed', '后处理期间配音文本、角色或来源已变化，请确认后重新制作')
    return shot


def validate_generation_plan(session, settings, shot, plan):
    if plan:
        if plan.get('raw_video_id'):
            raise AppError(409, 'revision_raw_already_exists', '修订原片已经存在，只能继续其后处理，不能重新提交视频模型')
        _assert_inputs(session, settings, SimpleNamespace(project_id=shot.project_id, shot_id=shot.id), plan)


def _save(job, plan):
    job.parameters_json = {**job.parameters_json, KEY: copy.deepcopy(plan)}


def _completed_child(session, settings, job, plan, child, kind):
    success = 'generated' if kind == 'quality' else 'succeeded'
    if child.status in {'queued', 'running', 'submitting', 'submitted'}:
        return None
    if child.status != success:
        code = 'revision_processing_unknown' if child.status == 'unknown' else 'revision_processing_child_failed'
        raise AppError(409, code, child.error_message or '修订后处理未完成，原片保留；重试只处理原片，不重新生成视频', {'child_id': child.id, 'kind': kind})
    identifier = child.video_id if kind == 'quality' else child.result_json.get('video_id')
    result = _video(session, identifier, job.project_id, job.shot_id)
    path = safe_media_path(settings.media_root, result.relative_path, must_exist=True)
    if not result.metadata_json.get('sha256') or _sha(path) != result.metadata_json['sha256']:
        raise AppError(409, 'revision_processing_output_changed', '后处理视频文件校验失败，不能作为最终版本')
    return result


def advance(session, settings, job):
    """One deterministic tick; each child ID commits before its worker can run."""
    plan = copy.deepcopy((job.parameters_json or {}).get(KEY))
    if not plan or plan.get('state') == 'ready':
        return
    _assert_inputs(session, settings, job, plan)
    current = _video(session, plan['raw_video_id'], job.project_id, job.shot_id)
    for stage in ('audio', 'lip_sync', 'quality'):
        config = plan['inputs'].get(stage)
        if not config:
            continue
        identifier = plan['children'].get(stage)
        child = session.get(m.QualityRender if stage == 'quality' else m.MediaProcessTask, identifier) if identifier else None
        if identifier and not child:
            raise AppError(409, 'revision_processing_child_missing', '后处理子任务不存在，未重复派发或猜测完成')
        if child:
            if stage == 'audio' and child.kind == 'audio_mix' and child.status == 'unknown':
                # Startup recovery cannot know whether the local encoder
                # published before it died. This child sends no external model
                # request. Retain its output/row and retry at a new owned path.
                child.status = 'failed'
                child.error_message = '本地混音进程中断，原输出保留；修订计划在新路径恢复，不重新请求视频模型'
                plan['history'].append({'stage': stage, 'child_id': child.id, 'status': 'interrupted_local_mix'})
                del plan['children'][stage]
                _save(job, plan)
                return
            completed = _completed_child(session, settings, job, plan, child, stage)
            if completed is None:
                return
            current = completed
            continue
        if stage == 'quality':
            from app.services.video_quality import create_render
            child = create_render(session, settings, current.id, config['settings'], route_snapshot=config.get('route'))
        else:
            from app.schemas_routing import TaskSubmit
            from app.services.media_processing import create_task
            if stage == 'audio':
                inputs = {'operation': 'audio_mix', 'video_id': current.id,
                          'audio_ids': [s['audio_id'] for s in config['sources']], 'mode': config['mode']}
                child = create_task(session, settings, TaskSubmit(slot='dialogue_generation', project_id=job.project_id,
                    shot_id=job.shot_id, inputs=inputs), kind='audio_mix',
                    audio_timing_overrides=[s.get('timing', {}) for s in config['sources']])
            else:
                mix = session.get(m.MediaProcessTask, plan['children'].get('audio'))
                audio_id = mix.result_json.get('audio_id') if mix else None
                if not audio_id:
                    raise AppError(409, 'revision_lip_audio_missing', '口型处理需要已完成的完整混音，未使用单句配音替换')
                inputs = {'video_id': current.id, 'audio_id': audio_id, 'participant_id': config['participant_id']}
                child = create_task(session, settings, TaskSubmit(slot='lip_sync', project_id=job.project_id,
                    shot_id=job.shot_id, inputs=inputs, parameters=config['parameters']), kind='lip_sync', route=config['route'])
        plan['children'][stage] = child.id
        plan['stage'] = stage
        _save(job, plan)
        session.flush()
        return
    plan.update(state='ready', stage='ready', final_video_id=current.id)
    current.metadata_json = {**current.metadata_json, KEY: {'job_id': job.id, 'state': 'ready',
        'raw_video_id': plan['raw_video_id'], 'source_video_id': plan['inputs']['source_video_id'],
        'reused_audio_ids': [x['audio_id'] for x in (plan['inputs'].get('audio') or {}).get('sources', [])],
        'children': copy.deepcopy(plan['children'])}}
    current.review_reason = '修订配音与画质处理已完成，请人工核对画面、声音及口型'
    job.status = 'generated'
    job.finished_at = datetime.now(timezone.utc)
    job.error_code = job.error_message = None
    _save(job, plan)


def fail(job, error):
    plan = copy.deepcopy(job.parameters_json[KEY])
    plan.update(state='blocked' if error.code == 'revision_processing_unknown' else 'failed',
                error={'code': error.code, 'message': error.message, 'details': error.details})
    _save(job, plan)
    job.status = 'failed'
    job.error_code = 'revision_postprocess_failed'
    job.error_message = '修订原片已保存；' + error.message
    job.finished_at = datetime.now(timezone.utc)


def retry(session, settings, job):
    plan = copy.deepcopy((job.parameters_json or {}).get(KEY))
    if not plan or not plan.get('raw_video_id') or job.error_code != 'revision_postprocess_failed':
        return False
    _assert_inputs(session, settings, job, plan)
    for stage, identifier in list(plan['children'].items()):
        child = session.get(m.QualityRender if stage == 'quality' else m.MediaProcessTask, identifier)
        if not child:
            raise AppError(409, 'revision_processing_child_missing', '原后处理任务记录缺失，请核对历史，未重发视频')
        if child.status == 'unknown':
            raise AppError(409, 'revision_processing_unknown', '原后处理任务结果未知，请先核对该子任务；未重发视频或模型')
        if child.status in {'failed', 'blocked', 'cancelled'}:
            if stage == 'quality' and (child.snapshot_json.get('native_execution') or {}).get('inference_submitted'):
                raise AppError(409, 'revision_processing_unknown', '原SeedVR任务已提交，请先核对并恢复该画质任务，未重复生成')
            plan['history'].append({'stage': stage, 'child_id': identifier, 'status': child.status})
            del plan['children'][stage]
    plan.update(state='processing')
    plan.pop('error', None)
    job.status = STATUS
    job.finished_at = None
    job.error_code = job.error_message = None
    _save(job, plan)
    return True


async def process_tick(settings, factory):
    with factory() as session:
        identifiers = list(session.scalars(select(m.GenerationJob.id).where(m.GenerationJob.status == STATUS)))
    for identifier in identifiers:
        with factory() as session:
            job = session.get(m.GenerationJob, identifier)
            try:
                advance(session, settings, job)
            except AppError as error:
                fail(job, error)
            except Exception:
                fail(job, AppError(500, 'revision_processing_internal_error', '后处理调度异常，请检查后台日志后重试此后处理；不会重发视频模型'))
            session.commit()


def assert_review_ready(job, video):
    plan = (job.parameters_json or {}).get(KEY) if job else None
    if plan and (plan.get('state') != 'ready' or plan.get('final_video_id') != video.id):
        raise AppError(409, 'revision_postprocess_required', '这是修订原片或处理中间版本，请等待配音／画质后处理完成后审核最终版本；原文件保留')


def assert_complete_revision(session, job, video):
    """Cheap read-only gate for lists, composition and publishing; no media hash."""
    if not job or job.project_id != video.project_id or job.shot_id != video.shot_id:
        raise AppError(409, 'revision_processing_scope', '视频与生成任务归属不匹配，不能选择为完整修订')
    assert_review_ready(job, video)
    if ((job.parameters_json or {}).get(KEY) or not job.parameters_json.get('based_on_video_id')
            or (video.metadata_json or {}).get('source_video_id')
            or (video.metadata_json or {}).get('processing_kind')):
        return
    for ancestor, audio_allowed in _ancestry(session, video, include_audio_scope=True):
        metadata = ancestor.metadata_json or {}
        if (metadata.get('quality_render_id') or metadata.get('processing_kind') == 'video_upscale'
                or audio_allowed and metadata.get('processing_kind') in {'audio_mix', 'lip_sync'}):
            raise AppError(409, 'revision_postprocess_required', '该历史修订原片尚未完成原配音／画质处理，请在成片审核点击“恢复配音与画质”后选择完整版本')

"""Legacy FinalComposition storage using the modern, stream-preserving encoder."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select, update

from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models import AuditEvent, FinalComposition, VideoRevision, GenerationJob
from app.schemas_product import TimelineRequest, TimelineClip, TimelineTransition, AssemblyOutput
from app.services.assembly import command_for, probe, validate_timeline
from app.services.quality import inspect_media


def _sources(session, item):
    from app.services.revision_postprocess import assert_complete_revision
    if not 1 <= len(item.video_ids_json) <= 100 or len(set(item.video_ids_json)) != len(item.video_ids_json):
        raise AppError(422, 'composition_clip_count', '旧成片需要1至100个不同的真实视频版本')
    videos = []
    for identifier in item.video_ids_json:
        video = session.get(VideoRevision, identifier)
        if not video or video.project_id != item.project_id or video.review_status != 'approved':
            raise AppError(409, 'composition_input_changed', '片段审核或归属已改变，请重新选择本项目批准的完整版本')
        assert_complete_revision(session, session.get(GenerationJob, video.generation_job_id), video)
        videos.append(video)
    return videos


def _snapshot(session, settings, item):
    videos = _sources(session, item)
    sources = [probe(safe_media_path(settings.media_root, video.relative_path, must_exist=True), settings) for video in videos]
    fps = max(source['fps'] for source in sources)
    # The public timeline contract uses integer output rates. Never silently
    # force a fractional source or >60fps down to the old fixed 24fps default.
    if fps < 12 or fps > 60 or abs(fps-round(fps)) > 1e-6:
        raise AppError(422, 'composition_source_fps_unsupported', '旧合成入口无法精确保留该源帧率，请在合成工作台明确选择输出规格；未自动降帧')
    # The retained endpoint accepts up to 100 clips; the modern interactive
    # editor caps its own list at 20. Every server-built child is validated and
    # the legacy 1..100 bound is enforced above. Only the editor's list cap is
    # bypassed; no user-supplied timeline/encoding options bypass validation.
    payload = TimelineRequest.model_construct(
        clips=[TimelineClip(video_id=video.id,trim_start=0,trim_end=None) for video in videos],
        transitions=[TimelineTransition(type='cut',duration=0,audio='cut') for _ in videos[1:]],
        output=AssemblyOutput(resolution_mode='source',fps=round(fps),codec='libx264',audio_policy='preserve',subtitles=False),
        service_id=None)
    return validate_timeline(session, settings, item.project_id, payload)


def compose(settings, snapshot, destination: Path, log_path: Path):
    """Encode this pure snapshot directly; do not enqueue AssemblyRender work."""
    configured = snapshot['service_snapshot']['settings']
    tools = replace(settings, ffmpeg_path=configured.get('ffmpeg_path') or settings.ffmpeg_path,
                    ffprobe_path=configured.get('ffprobe_path') or settings.ffprobe_path)
    command = command_for(tools, snapshot, destination)
    command.insert(1, '-nostdin')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('w', encoding='utf-8') as log:
        log.write(json.dumps(command, ensure_ascii=False)+'\n'); log.flush()
        result = subprocess.run(command, stdout=log, stderr=log, timeout=600, shell=False,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise AppError(502, 'composition_encoding_failed', '真实FFmpeg合成失败，源片和已有输出保留，请查看合成日志')
    checks = inspect_media(destination, tools)
    decode = next((check for check in checks if check['check_type']=='media_decode'), None)
    if not decode or decode['status']!='passed' or any(check['status']=='failed' for check in checks):
        raise AppError(502, 'composition_decode_failed', '合成成片完整解码未通过，未发布为成功')
    actual = probe(destination, tools)
    output = snapshot['output']
    if (actual['width'], actual['height']) != (output['width'], output['height']) or abs(actual['fps']-output['fps']) > 1e-6:
        raise AppError(502, 'composition_output_spec', '实际合成分辨率或帧率不符合源片保留规格')
    if not actual['has_audio']:
        raise AppError(502, 'composition_audio_missing', '合成输出没有要求保留的音轨，未把静音结果当成成功')
    if abs(actual['duration']-snapshot['duration']) > 1.1/output['fps'] or abs(actual['frames']-snapshot['expected_frames']) > 1:
        raise AppError(502, 'composition_output_timing', '合成时长或帧数不符合完整音视频时间线，未静默裁剪')
    for clip in snapshot['clips']:
        source = safe_media_path(settings.media_root, clip['relative_path'], must_exist=True)
        if hashlib.sha256(source.read_bytes()).hexdigest()!=clip['sha256']:
            raise AppError(409, 'composition_source_changed', '合成期间源视频变化，未发布此结果，原文件保留')
    cover = destination.with_suffix('.png')
    with log_path.open('a', encoding='utf-8') as log:
        cover_command = [tools.ffmpeg_path, '-nostdin', '-v', 'error', '-n', '-i', str(destination), '-frames:v', '1', str(cover)]
        log.write(json.dumps(cover_command, ensure_ascii=False)+'\n')
        result = subprocess.run(cover_command, stdout=log, stderr=log, timeout=60, shell=False,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise AppError(502, 'composition_cover_failed', '成片封面提取失败，视频输出已保留')
    return {'command': command, 'checks': checks, 'width': actual['width'], 'height': actual['height'],
        'fps': actual['fps'], 'has_audio': actual['has_audio'], 'actual': actual,
        'audio_note': '保留各源片完整音轨；只有原本无音轨的片段补静音。', 'audio_policy':'preserve',
        'timeline_snapshot': snapshot, 'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
        'log_path': str(log_path), 'cover_relative_path': cover.relative_to(settings.media_root).as_posix()}


async def process_composition(settings, session_factory):
    with session_factory() as session:
        item = session.scalar(select(FinalComposition).where(FinalComposition.status=='queued').order_by(FinalComposition.created_at).limit(1))
        if item is None:
            return
        identifier = item.id
        claimed = session.execute(update(FinalComposition).where(FinalComposition.id==identifier,
            FinalComposition.status=='queued').values(status='running'))
        session.commit()
        if claimed.rowcount != 1:
            return
    try:
        with session_factory() as session:
            item = session.get(FinalComposition, identifier)
            snapshot = _snapshot(session, settings, item)
            relative = f'compositions/{item.project_id}/{identifier}.mp4'
            destination = safe_media_path(settings.media_root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            item.metadata_json = {'timeline_snapshot':snapshot,'audio_policy':'preserve'}
            session.commit()
        metadata = await asyncio.to_thread(compose, settings, snapshot, destination, PROJECT_ROOT/f'logs/compositions/{identifier}.log')
        with session_factory() as session:
            item = session.get(FinalComposition, identifier)
            _sources(session, item)  # review/recovery may change while encoding
            item.status = 'generated'; item.relative_path = relative; item.metadata_json = metadata
            session.add(AuditEvent(object_type='final_composition',object_id=identifier,action='composed',details_json=metadata))
            session.commit()
    except (AppError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        with session_factory() as session:
            item = session.get(FinalComposition, identifier)
            item.status='failed'; item.error_message=error.message if isinstance(error, AppError) else str(error)
            session.commit()

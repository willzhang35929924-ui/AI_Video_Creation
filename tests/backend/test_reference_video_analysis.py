"""Media tests execute actual FFmpeg. Network errors alone use a mock transport."""
import asyncio
import base64
import copy
from io import BytesIO
from pathlib import Path
import wave

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select

from app.api.reference_video_library import router
from app.core.config import Settings
from app.core.db import Base, build_engine, build_session_factory
from app.core.errors import AppError, app_error_handler
from app.models.reference_video import ReferenceVideo, VideoAnalysis, StyleProfile
from app.schemas_reference_video import AnalyzeReferenceVideo, CreateStyleProfile, RetryVideoAnalysis
from app.services import reference_video_analysis as video


@pytest.fixture()
def local_env(tmp_path):
    configured = Settings.from_env()
    if not Path(configured.ffmpeg_path).is_file() or not Path(configured.ffprobe_path).is_file():
        pytest.skip('Actual configured FFmpeg tools are unavailable')
    settings = Settings(database_url='sqlite:///' + (tmp_path / 'video.db').as_posix(),
                        media_root=tmp_path / 'media', seed_demo_data=False, h3_demo_mode=True,
                        ffmpeg_path=configured.ffmpeg_path, ffprobe_path=configured.ffprobe_path)
    settings.media_root.mkdir()
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield settings, factory
    engine.dispose()


@pytest.fixture()
def source_video(local_env, tmp_path):
    settings, _ = local_env
    path = tmp_path / 'two-scenes.mp4'
    video.run([settings.ffmpeg_path, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=192x128:r=12:d=1',
        '-f', 'lavfi', '-i', 'color=c=blue:s=192x128:r=12:d=1', '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
        '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', path])
    return path


def imported(local_env, source_video):
    settings, factory = local_env
    with factory() as session:
        item, _ = video.import_video(session, settings, source_video.name, source_video.read_bytes(), source_note='local test source')
        return item.id


def analyzed(local_env, identifier, key='actual-analysis-1'):
    settings, factory = local_env
    with factory() as session:
        analysis = video.create_analysis(session, settings, identifier,
            AnalyzeReferenceVideo(idempotency_key=key, use_local_models=False, max_frames=4))
        analysis_id = analysis.id
    asyncio.run(video.process_analysis(settings, factory, analysis_id))
    with factory() as session:
        return copy.deepcopy(video.analysis_dto(video.get_analysis(session, analysis_id)))


def test_actual_decode_metrics_scene_cuts_and_no_derived_disk_cache(local_env, source_video):
    identifier = imported(local_env, source_video)
    result = analyzed(local_env, identifier)
    assert result['status'] == 'succeeded', result['error']
    assert result['result']['metadata']['full_decode'] is True
    assert result['result']['metadata']['duration_seconds'] == pytest.approx(2, abs=.1)
    assert len(result['result']['shots']) >= 2
    assert any(abs(s['start_seconds'] - 1) < .2 for s in result['result']['shots'][1:])
    assert result['result']['keyframes']
    assert result['result']['metrics']['sample_mean_saturation'] > .8
    assert result['result']['transcript']['status'] == 'no_audio_stream'
    assert result['result']['visual_model']['status'] == 'not_requested'
    settings, factory = local_env
    files = [p for p in settings.media_root.rglob('*') if p.is_file()]
    assert len(files) == 1 and files[0].name == 'original.mp4'
    assert 'relative_path' not in result['result']['keyframes'][0]
    with factory() as session:
        again, duplicate = video.import_video(session, settings, 'different-name.mp4', source_video.read_bytes())
        assert duplicate is True and again.id == identifier


def test_invalid_upload_cannot_become_active_or_leave_media(local_env):
    settings, factory = local_env
    with factory() as session:
        with pytest.raises(AppError):
            video.import_video(session, settings, '../bad.mp4', b'not a video')
        assert not list(session.scalars(select(ReferenceVideo)))
    assert not [p for p in settings.media_root.rglob('*') if p.is_file()]


def test_duplicate_upload_checks_actual_original_restores_missing_and_preserves_changed(local_env, source_video):
    identifier = imported(local_env, source_video)
    settings, factory = local_env
    with factory() as session:
        item = video.get_video(session, identifier)
        original = settings.media_root / item.relative_path
        original.unlink()  # This test owns only its isolated uploaded fixture.
        restored, duplicate = video.import_video(session, settings, source_video.name, source_video.read_bytes())
        assert duplicate and restored.id == identifier and video.file_digest(original) == item.sha256
        assert restored.metadata_json.get('restored_from_identical_upload_at')
        original.write_bytes(b'explicit changed fixture bytes')
        fresh, duplicate = video.import_video(session, settings, source_video.name, source_video.read_bytes())
        assert not duplicate and fresh.id != identifier
        assert original.read_bytes() == b'explicit changed fixture bytes'
        assert item.state == 'source_changed'
        assert video.file_digest(settings.media_root / fresh.relative_path) == fresh.sha256


def test_durable_idempotency_unknown_and_explicit_retry(local_env, source_video):
    identifier = imported(local_env, source_video)
    settings, factory = local_env
    payload = AnalyzeReferenceVideo(idempotency_key='durable-analysis-key', use_local_models=False)
    with factory() as session:
        first = video.create_analysis(session, settings, identifier, payload)
        assert video.create_analysis(session, settings, identifier, payload).id == first.id
        with pytest.raises(AppError, match='其他分析'):
            video.create_analysis(session, settings, identifier, payload.model_copy(update={'max_frames': 12}))
        first.status = 'unknown'
        session.commit()
        with pytest.raises(AppError, match='结果未知'):
            video.retry_analysis(session, settings, first.id, RetryVideoAnalysis(revision=first.revision, idempotency_key='retry-no-unknown'))
        with pytest.raises(AppError, match='未知'):
            video.create_analysis(session, settings, identifier, payload.model_copy(update={'idempotency_key': 'another-analysis-key'}))
        first.status = 'failed'
        session.commit()
        second = video.retry_analysis(session, settings, first.id,
            RetryVideoAnalysis(revision=first.revision, idempotency_key='explicit-failed-retry'))
        assert second.id != first.id and second.retry_of_id == first.id and second.version == 2


def test_transport_unknown_is_persisted_before_error_and_not_resent(local_env, source_video, monkeypatch):
    identifier = imported(local_env, source_video)
    settings, factory = local_env
    calls = []

    async def failing(request):
        calls.append(request)
        raise httpx.ReadTimeout('in-flight request timeout', request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr(video.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(failing)))
    with factory() as session:
        item = video.create_analysis(session, settings, identifier, AnalyzeReferenceVideo(idempotency_key='unknown-model-call'))
        with pytest.raises(video.ModelOutcomeUnknown):
            asyncio.run(video.local_request(session, item, '/v1/chat/completions', {'model': 'local'}, 'test_request'))
        session.refresh(item)
        attempts = item.receipt_json['attempt_history']
        assert len(attempts) == 1 and attempts[0]['status'] == 'unknown' and attempts[0]['request_sha256']
        item.status = 'running'
        session.commit()
        video.recover_interrupted(session)
        assert item.status == 'unknown'
    assert len(calls) == 1


def test_visual_receipt_and_timestamps_are_checked():
    model = video.LOCAL_SNAPSHOT['model_id']
    response = {'model': model, 'factory_receipt': {'model_id': model, 'offline': True, 'input_image_count': 1},
        'choices': [{'message': {'content': '{"observations":[{"timestamp_seconds":1,"shot_size":"近景"}],"opening":"未知"}'}}]}
    observations, _ = video.parse_visual_result(response, [1.0])
    assert observations[0]['needs_review'] and observations[0]['confidence'] is None
    assert not video.parse_visual_result(response, [9.0])[0]
    response['factory_receipt']['input_image_count'] = 2
    with pytest.raises(AppError, match='回执'):
        video.parse_visual_result(response, [1.0])


def test_bounded_keyframe_sampling_covers_the_full_interval():
    points = video.keyframe_timestamps([{'start_seconds': 0, 'end_seconds': 60}], 60, 8)
    assert len(points) == 8 and points == sorted(points)
    assert points[0] < 5 and points[-1] > 55
    assert all(0 <= p < 60 for p in points)


def test_audio_temporary_directory_is_cleaned_when_asr_outcome_is_unknown(local_env, source_video, tmp_path, monkeypatch):
    from app.services import screenplay_runtime
    settings, factory = local_env
    with_audio = tmp_path / 'actual-audio-video.mp4'
    video.run([settings.ffmpeg_path, '-nostdin', '-v', 'error', '-i', source_video, '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=16000:duration=2', '-map', '0:v', '-map', '1:a', '-c:v', 'copy',
        '-c:a', 'aac', '-shortest', with_audio])
    directories = []
    original_temporary_directory = video.tempfile.TemporaryDirectory

    def tracked_directory(**kwargs):
        directory = original_temporary_directory(dir=tmp_path, **kwargs)
        directories.append(Path(directory.name))
        return directory

    async def no_start(_snapshot):
        return

    async def unknown_asr(_session, _item, endpoint, body, kind):
        assert endpoint == '/factory/transcribe' and kind == 'audio_transcription'
        with wave.open(BytesIO(base64.b64decode(body['audio_base64'])), 'rb') as audio:
            assert audio.getframerate() == 16000 and audio.getnchannels() == 1 and audio.getnframes() > 0
        raise video.ModelOutcomeUnknown('Simulated unknown request outcome for cleanup regression only')

    monkeypatch.setattr(screenplay_runtime, 'ensure_local_service', no_start)
    monkeypatch.setattr(video.tempfile, 'TemporaryDirectory', tracked_directory)
    monkeypatch.setattr(video, 'local_request', unknown_asr)
    with factory() as session:
        item, _ = video.import_video(session, settings, with_audio.name, with_audio.read_bytes())
        analysis = video.create_analysis(session, settings, item.id, AnalyzeReferenceVideo(idempotency_key='cleanup-unknown-asr'))
        result = {'metadata': item.metadata_json, 'keyframes': [], 'visual_observations': []}
        with pytest.raises(video.ModelOutcomeUnknown):
            asyncio.run(video.enrich_local(session, settings, analysis, with_audio, result, []))
    assert directories and all(not directory.exists() for directory in directories)


def test_cleanup_error_cannot_turn_an_unknown_model_request_into_retryable_failure(local_env, source_video, monkeypatch):
    identifier = imported(local_env, source_video)
    settings, factory = local_env
    with factory() as session:
        item = video.create_analysis(session, settings, identifier, AnalyzeReferenceVideo(idempotency_key='cleanup-error-unknown'))
        analysis_id = item.id

    async def unknown_then_cleanup_error(session, _settings, item, _path, _result, _images):
        item.receipt_json = {**item.receipt_json, 'attempt_history': [{'id': 'explicit-error-fixture', 'status': 'unknown'}]}
        session.commit()
        raise PermissionError('Explicit cleanup I/O error regression fixture; no real model request')

    monkeypatch.setattr(video, 'enrich_local', unknown_then_cleanup_error)
    asyncio.run(video.process_analysis(settings, factory, analysis_id))
    with factory() as session:
        item = video.get_analysis(session, analysis_id)
        assert item.status == 'unknown'
        assert item.receipt_json['temporary_media_cleaned'] is False
        assert item.receipt_json['temporary_cleanup_status'] == 'not_verified_after_io_error'
        with pytest.raises(AppError, match='结果未知'):
            video.retry_analysis(session, settings, item.id,
                RetryVideoAnalysis(revision=item.revision, idempotency_key='cannot-replay-cleanup-error'))


def test_multi_video_style_keeps_disagreement_and_requires_primary(local_env, source_video):
    identifier = imported(local_env, source_video)
    first = analyzed(local_env, identifier)
    settings, factory = local_env
    # This test changes stored measured facts to exercise aggregation only;
    # it does not claim these facts came from a second real model request.
    with factory() as session:
        source = session.get(ReferenceVideo, identifier)
        other = ReferenceVideo(id='other-video', name='aggregation fixture', filename='fixture.mp4',
            relative_path=source.relative_path, sha256=source.sha256, metadata_json=source.metadata_json)
        session.add(other)
        session.flush()
        changed = copy.deepcopy(first['result'])
        changed['metrics']['average_shot_seconds'] = 12
        changed['metrics']['sample_mean_brightness'] = .99
        changed['transcript'] = {'text': 'Never insert original dialogue into creative context', 'status': 'transcribed_unverified'}
        analysis = VideoAnalysis(id='aggregation-analysis', video_id=other.id, version=1, idempotency_key='aggregation-fixture-key',
            request_sha256='0' * 64, status='succeeded', result_json=changed, receipt_json={'source_sha256': source.sha256})
        session.add(analysis)
        session.commit()
        profile = video.create_style_profile(session, CreateStyleProfile(video_ids=[identifier, other.id]))
        assert profile.profile_json['requires_primary_selection']
        assert len(profile.profile_json['style_groups']) == 2
        with pytest.raises(AppError, match='主参考'):
            video.style_prompt_context(session, profile.id)
        profile.primary_video_id = identifier
        profile.profile_json = video.build_style_profile([session.get(VideoAnalysis, first['id']), analysis], identifier)
        session.commit()
        context = video.style_prompt_context(session, profile.id)
        assert 'Never insert original dialogue' not in video.canonical(context)
        assert context['primary_video_id'] == identifier and context['model_training_performed'] is False


def test_generation_freezes_analyses_and_waits_without_http_inference(local_env, source_video):
    identifier = imported(local_env, source_video)
    settings, factory = local_env
    with factory() as session:
        frozen = video.freeze_reference_context(session, settings, [identifier], request_key='new-generation')
        assert len(frozen['analysis_ids']) == 1
        assert video.resolve_reference_context(session, frozen)['state'] == 'waiting'
        assert video.freeze_reference_context(session, settings, [identifier], request_key='same-source')['analysis_ids'] == frozen['analysis_ids']
        item = session.get(VideoAnalysis, frozen['analysis_ids'][0])
        item.options_json = {**item.options_json, 'use_local_models': False}
        session.commit()
    asyncio.run(video.process_analysis(settings, factory, item.id))
    with factory() as session:
        ready = video.resolve_reference_context(session, frozen)
        assert ready['state'] == 'ready'
        assert ready['context']['reference_video_ids'] == [identifier]
        assert ready['context']['analysis_ids'] == frozen['analysis_ids']


def test_failed_reanalysis_can_reuse_success_but_unknown_is_never_replayed(local_env, source_video):
    identifier = imported(local_env, source_video)
    succeeded = analyzed(local_env, identifier)
    settings, factory = local_env
    with factory() as session:
        later = video.create_analysis(session, settings, identifier, AnalyzeReferenceVideo(idempotency_key='later-reanalysis'))
        later.status = 'failed'
        session.commit()
        frozen = video.freeze_reference_context(session, settings, [identifier], request_key='use-saved-success')
        assert frozen['analysis_ids'] == [succeeded['id']]
        assert frozen['analysis_selection'][0]['fallback_to_prior_success'] is True
        later.status = 'unknown'
        session.commit()
        unknown = video.freeze_reference_context(session, settings, [identifier], request_key='unknown-not-replayed')
        assert unknown['analysis_ids'] == [later.id]
        assert video.resolve_reference_context(session, unknown)['state'] == 'blocked'
        assert len(list(session.scalars(select(VideoAnalysis)))) == 2


def test_cpu_analysis_behind_gpu_waiter_is_scheduled_without_gpu_gate(local_env, source_video, monkeypatch):
    identifier = imported(local_env, source_video)
    settings, factory = local_env
    with factory() as session:
        video.create_analysis(session, settings, identifier, AnalyzeReferenceVideo(idempotency_key='waiting-gpu-analysis'))
        original = video.get_video(session, identifier)
        other = ReferenceVideo(id='cpu-source-fixture', name='CPU scheduling fixture', filename=original.filename,
            relative_path=original.relative_path, sha256=original.sha256, metadata_json=original.metadata_json)
        session.add(other)
        session.commit()
        cpu = video.create_analysis(session, settings, other.id,
            AnalyzeReferenceVideo(idempotency_key='independent-cpu-analysis', use_local_models=False))
    dispatched = []

    async def record(_settings, _factory, analysis_id):
        dispatched.append(analysis_id)

    async def gpu_must_not_run(*_args):
        raise AssertionError('CPU job must not enter the GPU gate')

    monkeypatch.setattr(video, 'process_analysis', record)
    monkeypatch.setattr(video, '_reference_video_gpu_tick', gpu_must_not_run)
    asyncio.run(video.reference_video_tick(settings, factory))
    assert dispatched == [cpu.id]


def test_api_import_preview_metadata_archive_and_static_routes(local_env, source_video):
    settings, factory = local_env
    app = FastAPI()
    app.state.settings, app.state.session_factory = settings, factory
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post('/api/reference-videos', files=[('files', ('sample.mp4', source_video.read_bytes(), 'video/mp4'))])
        assert response.status_code == 201
        item = response.json()['items'][0]
        identifier = item['id']
        assert client.get('/api/reference-videos/style-profiles').json() == {'items': []}
        assert client.get('/api/reference-videos?q=sample').json()['items'][0]['id'] == identifier
        preview = client.get(f'/api/reference-videos/{identifier}/frames?timestamp_seconds=.5')
        assert preview.status_code == 200 and preview.content[:2] == b'\xff\xd8'
        assert preview.headers['cache-control'] == 'no-store'
        assert client.get(f'/api/reference-videos/{identifier}/frames?timestamp_seconds=100').status_code == 422
        edit = {'revision': item['revision'], 'name': 'Edited', 'category': 'test', 'tags': ['red', 'blue']}
        changed = client.patch(f'/api/reference-videos/{identifier}', json=edit)
        assert changed.status_code == 200
        assert client.patch(f'/api/reference-videos/{identifier}', json=edit).status_code == 409
        revision = changed.json()['revision']
        archived = client.delete(f'/api/reference-videos/{identifier}?revision={revision}')
        assert archived.json()['file_preserved'] is True
        assert client.get('/api/reference-videos').json()['items'] == []
        assert (settings.media_root / item['relative_path']).is_file()

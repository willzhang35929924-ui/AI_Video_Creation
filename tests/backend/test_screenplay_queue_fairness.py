"""Scheduler regressions: waiting library analysis must not starve other jobs."""
import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from app import models as m
from app.core.db import Base, build_engine, build_session_factory
from app.services import screenplay_generation as service
from app.services import video_quality
from app.services import gpu_gate


@pytest.fixture()
def factory(settings, monkeypatch):
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        session.add(m.AIService(id='queue_model', module='language_processing',
            provider='local_llm', name='isolated queue test',
            base_url='http://127.0.0.1:8771/v1', default_model='local', enabled=True))
        session.commit()

    async def released(_settings):
        return {'verified': True}

    monkeypatch.setattr(video_quality, 'confirm_gpu_release', released)
    return factory


def analysis(factory, identifier, status):
    with factory() as session:
        video_id = 'video_' + identifier
        session.add(m.ReferenceVideo(id=video_id, name=video_id, filename='test.mp4',
            relative_path='unused.mp4', sha256='a' * 64))
        session.flush()
        session.add(m.VideoAnalysis(id=identifier, video_id=video_id,
            idempotency_key=identifier, request_sha256='b' * 64, status=status))
        session.commit()


def job(factory, identifier, order, *, status='queued', context=None):
    snapshot = {'service_snapshot': {'service_id': 'queue_model',
        'service_revision': 1, 'is_cloud': False}}
    if context is not None:
        snapshot['reference_context'] = context
    with factory() as session:
        session.add(m.ScreenplayGenerationJob(id=identifier,
            idempotency_key=identifier, request_sha256='c' * 64,
            project_name=identifier, input_mode='video' if context else 'text',
            creative_prompt='isolated scheduler regression', status=status,
            snapshot_json=snapshot,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=order)))
        session.commit()


def tick(settings, factory):
    asyncio.run(service.screenplay_tick(settings, factory))


def collect_processed(monkeypatch):
    processed = []

    async def record(_settings, _factory, identifier):
        processed.append(identifier)

    monkeypatch.setattr(service, 'process_job', record)
    return processed


def test_cloud_screenplay_jobs_dispatch_when_local_gpu_health_is_uncertain(settings, factory, monkeypatch):
    job(factory, 'local_waiting', 0)
    job(factory, 'cloud_ready', 1)
    with factory() as session:
        cloud = session.get(m.ScreenplayGenerationJob, 'cloud_ready')
        cloud.snapshot_json = {**cloud.snapshot_json, 'service_snapshot': {**cloud.snapshot_json['service_snapshot'], 'is_cloud': True}}
        session.commit()
    processed = collect_processed(monkeypatch)

    async def uncertain_local_runtime():
        return True

    monkeypatch.setattr(gpu_gate, 'owned_model_busy', uncertain_local_runtime)
    asyncio.run(service.screenplay_tick(settings, factory))

    assert processed == ['cloud_ready']
    with factory() as session:
        assert session.get(m.ScreenplayGenerationJob, 'local_waiting').status == 'queued'


def test_waiting_video_allows_later_text_and_never_retries_unknown(settings, factory, monkeypatch):
    analysis(factory, 'waiting', 'running')
    job(factory, 'old_unknown', -1, status='unknown')
    job(factory, 'waiting_video', 0, context={'analysis_ids': ['waiting']})
    job(factory, 'later_text', 1)
    job(factory, 'next_text', 2)
    processed = collect_processed(monkeypatch)

    tick(settings, factory)

    assert processed == ['later_text']
    with factory() as session:
        assert session.get(m.ScreenplayGenerationJob, 'waiting_video').status == 'queued'
        assert session.get(m.ScreenplayGenerationJob, 'old_unknown').status == 'unknown'


def test_waiting_queue_has_no_fixed_candidate_limit(settings, factory, monkeypatch):
    analysis(factory, 'waiting', 'queued')
    for index in range(130):
        job(factory, f'waiting_{index:03}', index, context={'analysis_ids': ['waiting']})
    job(factory, 'runnable_after_130', 130)
    processed = collect_processed(monkeypatch)

    tick(settings, factory)

    assert processed == ['runnable_after_130']


def test_all_waiting_does_not_dispatch(settings, factory, monkeypatch):
    analysis(factory, 'waiting', 'running')
    analysis(factory, 'finished', 'succeeded')
    job(factory, 'waiting_video', 0,
        context={'analysis_ids': ['finished', 'waiting']})
    processed = collect_processed(monkeypatch)

    tick(settings, factory)

    assert processed == []


@pytest.mark.parametrize('analysis_status', ['failed', 'blocked', 'unknown'])
def test_failed_analysis_blocks_first_job_without_dispatching_later(settings, factory, analysis_status):
    analysis(factory, 'bad', analysis_status)
    analysis(factory, 'waiting', 'running')
    job(factory, 'invalid_video', 0,
        context={'analysis_ids': ['bad', 'waiting']})
    job(factory, 'later_text', 1)

    tick(settings, factory)

    with factory() as session:
        first = session.get(m.ScreenplayGenerationJob, 'invalid_video')
        assert first.status == 'blocked'
        assert first.receipt_json['reference_analysis']['state'] == 'blocked'
        assert not first.receipt_json.get('provider_request_submitted')
        assert session.get(m.ScreenplayGenerationJob, 'later_text').status == 'queued'


@pytest.mark.parametrize('context', [
    {'analysis_ids': ['missing']},
    {'reference_video_ids': ['missing']},
    {'analysis_ids': [None]},
    ['invalid frozen context'],
])
def test_missing_or_invalid_frozen_analysis_is_blocked_not_waiting(settings, factory, context):
    job(factory, 'invalid_video', 0, context=context)
    job(factory, 'later_text', 1)

    tick(settings, factory)

    with factory() as session:
        first = session.get(m.ScreenplayGenerationJob, 'invalid_video')
        assert first.status == 'blocked'
        assert '冻结参考视频分析不可用' in first.error_message
        assert not first.receipt_json.get('provider_request_submitted')
        assert session.get(m.ScreenplayGenerationJob, 'later_text').status == 'queued'


def test_completed_analysis_is_selected_in_stable_creation_order(settings, factory, monkeypatch):
    analysis(factory, 'done', 'succeeded')
    job(factory, 'b_job', 0)
    job(factory, 'a_video', 0, context={'analysis_ids': ['done']})
    processed = collect_processed(monkeypatch)

    tick(settings, factory)

    assert processed == ['a_video']


@pytest.mark.parametrize('finish_reason', ['length', 'time_limit'])
def test_interrupted_provider_output_is_failed_without_export_or_resubmission(settings, factory, monkeypatch, finish_reason):
    job(factory, 'interrupted', 0)
    submitted = []
    exported = []

    async def provider(_settings, session, item, _parts, _source):
        submitted.append(item.id)
        item.receipt_json = {'provider_request_submitted': True,
            'provider_response_relative_path': 'retained_actual_response.json'}
        session.commit()
        return ({'choices': [{'message': {'content': '{}'}, 'finish_reason': finish_reason}]},
            item.snapshot_json['service_snapshot'], '', 200)

    monkeypatch.setattr(service, '_call_provider', provider)
    monkeypatch.setattr(service, 'export_job', lambda *args: exported.append(args))

    asyncio.run(service.process_job(settings, factory, 'interrupted'))
    # Calling the processor a second time must not resend a terminal job.
    asyncio.run(service.process_job(settings, factory, 'interrupted'))

    with factory() as session:
        item = session.get(m.ScreenplayGenerationJob, 'interrupted')
        assert item.status == 'failed'
        assert item.receipt_json['finish_reason'] == finish_reason
        assert item.receipt_json['provider_response_received'] is True
        assert item.receipt_json['provider_response_relative_path'] == 'retained_actual_response.json'
        assert item.receipt_json['automatic_retry'] is False
        assert not item.screenplay_json
    assert submitted == ['interrupted']
    assert exported == []


@pytest.mark.parametrize('busy,loaded,foreign,expected_calls', [
    (True, False, False, 0),
    (False, True, False, 0),
    (False, False, False, 1),
    (True, True, True, 0),
])
def test_shared_gpu_gate_uses_owned_model_health_without_network(monkeypatch, isolate_owned_model_health, busy, loaded, foreign, expected_calls):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            assert url == 'http://127.0.0.1:8771/health'
            return self

        def raise_for_status(self):
            pass

        def json(self):
            return {'service': 'video-factory-screenplay-qwen-v1',
                'workspace': 'other_workspace' if foreign else str(gpu_gate.PROJECT_ROOT),
                'busy': busy, 'loaded': loaded}

    monkeypatch.setattr(gpu_gate, 'owned_model_busy', isolate_owned_model_health)
    monkeypatch.setattr(gpu_gate.httpx, 'AsyncClient', Client)

    @gpu_gate.gpu_submission
    async def submit():
        calls.append('submitted')

    asyncio.run(submit())

    assert len(calls) == expected_calls


@pytest.mark.parametrize('failure,expected_calls', [('timeout',0), ('json',0), ('connection_unknown',0), ('connection_refused',1)])
def test_health_uncertainty_defers_dispatch_and_only_proven_refusal_is_absent(monkeypatch, isolate_owned_model_health, failure, expected_calls):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, _url):
            if failure == 'timeout':
                raise gpu_gate.httpx.ReadTimeout('isolated timeout')
            if failure == 'connection_refused':
                try:
                    raise OSError(10061, 'isolated refused')
                except OSError as exc:
                    raise gpu_gate.httpx.ConnectError('isolated transport') from exc
            if failure == 'connection_unknown':
                raise gpu_gate.httpx.ConnectError('isolated connection uncertainty')
            return self

        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError('isolated invalid JSON')

    monkeypatch.setattr(gpu_gate, 'owned_model_busy', isolate_owned_model_health)
    monkeypatch.setattr(gpu_gate, '_managed_screenplay_process_state', lambda: None)
    monkeypatch.setattr(gpu_gate.httpx, 'AsyncClient', Client)

    @gpu_gate.gpu_submission
    async def submit():
        calls.append('submitted')

    asyncio.run(submit())
    assert len(calls) == expected_calls


def test_default_test_gate_never_constructs_a_real_health_client(monkeypatch):
    def forbidden_client(**kwargs):
        raise AssertionError('Isolated test attempted production health access')

    monkeypatch.setattr(gpu_gate.httpx, 'AsyncClient', forbidden_client)
    calls = []

    @gpu_gate.gpu_submission
    async def submit():
        calls.append('isolated')

    asyncio.run(submit())
    assert calls == ['isolated']

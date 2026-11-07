"""Isolated real SQLite projection tests; no model inference or file success claim."""
import asyncio
import copy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import pytest
from app import models as m
from app.core.db import Base, build_engine, build_session_factory
from app.services.screenplay_production_status import sync_production_status
from app.services.studio_pipeline import _persist
from app.services.studio_project_guard import project_guard
from app.api import studio as studio_api
from app.core.errors import AppError


@pytest.fixture()
def context(settings):
    engine = build_engine(settings.database_url); Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        project = m.Project(id='project', name='projection', source_uri='screenplay_job:source')
        session.add(project); session.flush()
        source = {'four_stage_status': {'analysis': 'succeeded', 'style_summary': 'succeeded',
            'screenplay': 'succeeded', 'new_video': 'not_started'},
            'reference_videos': {'analysis_ids': ['immutable_analysis']}, 'source_sha256': 'a' * 64}
        job = m.ScreenplayGenerationJob(id='source', idempotency_key='source',
            request_sha256='b' * 64, project_name='projection', input_mode='video',
            creative_prompt='test', project_id=project.id,
            receipt_json={'source': copy.deepcopy(source), 'reference_analysis': {'state': 'ready'}})
        session.add(job)
        shot = m.StoryboardShot(id='shot', project_id=project.id, order=1,
            scene_title='test', action='test', visual_prompt='test')
        session.add(shot); session.flush()
        child = m.GenerationJob(id='child', project_id=project.id, shot_id=shot.id,
            provider='comfyui_h3', status='succeeded', prompt='test')
        session.add(child)
        run = m.StudioRun(id='run', project_id=project.id, status='running',
            snapshot_json={'shot_ids': [shot.id]}, nodes_json=[],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        session.add(run); session.commit()
        yield SimpleNamespace(session=session, project=project, job=job, shot=shot,
            run=run, source=source, child=child)
    engine.dispose()


def state(c):
    c.session.refresh(c.job)
    source = c.job.receipt_json['source']
    for key in ['analysis', 'style_summary', 'screenplay']:
        assert source['four_stage_status'][key] == 'succeeded'
    assert source['reference_videos'] == c.source['reference_videos']
    assert source['source_sha256'] == c.source['source_sha256']
    assert c.job.receipt_json['reference_analysis'] == {'state': 'ready'}
    return source['four_stage_status']['new_video'], source['video_production']


def add_video(c):
    video = m.VideoRevision(id='actual_video', project_id=c.project.id, shot_id=c.shot.id,
        generation_job_id=c.child.id, relative_path='real/generated.mp4', revision=1)
    c.session.add(video); c.session.flush()
    c.run.nodes_json = [{'kind': 'video', 'status': 'succeeded', 'video_id': video.id}]
    return video


def test_successful_analysis_cannot_be_video_success(context):
    c = context
    sync_production_status(c.session, c.run); c.session.commit()
    status, evidence = state(c)
    assert status == 'generating' and evidence['videos'] == []
    assert evidence['run_id'] == c.run.id
    c.run.status = 'waiting_review'
    _persist(c.session, c.run, [{'kind': 'video', 'status': 'succeeded', 'video_id': 'invented'}])
    status, evidence = state(c)
    assert status == 'failed' and evidence['videos'] == []


def test_real_video_row_allows_review_but_not_final_success(context):
    c = context; add_video(c); c.run.status = 'waiting_review'
    _persist(c.session, c.run, c.run.nodes_json)
    status, evidence = state(c)
    assert status == 'awaiting_review'
    assert evidence['videos'][0]['video_id'] == 'actual_video'
    assert evidence['human_visual_approval_inferred'] is False
    c.run.status = 'completed'; sync_production_status(c.session, c.run); c.session.commit()
    assert state(c)[0] == 'awaiting_review'


@pytest.mark.parametrize('status,expected', [('running','generating'), ('generated','succeeded'),
    ('failed','failed'), ('unknown','unknown'), ('cancelled','cancelled')])
def test_only_real_matching_generated_render_is_final_success(context, status, expected):
    c = context; add_video(c)
    assembly = m.Assembly(id='assembly', project_id=c.project.id, name='test')
    c.session.add(assembly); c.session.flush()
    render = m.AssemblyRender(id='render', assembly_id=assembly.id, version=1,
        status=status, snapshot_json={}, relative_path='final/render.mp4' if status == 'generated' else None)
    c.session.add(render); c.session.flush()
    c.run.status = 'rendering'; c.run.result_json = {'render_id': render.id}
    sync_production_status(c.session, c.run); c.session.commit()
    actual, evidence = state(c)
    assert actual == expected
    assert evidence['render_id'] == render.id
    assert evidence['render']['status'] == status


@pytest.mark.parametrize('node_status,expected', [('unknown','unknown'), ('exception','failed'), ('failed','failed')])
def test_real_node_errors_remain_explicit(context, node_status, expected):
    c = context
    _persist(c.session, c.run, [{'kind': 'video', 'status': node_status}])
    assert state(c)[0] == expected


def test_foreign_render_and_missing_render_never_succeed(context):
    c = context; add_video(c)
    c.session.add(m.Project(id='foreign', name='foreign')); c.session.flush()
    c.session.add(m.Assembly(id='foreign_assembly', project_id='foreign', name='foreign')); c.session.flush()
    c.session.add(m.AssemblyRender(id='foreign_render', assembly_id='foreign_assembly',
        version=1, status='generated', snapshot_json={}, relative_path='foreign.mp4'))
    c.session.flush()
    for identifier in ['foreign_render', 'missing']:
        c.run.result_json = {'render_id': identifier}
        sync_production_status(c.session, c.run); c.session.commit()
        assert state(c)[0] == 'failed'


def test_older_run_and_wrong_origin_cannot_overwrite(context):
    c = context
    newer = m.StudioRun(id='newer', project_id=c.project.id, status='queued',
        snapshot_json={'shot_ids': [c.shot.id]}, nodes_json=[],
        created_at=c.run.created_at + timedelta(seconds=1))
    c.session.add(newer); c.session.commit()
    old = copy.deepcopy(c.job.receipt_json)
    sync_production_status(c.session, c.run); c.session.commit()
    assert c.job.receipt_json == old
    c.project.source_uri = 'word:unrelated'
    sync_production_status(c.session, newer); c.session.commit()
    assert c.job.receipt_json == old
    c.project.source_uri = 'screenplay_job:source'; c.job.project_id = None
    sync_production_status(c.session, newer); c.session.commit()
    assert c.job.receipt_json == old


def test_pause_and_resume_api_commit_source_stage(context):
    c = context
    studio_api.pause(c.project.id, session=c.session)
    assert state(c)[0] == 'paused'
    studio_api.resume(c.project.id, session=c.session)
    assert state(c)[0] == 'generating'


def test_known_failure_retry_api_updates_stage_and_preserves_history(context):
    c = context
    c.run.status = 'completed_with_errors'
    c.run.nodes_json = [{'id': 'video:shot', 'kind': 'video', 'target_id': 'shot',
        'status': 'failed', 'history': [], 'child_id': 'child', 'code': 'known_failure',
        'error': 'isolated known failure', 'dependencies': []}]
    c.session.commit()
    studio_api.retry(c.project.id, {'node_id': 'video:shot'}, session=c.session)
    assert state(c)[0] == 'generating'
    assert c.run.nodes_json[0]['history'][0]['child_id'] == 'child'
    assert c.run.nodes_json[0]['status'] == 'pending'


def test_targeted_retry_keeps_a_child_submitted_while_the_request_waits(context, settings):
    c = context
    project_id, run_id = c.project.id, c.run.id
    failed = {'id': 'video:shot', 'kind': 'video', 'target_id': 'shot', 'status': 'failed',
        'history': [], 'child_id': 'child', 'code': 'known_failure', 'dependencies': []}
    other = {'id': 'video:other', 'kind': 'video', 'target_id': 'other', 'status': 'dispatching',
        'history': [], 'child_id': None, 'dependencies': []}
    c.run.nodes_json = [failed, other]
    c.session.commit()
    factory = build_session_factory(c.session.get_bind())

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        with factory() as request_session:
            cached_run = request_session.get(m.StudioRun, run_id)
            assert cached_run.nodes_json[1]['child_id'] is None
            async with project_guard(app, project_id):
                request = SimpleNamespace(app=app)
                retry = asyncio.create_task(studio_api.retry_project(
                    project_id, request, {'node_id': failed['id']}, request_session))
                await asyncio.sleep(0)
                assert not retry.done()
                c.run.nodes_json = [failed, {**other, 'status': 'running', 'child_id': 'worker-submitted'}]
                c.session.commit()
            await asyncio.wait_for(retry, timeout=5)

    asyncio.run(scenario())
    c.session.refresh(c.run)
    retried, preserved = c.run.nodes_json
    assert retried['status'] == 'pending' and retried['history'][0]['child_id'] == 'child'
    assert preserved['status'] == 'running' and preserved['child_id'] == 'worker-submitted'


def test_unknown_retry_api_remains_forbidden_and_keeps_source_receipt(context):
    c = context
    c.run.nodes_json = [{'id': 'video:shot', 'kind': 'video', 'target_id': 'shot',
        'status': 'unknown', 'history': [], 'child_id': 'child', 'code': 'unknown',
        'error': 'unknown result', 'dependencies': []}]
    sync_production_status(c.session, c.run); c.session.commit()
    before = copy.deepcopy(c.job.receipt_json)
    with pytest.raises(AppError) as raised:
        studio_api.retry(c.project.id, {'node_id': 'video:shot'}, session=c.session)
    assert raised.value.code == 'studio_unknown_not_retryable'
    c.session.refresh(c.job)
    assert c.job.receipt_json == before

"""DAG/dispatch contracts only: no external service or model is called."""
from types import SimpleNamespace

import pytest

from app import models as m
from app.core.db import Base, build_engine, build_session_factory
from app.core.errors import AppError
from app.services import studio_pipeline as studio
from app.services import task_routing
from app.services import production_orchestrator as production
from app.schemas_cinematic import RunInput
from app.schemas_quality import QualitySettings


@pytest.fixture
def session(settings):
    settings.h3_demo_mode = False
    settings.container_mode = True
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    try:
        with build_session_factory(engine)() as session:
            yield session
    finally:
        engine.dispose()


def project_shots(session, count=1):
    project = m.Project(name='Isolated adapter compatibility')
    session.add(project); session.flush()
    script = m.ScriptRevision(project_id=project.id, revision=1, raw_text='Ocean waves.', status='approved')
    session.add(script)
    shots = [m.StoryboardShot(project_id=project.id, order=index + 1,
                             visual_prompt='Ocean waves over smooth stones.', aspect_ratio='16:9', status='approved')
             for index in range(count)]
    session.add_all(shots); session.flush()
    return project, shots, script


def video_service(session, kind, *, default=True):
    context = kind == 'context'
    service = m.AIService(module='video_generation', provider='video_api' if kind == 'cloud' else 'comfyui',
        name='Isolated ' + kind, is_default=default,
        default_model='cloud-video' if kind == 'cloud' else 'minimax_h3_ref2va_pruned_w6a8.safetensors',
        base_url='https://example.invalid/v1' if kind == 'cloud' else 'http://host.docker.internal:8788' if context else 'http://host.docker.internal:8189',
        settings_json=({'video_contract': 'video-jobs-v1'} if kind == 'cloud' else
                       {'workflow_profile': 'context_ir', 'h3_context_ir': True,
                        'local_service_origin': 'http://host.docker.internal:8788'} if context else
                       {'workflow_profile': 'native_h3_r2v', 'h3_context_ir': False,
                        'local_service_origin': 'http://host.docker.internal:8189'}))
    session.add(service); session.flush()
    return service


@pytest.mark.parametrize('kind', ['cloud', 'native'])
def test_existing_cloud_and_native_profiles_keep_keyframe_pipeline(session, settings, kind):
    project, shots, _ = project_shots(session)
    video_service(session, kind)
    run = studio.create_pipeline(session, settings, project.id, {'allow_cloud_costs': True})
    assert run.snapshot_json['h3_context_by_shot'] == {}
    assert run.snapshot_json['context_production'] is False
    assert any(node['id'] == 'keyframe:' + shots[0].id for node in run.nodes_json)
    video = next(node for node in run.nodes_json if node['kind'] == 'video')
    assert video['dependencies'] == ['keyframe:' + shots[0].id]


def test_cloud_without_quality_override_keeps_requested_generation_dimensions(session, settings):
    project, shots, _ = project_shots(session)
    video_service(session, 'cloud')
    run = studio.create_pipeline(session, settings, project.id,
        {'allow_cloud_costs': True, 'width': 1280, 'height': 720})
    assert run.snapshot_json['options']['quality_by_shot'] == {}
    assert studio.generation_dimensions(run, shots[0]) == (1280, 720)


def mixed_run(session, settings, monkeypatch):
    project, shots, script = project_shots(session, 2)
    cloud = video_service(session, 'cloud')
    context = video_service(session, 'context', default=False)
    session.add(m.RoutePolicy(scope='shot', target_id=shots[0].id, slot='video_generation', revision=1,
        settings_json={'mode': 'local_only', 'choices': [{'service_id': context.id, 'model_id': context.default_model}]}))
    session.flush()
    # This test isolates dispatch construction from the independent output-proof
    # registry. It does not create a generated/approved media record.
    monkeypatch.setattr(task_routing, '_evidence', lambda *_args: {'kind': 'isolated-route-contract'})
    run = studio.create_pipeline(session, settings, project.id, {'allow_cloud_costs': True})
    session.flush()
    return run, shots, script, cloud


def test_mixed_profiles_build_dependencies_per_shot_and_freeze_selected_origin(session, settings, monkeypatch):
    run, shots, _, _ = mixed_run(session, settings, monkeypatch)
    contexts = run.snapshot_json['h3_context_by_shot']
    assert set(contexts) == {shots[0].id}
    assert contexts[shots[0].id]['service']['base_url'] == 'http://host.docker.internal:8788'
    assert contexts[shots[0].id]['service']['h3_workflow']['actual_steps'] == 30
    nodes = {node['id']: node for node in run.nodes_json}
    assert 'keyframe:' + shots[0].id not in nodes
    assert nodes['video:' + shots[0].id]['dependencies'] == []
    assert nodes['video:' + shots[1].id]['dependencies'] == ['keyframe:' + shots[1].id]


def test_legacy_shot_in_mixed_run_still_requires_visual_preflight(session, settings, monkeypatch):
    run, shots, script, _ = mixed_run(session, settings, monkeypatch)
    monkeypatch.setattr(studio, 'production_context', lambda *_args: (None, None, script, {}, []))
    called = []
    def preflight(*_args, **kwargs):
        called.append(kwargs)
        return {'findings': [], 'inputs_hash': 'isolated'}
    monkeypatch.setattr(studio, 'shot_preflight', preflight)
    studio._accept_inputs(session, settings, run, shots[0])
    assert called[0]['context_inputs'] is True and called[0]['require_visual'] is False
    with pytest.raises(AppError) as blocked:
        studio._accept_inputs(session, settings, run, shots[1])
    assert blocked.value.code == 'studio_structured_script_required'
    assert len(called) == 1


def test_cloud_dispatch_in_mixed_run_keeps_legacy_adapter_arguments(session, settings, monkeypatch):
    from app.api import routes
    run, shots, _, cloud = mixed_run(session, settings, monkeypatch)
    legacy = shots[1]
    run.snapshot_json['options']['force_shot_ids'] = [legacy.id]
    monkeypatch.setattr(studio, '_accept_inputs', lambda *_args: None)
    captured = []
    async def stop_before_io(*_args, **kwargs):
        captured.append((_args[3], kwargs))
        raise AppError(422, 'isolated_no_submit', 'Stopped before provider submission')
    monkeypatch.setattr(routes, 'generate_for_shot', stop_before_io)
    entry = next(node for node in run.nodes_json if node['id'] == 'video:' + legacy.id)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    coroutine = studio._video(app, session, settings, run, entry, True)
    with pytest.raises(AppError) as stopped:
        try:
            coroutine.send(None)
        finally:
            coroutine.close()
    assert stopped.value.code == 'isolated_no_submit'
    request, arguments = captured[0]
    assert request.service_id == cloud.id
    assert arguments['legacy_production'] is True
    assert arguments['frozen_service'] is None and arguments['frozen_context'] is None


def test_container_requires_explicit_context_but_windows_legacy_loopback_mapping_survives(settings, monkeypatch):
    snapshot = {'provider': 'comfyui', 'base_url': 'http://127.0.0.1:8189',
                'model_id': 'minimax_h3_ref2va_pruned_w6a8.safetensors', 'settings': {}}
    monkeypatch.setattr(studio.sys, 'platform', 'win32')
    settings.container_mode = True
    assert studio.wants_context_profile(settings, snapshot) is False
    settings.container_mode = False
    assert studio.wants_context_profile(settings, snapshot) is True
    snapshot['settings'] = {'workflow_profile': 'native_h3_r2v', 'h3_context_ir': False}
    assert studio.wants_context_profile(settings, snapshot) is False
    snapshot['settings'] = {'workflow_profile': 'context_ir', 'h3_context_ir': True}
    settings.container_mode = True
    assert studio.wants_context_profile(settings, snapshot) is True
    snapshot['provider'] = 'video_api'
    assert studio.wants_context_profile(settings, snapshot) is False


@pytest.mark.parametrize('kind', ['cloud', 'native'])
def test_cinematic_run_keeps_legacy_services_frozen_for_cloud_and_native(session, settings, kind):
    project, shots, script = project_shots(session)
    script.parsed_json = {'versions': {'isolated': 'contract'}, 'rules_snapshot': {'isolated': True}}
    video = video_service(session, kind)
    image = m.AIService(module='character_generation', provider='openai_compatible', name='Isolated image API',
        base_url='https://example.invalid/v1', default_model='image-model', settings_json={'width': 832, 'height': 1024})
    session.add(image); session.flush()
    run = production.create_run(session, settings, project.id,
        RunInput(shot_ids=[shots[0].id], video_service_id=video.id, visual_service_id=image.id, confirmed=True))
    assert run.snapshot_json['h3_context_by_shot'] == {}
    assert run.snapshot_json['context_production'] is False
    assert run.snapshot_json['selected_service_snapshots']['video']['service_id'] == video.id
    assert run.snapshot_json['selected_service_snapshots']['visual']['service_id'] == image.id
    assert run.snapshot_json['options']['visual_width'] == 832
    assert run.snapshot_json['options']['visual_height'] == 1024


def test_cinematic_context_profile_does_not_require_unneeded_image_service(session, settings):
    project, shots, _ = project_shots(session)
    video = video_service(session, 'context')
    run = production.create_run(session, settings, project.id,
        RunInput(shot_ids=[shots[0].id], video_service_id=video.id, confirmed=True))
    assert set(run.snapshot_json['h3_context_by_shot']) == {shots[0].id}
    assert run.snapshot_json['selected_service_snapshots'] == {}
    assert run.snapshot_json['context_production'] is True


def test_cinematic_input_approval_keeps_visual_requirement_for_legacy_shot_in_mixed_run(session, settings, monkeypatch):
    from app.api import cinematic
    from app.schemas import ApprovalRequest
    from starlette.requests import Request
    project, shots, script = project_shots(session, 2)
    run = m.ProductionRun(project_id=project.id, script_revision_id=script.id, status='waiting_review',
        snapshot_json={'context_production': True, 'h3_context_by_shot': {shots[0].id: {'service': {}}}},
        steps_json=[{'shot_id': shot.id} for shot in shots])
    session.add(run); session.flush()
    calls = []
    def preflight(_session, _settings, shot, **kwargs):
        calls.append((shot.id, kwargs))
        return {'ready': not kwargs['require_visual'], 'findings': []}
    monkeypatch.setattr(cinematic, 'shot_preflight', preflight)
    request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(settings=settings))})
    with pytest.raises(AppError) as blocked:
        cinematic.approve_inputs(run.id, ApprovalRequest(operator='isolated-test'), request, session)
    assert blocked.value.code == 'production_inputs_not_ready'
    assert [options['require_visual'] for _, options in calls] == [False, True]
    assert [options['context_inputs'] for _, options in calls] == [True, False]


def test_cinematic_mixed_run_prepares_only_legacy_keyframe_before_video(session, settings, monkeypatch):
    project, shots, script = project_shots(session, 2)
    run = m.ProductionRun(project_id=project.id, script_revision_id=script.id, status='ready',
        snapshot_json={'options': {'seed': 17, 'width': 1024, 'height': 576}, 'context_production': True,
            'h3_context_by_shot': {shots[0].id: {'service': {}}}},
        steps_json=[{'shot_id': shot.id, 'inputs_hash': production.generation_inputs_hash(session, shot),
                     'stage': 'ready', 'attempt': 0, 'video_job_id': None, 'video_id': None, 'keyframe_id': None}
                    for shot in shots])
    session.add(run); session.flush()
    monkeypatch.setattr(production, 'shot_preflight', lambda *_args, **_kwargs: {'ready': True, 'findings': []})
    monkeypatch.setattr(production, 'production_context', lambda *_args: (None, None, script, {}, []))
    built = []
    def prepare_visual(_session, _settings, _project, payload):
        built.append(payload.shot_id)
        return SimpleNamespace(id='isolated-visual', status='needs_review')
    monkeypatch.setattr(production, 'build_visual', prepare_visual)
    coroutine = production.advance_run(SimpleNamespace(), settings, session, run)
    with pytest.raises(StopIteration):
        try:
            coroutine.send(None)
        finally:
            coroutine.close()
    assert built == [shots[1].id]
    assert run.status == 'waiting_visual_review'
    assert run.steps_json[0]['keyframe_id'] is None
    assert run.steps_json[1]['keyframe_id'] == 'isolated-visual'


def test_saved_quality_locks_only_context_modes_in_mixed_run(session, settings):
    from app.services.studio_quality_apply import apply_saved_quality
    project, shots, _ = project_shots(session, 2)
    high = QualitySettings(mode='high').model_dump()
    standard = QualitySettings(mode='standard').model_dump()
    run = m.StudioRun(project_id=project.id, status='paused',
        snapshot_json={'shot_ids': [shot.id for shot in shots], 'context_production': True,
            'h3_context_by_shot': {shots[0].id: {'service': {'h3_workflow': {'actual_steps': 30}}}},
            'options': {'quality': high, 'quality_by_shot': {shot.id: high for shot in shots}}},
        nodes_json=[{'id': 'video:' + shot.id, 'kind': 'video', 'target_id': shot.id, 'status': 'pending'} for shot in shots])
    session.add_all([run, m.QualityProfile(scope='project', target_id=project.id, revision=1, settings_json=standard),
                     m.QualityProfile(scope='shot', target_id=shots[0].id, revision=1, settings_json=high)])
    session.commit()
    result = apply_saved_quality(session, settings, project.id, expected_profile_revision=1)
    assert result['applied'] == []
    assert run.snapshot_json['h3_context_by_shot'][shots[0].id]['service']['h3_workflow']['actual_steps'] == 30

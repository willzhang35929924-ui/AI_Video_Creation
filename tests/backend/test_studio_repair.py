import pytest

from app import models as m
from app.models.studio import StudioRun


def add_stuck_run(client, *, visual_status='queued', run_status='running',
                  character_status='exception', character_code='capability_unverified',
                  capability_ready=False, character_child_id=None):
    factory = client.app.state.session_factory
    with factory() as session:
        project = m.Project(name='一键修复测试')
        session.add(project)
        session.flush()
        image_service = None
        if capability_ready:
            image_service = m.AIService(
                id='service_repair_test', module='character_generation', name='repair image',
                provider='comfyui', base_url='http://127.0.0.1:8189', enabled=True,
                default_model='flux-2-klein-4b-fp8.safetensors',
                settings_json={'candidate_model': 'flux-2-klein-4b-fp8.safetensors'},
                capabilities_json={'text_to_image': {'state': 'detected', 'model_id': 'flux-2-klein-4b-fp8.safetensors'}})
            session.add(image_service)
            session.flush()
        visual_id = 'visual_repair_test'
        session.add(m.ProductionVisual(
            id=visual_id,
            project_id=project.id,
            kind='scene_reference',
            status=visual_status,
            prompt='fixture only',
            snapshot_json={'service_snapshot': {
                'provider': 'comfyui',
                'is_cloud': False,
            }},
        ))
        session.add(StudioRun(
            id='studio_repair_test',
            project_id=project.id,
            status=run_status,
            snapshot_json={'options': {'allow_cloud_costs': False}, 'services': {
                'image': ({'service_id': image_service.id, 'service_revision': image_service.revision,
                           'provider': 'comfyui', 'model_id': 'flux-2-klein-4b-fp8.safetensors',
                           'settings': {'candidate_model': 'flux-2-klein-4b-fp8.safetensors'},
                           'is_cloud': False} if image_service else {})}},
            nodes_json=[
                {'id': 'scene:fixture', 'kind': 'scene', 'target_id': 'scene_fixture',
                 'status': 'running', 'child_id': visual_id, 'error': None, 'code': None},
                {'id': 'character:fixture', 'kind': 'character', 'target_id': 'character_fixture',
                 'status': character_status, 'child_id': character_child_id, 'error': 'capability unverified',
                 'code': character_code, 'route': {}},
            ],
        ))
        session.commit()
        return project.id, visual_id


def test_repair_dispatches_only_the_existing_queued_local_visual(client, monkeypatch):
    from app.services import cinematic_visuals, gpu_gate

    project_id, visual_id = add_stuck_run(client)
    calls = []

    async def idle():
        return {'state': 'idle', 'reason': 'health_unreachable_and_no_managed_runtime_process'}

    async def dispatch(_settings, factory, *, visual_id):
        calls.append(visual_id)
        with factory() as session:
            visual = session.get(m.ProductionVisual, visual_id)
            visual.status = 'submitted'
            visual.provider_job_id = 'comfy_prompt_receipt'
            session.commit()

    monkeypatch.setattr(gpu_gate, 'owned_model_status', idle)
    monkeypatch.setattr(cinematic_visuals, 'visual_tick', dispatch)

    response = client.post(f'/api/studio/projects/{project_id}/repair', json={})

    assert response.status_code == 200
    result = response.json()
    assert calls == [visual_id]
    assert result['gpu_gate']['state'] == 'idle'
    assert result['actions'][0]['code'] == 'visual_dispatched'
    assert result['actions'][0]['provider_job_id'] == 'comfy_prompt_receipt'
    assert result['blockers'][0]['code'] == 'capability_unverified'


def test_repair_does_not_retry_unknown_visual(client, monkeypatch):
    from app.services import cinematic_visuals, gpu_gate

    project_id, _ = add_stuck_run(client, visual_status='unknown')
    calls = []

    async def idle():
        return {'state': 'idle', 'reason': 'health_idle'}

    async def dispatch(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(gpu_gate, 'owned_model_status', idle)
    monkeypatch.setattr(cinematic_visuals, 'visual_tick', dispatch)

    response = client.post(f'/api/studio/projects/{project_id}/repair', json={})

    assert response.status_code == 200
    assert calls == []
    assert response.json()['actions'][0]['code'] == 'no_queued_visual'
    assert response.json()['blockers'][0]['code'] == 'capability_unverified'


def test_repair_does_not_submit_while_gpu_state_is_unknown(client, monkeypatch):
    from app.services import cinematic_visuals, gpu_gate

    project_id, _ = add_stuck_run(client)
    calls = []

    async def unknown():
        return {'state': 'unknown', 'reason': 'health_unreachable_while_managed_runtime_process_is_alive'}

    async def dispatch(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(gpu_gate, 'owned_model_status', unknown)
    monkeypatch.setattr(cinematic_visuals, 'visual_tick', dispatch)

    response = client.post(f'/api/studio/projects/{project_id}/repair', json={})

    assert response.status_code == 200
    assert calls == []
    assert response.json()['actions'][0]['code'] == 'gpu_health_unknown'


@pytest.mark.parametrize('run_status', ['running', 'completed_with_errors'])
def test_repair_requeues_capability_failure_and_advances_pipeline(client, monkeypatch, run_status):
    from app.services import gpu_gate, studio_pipeline

    project_id, _ = add_stuck_run(client, visual_status='approved', run_status=run_status, capability_ready=True)
    calls = []

    async def idle():
        return {'state': 'idle', 'reason': 'health_idle'}

    async def advance(_app, _settings, session, run):
        calls.append((run.status, [n['status'] for n in run.nodes_json if n['kind'] == 'character']))

    monkeypatch.setattr(gpu_gate, 'owned_model_status', idle)
    monkeypatch.setattr(studio_pipeline, 'advance_pipeline', advance)

    response = client.post(f'/api/studio/projects/{project_id}/repair', json={})

    assert response.status_code == 200, response.text
    result = response.json()
    assert calls == [('running', ['pending'])]
    assert result['run_status'] == 'running'
    assert any(action['code'] == 'character_nodes_requeued' and action['status'] == 'applied'
               for action in result['actions'])
    assert not any(blocker['code'] == 'capability_unverified' for blocker in result['blockers'])
    with client.app.state.session_factory() as session:
        run = session.get(StudioRun, 'studio_repair_test')
        character = next(n for n in run.nodes_json if n['kind'] == 'character')
        assert character['status'] == 'pending'
        assert character['history'][-1]['action'] == 'one_click_repair_requeued'


def test_repair_leaves_capability_failure_blocked_without_current_capability(client, monkeypatch):
    from app.services import gpu_gate, studio_pipeline

    project_id, _ = add_stuck_run(client, visual_status='approved', run_status='completed_with_errors')
    calls = []

    async def idle():
        return {'state': 'idle', 'reason': 'health_idle'}

    async def advance(*_args):
        calls.append(True)

    monkeypatch.setattr(gpu_gate, 'owned_model_status', idle)
    monkeypatch.setattr(studio_pipeline, 'advance_pipeline', advance)

    response = client.post(f'/api/studio/projects/{project_id}/repair', json={})

    assert response.status_code == 200
    result = response.json()
    assert calls == []
    assert result['run_status'] == 'completed_with_errors'
    assert result['blockers'][0]['code'] == 'capability_unverified'
    with client.app.state.session_factory() as session:
        run = session.get(StudioRun, 'studio_repair_test')
        assert next(n for n in run.nodes_json if n['kind'] == 'character')['status'] == 'exception'


def test_repair_never_requeues_unknown_character_request(client, monkeypatch):
    from app.services import gpu_gate, studio_pipeline

    project_id, _ = add_stuck_run(client, visual_status='approved', run_status='completed_with_errors',
                                  character_status='unknown', character_code='studio_dispatch_unknown',
                                  capability_ready=True, character_child_id='existing-submission')
    calls = []

    async def idle():
        return {'state': 'idle', 'reason': 'health_idle'}

    async def advance(*_args):
        calls.append(True)

    monkeypatch.setattr(gpu_gate, 'owned_model_status', idle)
    monkeypatch.setattr(studio_pipeline, 'advance_pipeline', advance)

    response = client.post(f'/api/studio/projects/{project_id}/repair', json={})

    assert response.status_code == 200
    assert calls == []
    assert response.json()['blockers'][0]['code'] == 'studio_dispatch_unknown'
    with client.app.state.session_factory() as session:
        run = session.get(StudioRun, 'studio_repair_test')
        character = next(n for n in run.nodes_json if n['kind'] == 'character')
        assert character['status'] == 'unknown'
        assert character['child_id'] == 'existing-submission'


@pytest.mark.parametrize('gpu_state', ['busy', 'unknown'])
def test_repair_does_not_requeue_character_while_gpu_not_idle(client, monkeypatch, gpu_state):
    from app.services import gpu_gate, studio_pipeline

    project_id, _ = add_stuck_run(client, visual_status='approved', run_status='completed_with_errors',
                                  capability_ready=True)
    calls = []

    async def status():
        return {'state': gpu_state, 'reason': 'test_'+gpu_state}

    async def advance(*_args):
        calls.append(True)

    monkeypatch.setattr(gpu_gate, 'owned_model_status', status)
    monkeypatch.setattr(studio_pipeline, 'advance_pipeline', advance)

    response = client.post(f'/api/studio/projects/{project_id}/repair', json={})

    assert response.status_code == 200
    result = response.json()
    assert calls == []
    assert result['run_status'] == 'completed_with_errors'
    assert any(action['code'] == 'character_repair_gpu_blocked' for action in result['actions'])
    with client.app.state.session_factory() as session:
        run = session.get(StudioRun, 'studio_repair_test')
        assert next(n for n in run.nodes_json if n['kind'] == 'character')['status'] == 'exception'

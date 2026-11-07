"""Prompt editing remains unbounded in review; cloud adapters guard their own ceilings."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import models as m
from app.schemas import GenerationRequest, RevisionRequest
from app.services import video_services


def test_generation_request_does_not_apply_a_shared_local_prompt_ceiling():
    long_prompt = '本地完整镜头输入：' + '景' * 20000
    request = GenerationRequest(prompt_override=long_prompt)
    revision = RevisionRequest(prompt_override=long_prompt, instruction='修改要求：' + '动' * 6000)
    assert request.prompt_override == long_prompt
    assert revision.prompt_override == long_prompt
    assert len(revision.instruction) > 4000


def test_direct_shot_and_structured_dialogue_editors_accept_long_local_video_text(client):
    from app.schemas_cinematic import LineIR, VisualInput
    from app.schemas_screenplay import LineInput, AudioInput
    text='完整输入'+'长'*40000
    project=client.post('/api/projects',json={'name':'长输入无共享上限'}).json()
    created=client.post(f"/api/projects/{project['id']}/shots",json={
        'order':1,'visual_prompt':text,'dialogue':text,'narration':text,'action':text,
        'negative_prompt':text,'audio_direction':text})
    assert created.status_code==201,created.text
    shot=created.json()
    assert shot['visual_prompt']==text
    edited=client.patch(f"/api/shots/{shot['id']}",json={'visual_prompt':text+'修订','dialogue':text})
    assert edited.status_code==200,edited.text
    assert edited.json()['visual_prompt']==text+'修订'
    assert LineInput(participant_id='role',text=text).text==text
    assert AudioInput(description=text).description==text
    assert LineIR(character_key='role',text=text).text==text
    assert VisualInput(kind='keyframe',shot_id=shot['id'],instructions=text).instructions==text


def test_review_accepts_and_persists_long_prompt_override_and_instruction(client, monkeypatch):
    project_id = shot_id = None
    with client.app.state.session_factory() as session:
        project = m.Project(name='长提示词审片回归')
        session.add(project)
        session.flush()
        script = m.ScriptRevision(project_id=project.id, revision=1, raw_text='回归测试', parsed_json={})
        session.add(script)
        session.flush()
        scene = m.Scene(project_id=project.id, script_revision_id=script.id, order=1, name='测试场景')
        session.add(scene)
        session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, scene_title='测试镜头')
        session.add(shot)
        session.flush()
        session.add(m.ShotDetail(shot_id=shot.id, scene_id=scene.id))
        session.commit()
        project_id, shot_id = project.id, shot.id

    captured = {}

    def capture_pipeline(_session, _settings, _project_id, options, *, prior_quality=None):
        captured.update(options)
        captured['prior_quality'] = prior_quality

    from app.services import studio_pipeline
    monkeypatch.setattr(studio_pipeline, 'create_pipeline', capture_pipeline)
    prompt = '自定义完整提示词：' + '场' * 12000
    instruction = '仅修改本镜头：' + '动作' * 2500
    response = client.post(
        f'/api/studio/projects/{project_id}/shots/{shot_id}/decision',
        json={'action': 'modify', 'instruction': instruction, 'prompt_override': prompt},
    )

    assert response.status_code == 200, response.text
    assert captured['prompt_overrides'][shot_id] == prompt
    assert captured['instructions'][shot_id] == instruction
    # This first review has no saved run/video quality to freeze or fabricate.
    assert captured['prior_quality'] is None
    with client.app.state.session_factory() as session:
        decision = session.scalar(
            select(m.StudioDecision).where(
                m.StudioDecision.project_id == project_id,
                m.StudioDecision.shot_id == shot_id,
            )
        )
        assert decision.instruction == instruction


def test_generic_cloud_video_prompt_limit_is_enforced_before_submission(settings, monkeypatch):
    provider = video_services.CloudVideoJobs(
        settings,
        {'model_id': 'configured-cloud-model', 'base_url': 'https://example.invalid', 'settings': {'max_prompt_characters': 7000}},
        'test-only-secret',
    )
    request = SimpleNamespace(
        prompt='x' * 7001,
        reference_assets=[],
        job_id='job-limit-test',
        duration_seconds=5,
        parameters={'width': 1280, 'height': 720},
        seed=1,
    )
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('oversized prompt must be blocked before network submission')

    monkeypatch.setattr(video_services, 'service_request', forbidden)
    from app.core.errors import AppError
    with pytest.raises(AppError) as error:
        asyncio.run(provider.submit(request))
    assert error.value.code == 'cloud_prompt_too_long'
    assert calls == []


def test_generic_cloud_video_accepts_exact_prompt_limit(settings, monkeypatch):
    provider = video_services.CloudVideoJobs(
        settings,
        {'model_id': 'configured-cloud-model', 'base_url': 'https://example.invalid', 'settings': {'max_prompt_characters': 7000}},
        'test-only-secret',
    )
    request = SimpleNamespace(
        prompt='x' * 7000,
        reference_assets=[],
        job_id='job-limit-test',
        duration_seconds=5,
        parameters={'width': 1280, 'height': 720},
        seed=1,
    )
    calls = []

    async def accepted(_settings, _snapshot, method, path, _secret, **kwargs):
        calls.append((method, path, kwargs['json']['prompt']))
        return httpx.Response(
            200,
            json={'id': 'provider-task-1'},
            request=httpx.Request('POST', 'https://example.invalid/video/jobs'),
        )

    monkeypatch.setattr(video_services, 'service_request', accepted)
    result = asyncio.run(provider.submit(request))
    assert result.provider_job_id == 'provider-task-1'
    assert calls == [('POST', '/video/jobs', 'x' * 7000)]


@pytest.mark.parametrize('limit', [None, 25000])
def test_custom_cloud_uses_its_own_optional_limit(settings, monkeypatch, limit):
    provider = video_services.CloudVideoJobs(settings, {
        'model_id': 'configured-cloud-model', 'base_url': 'https://example.invalid',
        'settings': {'max_prompt_characters': limit}}, 'test-only-secret')
    prompt = '景' * 20000
    request = SimpleNamespace(prompt=prompt, reference_assets=[], job_id='job-no-fixed-ceiling',
        duration_seconds=5, parameters={'width': 1280, 'height': 720}, seed=1)
    calls = []
    async def accepted(_settings, _snapshot, method, path, _secret, **kwargs):
        calls.append(kwargs['json']['prompt'])
        return httpx.Response(200, json={'id': 'provider-task-1'},
                              request=httpx.Request('POST', 'https://example.invalid/video/jobs'))
    monkeypatch.setattr(video_services, 'service_request', accepted)
    asyncio.run(provider.submit(request))
    assert calls == [prompt]


@pytest.mark.parametrize('limit', [0, -1, True, 1.5, '7000'])
def test_configured_cloud_limit_requires_positive_integer(limit):
    from app.core.errors import AppError
    from app.services.ai_services import validate_configuration
    with pytest.raises(AppError) as error:
        validate_configuration('video_generation', 'video_api', 'https://example.invalid',
                               {'video_contract': 'video-jobs-v1', 'max_prompt_characters': limit})
    assert error.value.code == 'invalid_prompt_limit'


def test_local_service_cannot_enable_cloud_prompt_interception():
    from app.core.errors import AppError
    from app.services.ai_services import validate_configuration
    with pytest.raises(AppError) as error:
        validate_configuration('video_generation', 'comfyui', 'http://127.0.0.1:8189',
                               {'max_prompt_characters': 7000})
    assert error.value.code == 'prompt_limit_not_configurable'



def test_local_h3_workflow_serializes_full_long_prompt(settings, tmp_path, monkeypatch):
    # Transport-only test: executes the actual adapter but never reaches a GPU.
    import hashlib
    import json
    from dataclasses import replace
    from pathlib import Path
    from PIL import Image
    from app.services import providers
    workflow = Path(__file__).resolve().parents[2] / 'workflows/h3_r2v_character_v1.json'
    config = replace(settings, h3_demo_mode=False, h3_workflow_api_json=workflow)
    image = tmp_path / 'reference.png'
    Image.new('RGB', (64, 64), 'white').save(image)
    prompt = '本地保留全部文字：' + '景' * 30000
    captured = []
    def handler(request):
        if request.url.path == '/upload/image':
            return httpx.Response(200, json={'name': 'reference.png', 'subfolder': 'video_factory'})
        if request.url.path == '/prompt':
            captured.append(json.loads(request.content)['prompt'])
            return httpx.Response(200, json={'prompt_id': 'isolated-transport-only'})
        raise AssertionError('Unexpected transport URL: '+str(request.url))
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(providers, 'PROJECT_ROOT', tmp_path)
    client_origins = []
    def transport_client(origins, **kwargs):
        client_origins.append(origins)
        return original(transport=transport, trust_env=False, follow_redirects=False, **kwargs)
    monkeypatch.setattr(providers, 'local_service_client', transport_client)
    payload = providers.VideoGenerationRequest(job_id='local-long-prompt', project_id='p', shot_id='s',
        prompt=prompt, negative_prompt='', duration_seconds=5, aspect_ratio='16:9', seed=1,
        parameters={'width':1024, 'height':576}, output_dir=tmp_path/'output', reference_assets=[
            {'asset_id':'image', 'absolute_path':str(image), 'sha256':hashlib.sha256(image.read_bytes()).hexdigest()}])
    result = asyncio.run(providers.ComfyUIH3Provider(config).submit(payload))
    assert result.provider_job_id == 'isolated-transport-only'
    assert client_origins and all(origins == [config.comfyui_base_url] for origins in client_origins)
    mapping = json.loads(workflow.read_text(encoding='utf-8'))['_video_factory']['prompt_path']
    body = captured[0]
    for key in mapping:
        body = body[key]
    assert body == prompt


def test_custom_cloud_prompt_limit_saves_through_actual_service_api(client, settings):
    response = client.post('/api/settings/ai-services', json={'module':'video_generation',
        'provider':'video_api', 'name':'Configured ceiling contract test',
        'base_url':'https://example.invalid/v1', 'default_model':'configured-cloud-model',
        'settings':{'video_contract':'video-jobs-v1', 'max_prompt_characters':9000}})
    assert response.status_code == 201, response.text
    assert response.json()['settings']['max_prompt_characters'] == 9000
    with client.app.state.session_factory() as session:
        _, snapshot, provider = video_services.select_video_service(session, settings, 'none', response.json()['id'])
        assert snapshot['settings']['max_prompt_characters'] == 9000
        assert provider.snapshot['settings']['max_prompt_characters'] == 9000

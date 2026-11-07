import json
import secrets

from sqlalchemy import select
from app.models import AIService
from app.core.secrets import protect, reveal, redact


def make_service(client, **kwargs):
    response = client.post('/api/settings/ai-services', json={'module': 'character_generation', 'provider': 'openai_compatible', 'name': 'Credential storage contract only', 'base_url': 'https://example.com/v1', 'default_model': 'manually-entered-model', **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def test_actual_windows_dpapi_roundtrip():
    secret = 'storage-test-' + secrets.token_urlsafe(24)
    encrypted = protect(secret)
    assert secret.encode() not in encrypted
    assert reveal(encrypted) == secret


def test_key_is_never_returned_and_unmodified_edit_preserves_it(client):
    secret = 'storage-contract-' + secrets.token_urlsafe(24)
    item = make_service(client, api_key=secret)
    assert item['has_api_key'] and item['api_key_mask'] == '***'
    assert secret not in json.dumps(item)
    changed = client.patch('/api/settings/ai-services/' + item['id'], json={'name': 'Changed name only'}).json()
    assert changed['has_api_key']
    with client.app.state.session_factory() as session:
        row = session.get(AIService, item['id'])
        assert reveal(row.encrypted_secret) == secret
    listing = client.get('/api/settings/ai-services').text
    assert secret not in listing and 'encrypted_secret' not in listing
    refused = client.patch('/api/settings/ai-services/' + item['id'], json={'clear_api_key': True})
    assert refused.status_code == 409
    assert client.patch('/api/settings/ai-services/' + item['id'], json={'clear_api_key': True, 'confirm_clear': True}).json()['has_api_key'] is False


def test_module_defaults_are_independent_and_save_does_not_connect(client):
    before = client.get('/api/settings/ai-services').json()
    assert all(item['settings'].get('timeout') is None for item in before)
    video_default = next(row['id'] for row in before if row['module'] == 'video_generation' and row['is_default'])
    item = make_service(client)
    assert item['last_test'] == {} and item['capabilities'] == {}
    assert item['models'] == []
    assert client.post('/api/settings/ai-services/' + item['id'] + '/set-default').status_code == 200
    after = client.get('/api/settings/ai-services').json()
    assert next(row['id'] for row in after if row['module'] == 'video_generation' and row['is_default']) == video_default


def test_url_and_config_secret_rejection(client):
    response = client.post('/api/settings/ai-services', json={'module': 'character_generation', 'provider': 'openai_compatible', 'name': 'Invalid', 'base_url': 'https://example.com/?api_key=secret'})
    assert response.status_code == 422
    response = client.post('/api/settings/ai-services', json={'module': 'character_generation', 'provider': 'comfyui', 'name': 'Invalid', 'base_url': 'http://127.0.0.1:8189', 'settings': {'headers': {'Authorization': 'no'}}})
    assert response.status_code == 422
    assert redact({'Authorization': 'secret', 'error': 'Bearer abcdefg'}) == {'Authorization': '***', 'error': 'Bearer ***'}


def test_real_offline_connection_and_manual_model_source(client):
    item = client.post('/api/settings/ai-services', json={'module': 'character_generation', 'provider': 'comfyui', 'name': 'Closed local socket negative test', 'base_url': 'http://127.0.0.1:1', 'models': ['manual-model'], 'settings': {'timeout': 3, 'concurrency': 1}}).json()
    tested = client.post('/api/settings/ai-services/' + item['id'] + '/test').json()
    assert tested['last_test']['status'] == 'failed'
    assert tested['last_test']['http_status'] is None
    assert tested['models'][0]['source'] == 'manual'
    assert tested['last_success_at'] is None
def test_unbounded_timeout_is_accepted_but_malformed_numeric_configuration_is_rejected(client):
    response=client.post('/api/settings/ai-services',json={'module':'character_generation','provider':'comfyui','name':'invalid isolated contract','base_url':'http://127.0.0.1:1','settings':{'timeout':None}})
    assert response.status_code==201, response.text
    invalid=client.post('/api/settings/ai-services',json={'module':'character_generation','provider':'comfyui','name':'invalid isolated contract','base_url':'http://127.0.0.1:1','settings':{'timeout':'not-a-number'}})
    assert invalid.status_code==422
    assert invalid.json()['error']['code']=='invalid_numeric_settings'


def test_unlimited_timeout_is_valid_and_no_longer_has_a_600_second_ceiling(client):
    from app.services.ai_services import validate_configuration

    validate_configuration('character_generation', 'comfyui', 'http://127.0.0.1:8189', {'timeout': None})
    validate_configuration('character_generation', 'comfyui', 'http://127.0.0.1:8189', {'timeout': 3600})
    response = client.post('/api/settings/ai-services', json={
        'module': 'character_generation', 'provider': 'comfyui', 'name': 'Unlimited generation timeout',
        'base_url': 'http://127.0.0.1:1', 'settings': {'timeout': None},
    })
    assert response.status_code == 201, response.text
    assert response.json()['settings']['timeout'] is None


def test_comfyui_client_timeout_defaults_to_unbounded(settings):
    assert settings.comfyui_client_timeout_seconds is None


def test_service_request_builds_an_httpx_client_without_timeout_when_profile_is_unbounded(settings, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.services import ai_services

    observed = {}

    class FakeClient:
        def __init__(self, **kwargs):
            observed.update(kwargs)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return None
        async def request(self, *_args, **_kwargs):
            return 'response'

    monkeypatch.setattr(ai_services.httpx, 'AsyncClient', FakeClient)
    snapshot = {'provider': 'comfyui', 'base_url': 'http://127.0.0.1:8189', 'settings': {'timeout': None}}
    assert asyncio.run(ai_services.service_request(settings, snapshot, 'POST', '/prompt')) == 'response'
    assert observed['timeout'] is None


def test_comfy_probe_detects_the_selected_candidate_model_not_other_checkpoints(client,monkeypatch):
    """Controlled inventory response; does not claim real model execution."""
    import httpx
    from app.services import ai_services
    nodes={name:{} for name in ('UNETLoader','CLIPLoader','VAELoader','CLIPTextEncode','ConditioningZeroOut','CFGGuider','KSamplerSelect','RandomNoise','EmptyFlux2LatentImage','Flux2Scheduler','SamplerCustomAdvanced','VAEDecode','SaveImage','KSampler')}
    for name,field,choices in [('UNETLoader','unet_name',['flux-2-klein-4b-fp8.safetensors']),('CLIPLoader','clip_name',['qwen_3_4b.safetensors']),('VAELoader','vae_name',['flux2-vae.safetensors']),('CheckpointLoaderSimple','ckpt_name',['sd_xl_base_1.0.safetensors'])]:
        nodes[name]={'input':{'required':{field:[choices]}}}
    async def inventory(*args,**kwargs):return httpx.Response(200,json=nodes,request=httpx.Request('GET','http://127.0.0.1:1/object_info'))
    monkeypatch.setattr(ai_services,'service_request',inventory)
    options={'candidate_model':'uninstalled-candidate','text_encoder':'qwen_3_4b.safetensors','vae':'flux2-vae.safetensors'}
    result=client.post('/api/settings/ai-services',json={'module':'character_generation','provider':'comfyui','name':'Selected capability contract','base_url':'http://127.0.0.1:1','default_model':'flux-2-klein-4b-fp8.safetensors','settings':options})
    item=result.json();assert result.status_code==201,result.text
    failed=client.post('/api/settings/ai-services/'+item['id']+'/test').json()
    assert 'text_to_image' not in failed['capabilities']
    options['candidate_model']='flux-2-klein-4b-fp8.safetensors'
    assert client.patch('/api/settings/ai-services/'+item['id'],json={'settings':options}).status_code==200
    detected=client.post('/api/settings/ai-services/'+item['id']+'/test').json()
    assert detected['capabilities']['text_to_image']['model_id']==options['candidate_model']
    assert detected['capabilities']['text_to_image']['state']=='detected'


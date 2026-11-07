"""Isolated HTTP/profile contracts; no model inference or external network calls."""
import asyncio
import socket
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.errors import AppError
from app.core import trusted_service_origins as origins
from app.services import ai_services


def create_local(client, **changes):
    body = {'module': 'video_generation', 'provider': 'comfyui', 'name': 'My local node',
            'base_url': 'http://host.docker.internal:8788',
            'default_model': 'minimax_h3_ref2va_pruned_w6a8.safetensors',
            'settings': {'local_service_origin': 'http://host.docker.internal:8788', 'workflow_profile': 'context_ir'}}
    body.update(changes)
    return client.post('/api/settings/ai-services', json=body)


def test_profile_confirmation_persists_without_environment_and_cannot_move(client, monkeypatch):
    monkeypatch.delenv('TRUSTED_LOCAL_SERVICE_ORIGINS', raising=False)
    response = create_local(client)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item['trusted_local_endpoint'] is True
    assert item['settings']['h3_context_ir'] is True
    assert item['settings']['local_service_origin'] == 'http://host.docker.internal:8788'
    assert 'workflow_path' not in item['settings']
    assert next(row for row in client.get('/api/settings/ai-services').json() if row['id'] == item['id'])['settings'] == item['settings']
    refused = client.patch('/api/settings/ai-services/' + item['id'], json={'base_url': 'http://host.docker.internal:9999'})
    assert refused.status_code == 422
    assert refused.json()['error']['code'] == 'local_origin_confirmation_mismatch'
    changed = client.patch('/api/settings/ai-services/' + item['id'], json={
        'base_url': 'http://192.168.1.15:8788', 'settings': {**item['settings'], 'local_service_origin': 'http://192.168.1.15:8788'}})
    assert changed.status_code == 200
    assert changed.json()['trusted_local_endpoint'] is True


@pytest.mark.parametrize('value', ['https://api.example.com', 'http://169.254.169.254',
    'http://[fd00:ec2::254]', 'http://100.100.100.200', 'http://user:pass@model:8788',
    'http://model:8788/?key=secret', 'http://model:8788/v1'])
def test_profile_trust_rejects_public_metadata_credentials_and_nonorigin(client, value):
    response = create_local(client, base_url=value, settings={'local_service_origin': value})
    assert response.status_code == 422


@pytest.mark.parametrize('ip', ['169.254.169.254', '8.8.8.8', 'fd00:ec2::254'])
def test_confirmed_name_cannot_resolve_to_metadata_or_public_ip(monkeypatch, ip):
    monkeypatch.setattr(origins.socket, 'getaddrinfo', lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 8788))])
    with pytest.raises(AppError) as error:
        origins.pin_local_service_url('http://inference:8788/object_info')
    assert error.value.code == 'unsafe_local_service_address'


def test_local_request_pins_dns_retains_host_and_refuses_redirect(settings, monkeypatch):
    seen = {}
    monkeypatch.setattr(origins.socket, 'getaddrinfo', lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.65.254', 8788))])
    class Client:
        def __init__(self, **kwargs): seen.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def request(self, method, url, **kwargs):
            seen.update(url=url, request=kwargs)
            return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest/meta-data'}, request=httpx.Request(method, url))
    monkeypatch.setattr(ai_services.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(ai_services, 'pin_public_url', lambda *_args: pytest.fail('local trust must not change public import pinning'))
    snapshot = {'provider': 'comfyui', 'base_url': 'http://host.docker.internal:8788',
                'settings': {'local_service_origin': 'http://host.docker.internal:8788'}}
    with pytest.raises(AppError) as error:
        asyncio.run(ai_services.service_request(settings, snapshot, 'GET', '/object_info'))
    assert error.value.code == 'service_redirect_refused'
    assert seen['url'] == 'http://192.168.65.254:8788/object_info'
    assert seen['request']['headers']['Host'] == 'host.docker.internal:8788'
    assert seen['follow_redirects'] is False and seen['trust_env'] is False


def test_container_defaults_and_builtin_graph_are_server_paths(client, monkeypatch):
    monkeypatch.setenv('VIDEO_FACTORY_CONTAINER', '1')
    options = client.get('/api/settings/ai-service-options').json()
    assert options['defaults']['context_ir'] == 'http://host.docker.internal:8788'
    assert options['defaults']['comfyui'] == 'http://host.docker.internal:8189'
    response = create_local(client, settings={'local_service_origin': 'http://host.docker.internal:8788', 'workflow_profile': 'native_h3_r2v', 'workflow_path': 'C:/not-in-container/old.json'})
    assert response.status_code == 201
    assert response.json()['settings']['workflow_path'] == str(ai_services.PROJECT_ROOT / 'workflows/h3_r2v_character_v1.json')
    monkeypatch.delenv('VIDEO_FACTORY_CONTAINER')
    assert client.get('/api/settings/ai-service-options').json()['defaults']['context_ir'] == 'http://127.0.0.1:8788'


def test_reachable_comfy_with_missing_workflow_is_not_reported_ready(client, monkeypatch):
    async def inventory(*_args, **_kwargs):
        return httpx.Response(200, json={}, request=httpx.Request('GET', 'http://127.0.0.1:1/object_info'))
    monkeypatch.setattr(ai_services, 'service_request', inventory)
    item = create_local(client, base_url='http://127.0.0.1:1', settings={'workflow_profile': 'native_h3_r2v'}).json()
    result = client.post('/api/settings/ai-services/' + item['id'] + '/test').json()
    assert result['last_test']['status'] == 'reachable'
    assert result['last_test']['generation_ready'] is False
    assert result['last_test']['diagnostic_code'] == 'comfy_workflow_not_ready'
    assert not result['capabilities']


def test_context_probe_keeps_not_ready_runtime_checks_and_requires_real_receipt(client, monkeypatch):
    runtime = {'schema_version': 1, 'service': 'video-factory-native-context-runtime-v1',
               'watcher_running': True, 'source_baseline': {'ok': True},
               'profile_id': 'local-h3ir-qwen4b-v3', 'memory_policy': 'runtime_owned',
               'ready': False, 'checks': [{'name': 'models', 'ok': False}], 'qwen_health': {
                   'service': 'video-factory-h3ir-qwen4b-v1', 'profile': 'h3ir-qwen4b-32k',
                   'model_id': 'Qwen/Qwen3-VL-4B-Instruct', 'offline': True, 'max_model_len': 32768,
                   'busy': False, 'loaded': False, 'script_sha256': 'a' * 64,
                   'inference_phase': 'idle', 'gpu_release_status': 'not_used', 'last_gpu_cleanup': None}}
    calls = []
    async def inventory(_settings, _snapshot, _method, endpoint, *_args, **_kwargs):
        calls.append(endpoint)
        return httpx.Response(200, json=runtime if endpoint == '/video_factory/runtime' else {}, request=httpx.Request('GET', 'http://node:8788' + endpoint))
    monkeypatch.setattr(ai_services, 'service_request', inventory)
    item = create_local(client).json()
    result = client.post('/api/settings/ai-services/' + item['id'] + '/test').json()['last_test']
    assert result['diagnostic_code'] == 'context_ir_runtime_not_ready'
    assert result['runtime']['checks'] == runtime['checks']
    assert result['status'] == 'reachable' and result['capabilities'] == {}
    assert calls == ['/object_info', '/video_factory/runtime']
    runtime['ready'] = True
    result = client.post('/api/settings/ai-services/' + item['id'] + '/test').json()['last_test']
    assert result['status'] == 'connected'
    assert result['capabilities']['reference_to_video']['state'] == 'detected'


def test_pinned_local_client_covers_uploads_queries_and_every_request(monkeypatch):
    calls, lookups = [], []
    def resolve(*args, **_kwargs):
        lookups.append(args[0])
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.20', 8788))]
    monkeypatch.setattr(origins.socket, 'getaddrinfo', resolve)
    async def responder(request):
        calls.append((str(request.url), dict(request.headers), dict(request.extensions), await request.aread()))
        return httpx.Response(200, json={'ok': True})
    monkeypatch.setattr(origins.httpx, 'AsyncHTTPTransport', lambda **_kwargs: httpx.MockTransport(responder))
    async def exercise():
        async with origins.local_service_client(['http://model.local:8788'], follow_redirects=True) as client:
            await client.post('http://model.local:8788/upload/image', content=b'actual-request-body')
            await client.get('http://model.local:8788/view', params={'filename': 'test image.png', 'type': 'output'})
            with pytest.raises(AppError) as error:
                await client.get('http://model.local:9999/queue')
            assert error.value.code == 'service_origin_changed'
    asyncio.run(exercise())
    assert len(calls) == len(lookups) == 2
    assert calls[0][0] == 'http://192.168.1.20:8788/upload/image'
    assert calls[0][1]['host'] == 'model.local:8788'
    assert calls[0][2]['sni_hostname'] == 'model.local'
    assert calls[0][3] == b'actual-request-body'
    assert urlsplit(calls[1][0]).netloc == '192.168.1.20:8788'
    assert parse_qs(urlsplit(calls[1][0]).query) == {'filename': ['test image.png'], 'type': ['output']}


def test_pinned_client_cannot_follow_redirect_or_override_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(origins.socket, 'getaddrinfo', lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.20', 8788))])
    def responder(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest/meta-data'})
    monkeypatch.setattr(origins.httpx, 'AsyncHTTPTransport', lambda **_kwargs: httpx.MockTransport(responder))
    async def exercise():
        async with origins.local_service_client(['http://model:8788'], follow_redirects=True) as client:
            with pytest.raises(AppError) as error:
                await client.get('http://model:8788/queue', follow_redirects=True)
            assert error.value.code == 'service_redirect_refused'
    asyncio.run(exercise())
    assert len(calls) == 1
    with pytest.raises(ValueError):
        origins.local_service_client(['http://model:8788'], transport=httpx.MockTransport(responder))

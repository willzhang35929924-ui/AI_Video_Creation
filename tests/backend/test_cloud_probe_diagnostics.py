"""Isolated probe contracts only; these tests do not contact a provider."""
import asyncio
import ssl
from types import SimpleNamespace

import httpx
import pytest

from app.core.errors import AppError
from app.services import ai_services


def service(provider='video_api', module='video_generation'):
    return SimpleNamespace(id='isolated', revision=1, provider=provider, name='isolated',
        module=module, base_url='https://example.invalid/v1', default_model='model',
        settings_json={'video_contract': 'video-jobs-v1'}, encrypted_secret=None)


def run_probe(monkeypatch, response=None, error=None, profile=None):
    calls = []
    async def request(_settings, _snapshot, method, endpoint, secret, **kwargs):
        calls.append((method, endpoint, kwargs.get('client_timeout')))
        if error:
            raise error
        return response
    monkeypatch.setattr(ai_services, 'service_request', request)
    result = asyncio.run(ai_services.probe_service(profile or service(), SimpleNamespace()))
    assert calls == [('GET', '/models', 10.0)]
    return result


def response(status, payload=None):
    return httpx.Response(status, json=payload if payload is not None else {},
        request=httpx.Request('GET', 'https://example.invalid/v1/models'))


@pytest.mark.parametrize(('status', 'code'), [(401, 'authentication_failed'), (403, 'permission_denied'),
    (429, 'rate_limited'), (500, 'provider_error'), (503, 'provider_error'), (302, 'redirect_not_followed')])
def test_http_failures_have_specific_safe_diagnostics(monkeypatch, status, code):
    result = run_probe(monkeypatch, response(status, {'detail': 'sensitive response should not appear'}))
    assert result['status'] == 'failed'
    assert result['http_status'] == status and result['diagnostic_code'] == code
    assert 'sensitive' not in str(result)


@pytest.mark.parametrize('status', [404, 405])
def test_missing_models_endpoint_is_reachable_not_authenticated(monkeypatch, status):
    result = run_probe(monkeypatch, response(status))
    assert result['status'] == 'reachable'
    assert result['diagnostic_code'] == 'model_list_unavailable'
    assert '认证和生成能力均未验证' in result['error']
    assert result['video_contract']['state'] == 'unverified'


def test_models_success_does_not_verify_video_generation(monkeypatch):
    result = run_probe(monkeypatch, response(200, {'data': [{'id': 'model'}]}))
    assert result['status'] == 'connected' and result['models'][0]['id'] == 'model'
    assert result['video_contract']['state'] == 'unverified'
    assert not result['capabilities']
    assert '不证明' in result['notice']


@pytest.mark.parametrize(('error', 'code'), [
    (AppError(422, 'dns_resolution_failed', '无法解析目标域名'), 'dns_resolution_failed'),
    (httpx.ConnectTimeout('private URL'), 'timeout'),
    (httpx.ConnectError('private URL'), 'connection_failed'),
    (ValueError('private response'), 'invalid_response'),
])
def test_network_and_parse_failures_do_not_expose_exception(monkeypatch, error, code):
    result = run_probe(monkeypatch, error=error)
    assert result['diagnostic_code'] == code
    assert 'private' not in str(result)


def test_tls_error_in_chained_connection_error(monkeypatch):
    error = httpx.ConnectError('private URL')
    error.__cause__ = ssl.SSLCertVerificationError('certificate detail')
    result = run_probe(monkeypatch, error=error)
    assert result['diagnostic_code'] == 'tls_error'
    assert 'certificate detail' not in str(result)


def test_unexpected_json_shape_is_not_a_success(monkeypatch):
    result = run_probe(monkeypatch, response(200, {'data': 'incorrect'}))
    assert result['status'] == 'failed' and result['diagnostic_code'] == 'invalid_response'

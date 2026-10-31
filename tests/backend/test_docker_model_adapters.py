import asyncio
import errno
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from app.core.errors import AppError
from app.services import gpu_gate, runtime_registry, screenplay_runtime, voice_library, reference_video_analysis


@pytest.fixture
def container(monkeypatch):
    monkeypatch.setenv('VIDEO_FACTORY_CONTAINER', '1')
    monkeypatch.setenv('LOCAL_SCREENPLAY_BASE_URL', 'http://host.docker.internal:8771/v1')
    monkeypatch.setenv('TRUSTED_LOCAL_SERVICE_ORIGINS', 'http://host.docker.internal:8771')
    monkeypatch.delenv('LOCAL_SCREENPLAY_EXPECTED_WORKSPACE', raising=False)


def health():
    return {'service': 'video-factory-screenplay-qwen-v1', 'model_id': screenplay_runtime.MODEL,
            'offline': True, 'workspace': 'D:/factory', 'busy': False, 'loaded': False,
            'script_sha256': hashlib.sha256(screenplay_runtime.SCRIPT.read_bytes()).hexdigest()}


class Client:
    result = None
    error = None
    urls = []
    def __init__(self, *args, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def get(self, url):
        self.urls.append(url)
        if self.error: raise self.error
        result = self.result
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)


def test_factory_requires_explicit_trusted_origin(container, monkeypatch):
    assert screenplay_runtime.factory_base_url() == 'http://host.docker.internal:8771/v1'
    monkeypatch.setenv('TRUSTED_LOCAL_SERVICE_ORIGINS', '')
    with pytest.raises(AppError) as error: screenplay_runtime.factory_base_url()
    assert error.value.code == 'screenplay_local_untrusted'
    monkeypatch.setenv('LOCAL_SCREENPLAY_BASE_URL', 'http://user:pass@host.docker.internal:8771/v1')
    with pytest.raises(AppError): screenplay_runtime.factory_base_url()
    monkeypatch.setenv('LOCAL_SCREENPLAY_BASE_URL', '')
    with pytest.raises(AppError) as error: screenplay_runtime.factory_base_url()
    assert error.value.code == 'screenplay_local_not_configured'


def test_factory_health_identity_and_workspace(container, monkeypatch):
    Client.result, Client.error, Client.urls = health(), None, []
    monkeypatch.setattr(screenplay_runtime.httpx, 'AsyncClient', Client)
    monkeypatch.setenv('LOCAL_SCREENPLAY_EXPECTED_WORKSPACE', r'D:\factory')
    asyncio.run(screenplay_runtime.ensure_local_service(screenplay_runtime.factory_snapshot()))
    assert Client.urls == ['http://host.docker.internal:8771/health']
    Client.result = {**health(), 'script_sha256': 'wrong'}
    with pytest.raises(AppError) as error:
        asyncio.run(screenplay_runtime.ensure_local_service(screenplay_runtime.factory_snapshot()))
    assert error.value.code == 'screenplay_local_identity'
    Client.result = {**health(), 'workspace': 'D:/other'}
    with pytest.raises(AppError): asyncio.run(screenplay_runtime.ensure_local_service(screenplay_runtime.factory_snapshot()))


def test_factory_only_probes_its_own_protocol(container, monkeypatch):
    monkeypatch.setattr(screenplay_runtime, '_ensure_container', lambda *_: pytest.fail('unrelated services must not probe factory'))
    asyncio.run(screenplay_runtime.ensure_local_service({'provider': 'local_llm', 'base_url': 'https://example.com/v1', 'model_id': 'other'}))


def test_container_gpu_gate_fails_closed(container, monkeypatch):
    monkeypatch.setattr(gpu_gate.httpx, 'AsyncClient', Client)
    Client.result, Client.error = health(), None
    assert asyncio.run(gpu_gate.owned_model_status())['state'] == 'idle'
    Client.result = {**health(), 'loaded': True}
    assert asyncio.run(gpu_gate.owned_model_status())['state'] == 'busy'
    Client.result = {**health(), 'script_sha256': 'wrong'}
    assert asyncio.run(gpu_gate.owned_model_status())['state'] == 'unknown'
    Client.error = httpx.ConnectTimeout('unreachable')
    assert asyncio.run(gpu_gate.owned_model_status())['state'] == 'unknown'
    try:
        raise OSError(errno.ECONNREFUSED, 'refused')
    except OSError as cause:
        try: raise httpx.ConnectError('refused') from cause
        except httpx.ConnectError as error: Client.error = error
    assert asyncio.run(gpu_gate.owned_model_status())['state'] == 'idle'
    Client.error = None
    monkeypatch.setenv('LOCAL_SCREENPLAY_BASE_URL', '')
    assert asyncio.run(gpu_gate.owned_model_status())['reason'] == 'no_managed_container_factory'


def test_comfy_gpu_preflight_requires_release_or_positive_independence(container, settings, monkeypatch):
    from app.services import video_quality
    calls = []
    async def release(_settings):
        calls.append('release')
        return {'state': 'released'}
    monkeypatch.setattr(video_quality, 'confirm_gpu_release', release)
    monkeypatch.setattr(gpu_gate.httpx, 'AsyncClient', Client)
    Client.error, Client.result = None, {}
    assert asyncio.run(gpu_gate.prepare_local_llm_gpu(settings)) == {'state':'released'}
    assert calls == ['release']
    monkeypatch.setenv('LOCAL_LLM_GPU_INDEPENDENT', 'true')
    Client.error = httpx.ConnectTimeout('not contacted')
    assert asyncio.run(gpu_gate.prepare_local_llm_gpu(settings))['state'] == 'independent_gpu_declared'
    monkeypatch.delenv('LOCAL_LLM_GPU_INDEPENDENT')
    with pytest.raises(httpx.ConnectTimeout): asyncio.run(gpu_gate.prepare_local_llm_gpu(settings))
    try:
        raise OSError(errno.ECONNREFUSED, 'refused')
    except OSError as cause:
        try: raise httpx.ConnectError('refused') from cause
        except httpx.ConnectError as error: Client.error = error
    assert asyncio.run(gpu_gate.prepare_local_llm_gpu(settings))['state'] == 'comfy_connection_refused'
    assert calls == ['release']


def test_windows_runtime_is_not_executed_in_container(container, settings):
    for family in ('cosyvoice3', 'indextts25', 'latentsync15', 'seedvr2'):
        status = runtime_registry.manifest_status(family, settings)
        assert status['state'] == 'unavailable' and '容器' in status['reasons'][0]
        with pytest.raises(AppError): runtime_registry.manifest_status(family, settings, require_verified=True)


def test_voice_directory_confined_to_imports(container, settings, monkeypatch, tmp_path):
    root = tmp_path / 'imports' / 'recordings'
    root.mkdir(parents=True)
    good = root / 'voice.wav'; good.write_bytes(b'voice')
    other = tmp_path / 'private.wav'; other.write_bytes(b'private')
    monkeypatch.setenv('LOCAL_RECORDINGS_DIR', str(root))
    monkeypatch.setattr(voice_library, 'import_bytes', lambda session, settings, filename, data, **kw: (filename, data))
    assert voice_library.import_path(None, settings, str(good)) == ('voice.wav', b'voice')
    with pytest.raises(AppError) as error: voice_library.import_path(None, settings, str(other))
    assert error.value.code == 'voice_source_scope'
    monkeypatch.setenv('LOCAL_RECORDINGS_DIR', str(tmp_path))
    with pytest.raises(AppError) as error: voice_library.source_directory(settings)
    assert error.value.code == 'voice_source_configuration'


def test_missing_factory_blocks_before_reference_attempt(container, monkeypatch):
    monkeypatch.setenv('LOCAL_SCREENPLAY_BASE_URL', '')
    item = SimpleNamespace(receipt_json={})
    session = SimpleNamespace(commit=lambda: pytest.fail('must not persist a request attempt'))
    with pytest.raises(AppError) as error:
        asyncio.run(reference_video_analysis.local_request(session, item, '/factory/transcribe', {}, 'audio'))
    assert error.value.code == 'screenplay_local_not_configured'


@pytest.mark.parametrize('endpoint', ['/v1/chat/completions', '/factory/transcribe'])
def test_reference_requests_use_the_trusted_factory_and_keep_receipt(container, monkeypatch, endpoint):
    requests = []
    class PostClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, json):
            requests.append((url, json))
            return SimpleNamespace(status_code=200, content=b'{"ok":true}', json=lambda: {'ok':True})
    monkeypatch.setattr(reference_video_analysis.httpx, 'AsyncClient', PostClient)
    commits = []
    session = SimpleNamespace(commit=lambda: commits.append(True))
    item = SimpleNamespace(receipt_json={})
    assert asyncio.run(reference_video_analysis.local_request(session, item, endpoint, {'sample':'value'}, 'test')) == {'ok':True}
    assert requests == [('http://host.docker.internal:8771' + endpoint, {'sample':'value'})]
    assert len(commits) == 2 and item.receipt_json['attempt_history'][0]['status'] == 'responded'

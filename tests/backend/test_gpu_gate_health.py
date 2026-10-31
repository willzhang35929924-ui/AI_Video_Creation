import asyncio
import json

import httpx

from app.services import gpu_gate


def registry(root, processes):
    directory = root / 'data' / 'pids'
    directory.mkdir(parents=True)
    (directory / 'dev-processes.json').write_text(json.dumps({
        'schemaVersion': 1,
        'projectRoot': str(root),
        'processes': processes,
    }), encoding='utf-8')


class TimeoutClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url):
        raise httpx.ConnectTimeout('loopback probe timed out')


class HealthClient(TimeoutClient):
    def __init__(self, health, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.health = health

    async def get(self, _url):
        return type('Response', (), {
            'raise_for_status': lambda self: None,
            'json': lambda self: self.health,
            'health': self.health,
        })()


def test_timeout_with_verified_registry_and_no_qwen_process_is_idle(tmp_path, monkeypatch):
    registry(tmp_path, [{'kind': 'backend', 'pid': 12345,
                         'workingDirectory': str(tmp_path)}])
    monkeypatch.setattr(gpu_gate, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(gpu_gate.httpx, 'AsyncClient', TimeoutClient)

    status = asyncio.run(gpu_gate.owned_model_status())

    assert status == {
        'state': 'idle',
        'reason': 'health_unreachable_and_no_managed_runtime_process',
    }
    assert asyncio.run(gpu_gate.owned_model_busy()) is False


def test_timeout_with_live_managed_qwen_process_remains_fail_closed(tmp_path, monkeypatch):
    registry(tmp_path, [{'kind': 'screenplay_model', 'pid': __import__('os').getpid(),
                         'workingDirectory': str(tmp_path)}])
    monkeypatch.setattr(gpu_gate, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(gpu_gate.httpx, 'AsyncClient', TimeoutClient)

    status = asyncio.run(gpu_gate.owned_model_status())

    assert status['state'] == 'unknown'
    assert status['reason'] == 'health_unreachable_while_managed_runtime_process_is_alive'


def test_timeout_without_verifiable_process_registry_stays_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_gate, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(gpu_gate.httpx, 'AsyncClient', TimeoutClient)

    status = asyncio.run(gpu_gate.owned_model_status())

    assert status['state'] == 'unknown'
    assert status['reason'] == 'health_unreachable_and_runtime_process_state_unverified'


def test_loaded_model_health_keeps_gpu_reserved_until_unload(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_gate, 'PROJECT_ROOT', tmp_path)
    health = {
        'service': 'video-factory-screenplay-qwen-v1',
        'workspace': str(tmp_path),
        'busy': False,
        'loaded': True,
    }
    monkeypatch.setattr(gpu_gate.httpx, 'AsyncClient', lambda *args, **kwargs: HealthClient(health))

    status = asyncio.run(gpu_gate.owned_model_status())

    assert status == {'state': 'busy', 'reason': 'health_busy'}

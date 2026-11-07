import asyncio

import httpx
import pytest

from app.core.config import PROJECT_ROOT
from app.services import context_peer_memory as memory


GIB = 1024**3
IDLE = {'queue_running': [], 'queue_pending': []}


def native_stats(reserved=64 * 1024**2):
    return {'system': {'argv': ['E:/ComfyUI/main.py', '--listen', '127.0.0.1', '--port', '8189',
            '--disable-async-offload', '--disable-pinned-memory', '--disable-comfy-compiler',
            '--models-directory', 'E:/Models/H3R2V',
            '--input-directory', str(PROJECT_ROOT / 'data/comfyui_runtime/input'),
            '--output-directory', str(PROJECT_ROOT / 'data/comfyui_runtime/output')]},
            'devices': [{'type': 'cuda', 'torch_vram_total': reserved, 'vram_total': 16 * GIB}]}


@pytest.fixture(autouse=True)
def controlled_commit(monkeypatch):
    host = {'commit_bytes': 51 * GIB, 'commit_limit_bytes': 56 * GIB, 'headroom_bytes': 5 * GIB}
    monkeypatch.setattr(memory, '_commit_snapshot', lambda: dict(host))
    monkeypatch.setattr(memory, '_last_clean_history', None)
    return host


def invoke(handler, origin=memory.NATIVE_ORIGIN):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await memory.prepare_context_peer_memory(client, origin)
    return asyncio.run(scenario())


@pytest.mark.parametrize('condition,state', [
    ('busy', 'peer_busy'), ('unreachable', 'peer_unverified'), ('malformed', 'peer_unverified'),
    ('foreign', 'peer_identity_unverified'), ('went_busy', 'peer_busy'),
])
def test_peer_not_mutated_unless_project_identity_and_idle_are_confirmed(condition, state):
    queue_reads = 0

    def request(req):
        nonlocal queue_reads
        assert req.method == 'GET'
        if req.url.path == '/queue':
            queue_reads += 1
            if condition == 'unreachable':
                raise httpx.ReadTimeout('Controlled timeout', request=req)
            if condition == 'malformed':
                return httpx.Response(200, json={'queue_running': []})
            if condition == 'busy' or condition == 'went_busy' and queue_reads > 1:
                return httpx.Response(200, json={'queue_running': ['different job'], 'queue_pending': []})
            return httpx.Response(200, json=IDLE)
        if req.url.path == '/system_stats':
            stats = native_stats(2 * GIB)
            if condition == 'foreign':
                stats['system']['argv'][-1] = 'D:/other-comfy/project/output'
            return httpx.Response(200, json=stats)
        assert req.url.path == '/history'
        return httpx.Response(200, json={'last-job': {}})

    evidence = invoke(request)
    assert evidence['state'] == state and evidence['ready'] is False
    assert evidence['release_requested'] is False


def test_refused_and_clean_peer_use_existing_commit_guard_floor(controlled_commit):
    def refused(req):
        raise httpx.ConnectError('Controlled refusal', request=req) from ConnectionRefusedError(10061, 'refused')

    evidence = invoke(refused)
    assert evidence['state'] == 'peer_absent' and evidence['ready'] is True
    controlled_commit.update(commit_bytes=54 * GIB + 1, headroom_bytes=2 * GIB - 1)

    def clean(req):
        assert req.method == 'GET'
        return httpx.Response(200, json={'/queue': IDLE, '/system_stats': native_stats(), '/history': {}}[req.url.path])

    evidence = invoke(clean)
    assert evidence['state'] == 'commit_below_guard' and evidence['ready'] is False
    assert evidence['commit_after']['headroom_bytes'] == 2 * GIB - 1


def test_unknown_host_commit_does_not_touch_peer(monkeypatch):
    monkeypatch.setattr(memory, '_commit_snapshot', lambda: None)

    def no_request(req):
        pytest.fail(f'Unexpected request to {req.url.path}')

    evidence = invoke(no_request)
    assert evidence['state'] == 'commit_unverified' and evidence['ready'] is False


def test_retained_native_cache_released_once_and_verified_before_h3(controlled_commit):
    events = []
    released = False

    def request(req):
        nonlocal released
        if req.method == 'POST':
            assert req.url.path == '/free'
            assert events[-1] == '/queue'
            assert not released
            released = True
            controlled_commit.update(commit_bytes=43 * GIB, headroom_bytes=13 * GIB)
            return httpx.Response(200)
        events.append(req.url.path)
        if req.url.path == '/system_stats':
            stats = native_stats() if released else native_stats(2 * GIB)
            return httpx.Response(200, json=stats)
        return httpx.Response(200, json={'/queue': IDLE, '/history': {'last-job': {}}}[req.url.path])

    evidence = invoke(request)
    assert evidence['ready'] and evidence['state'] == 'peer_released' and released
    assert len(evidence['observations']) == 2
    assert evidence['commit_before']['headroom_bytes'] == 5 * GIB
    assert evidence['commit_after']['headroom_bytes'] == 13 * GIB
    assert invoke(request)['state'] == 'peer_clean'

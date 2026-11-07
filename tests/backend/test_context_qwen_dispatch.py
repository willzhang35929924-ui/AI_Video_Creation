import asyncio

import httpx
import pytest

from app.core.errors import AppError
from app.services.h3_context_provider import require_context_qwen
from app.services.studio_pipeline import _defer_context_dispatch


def test_context_probe_rejects_restore_race_and_admits_only_verified_idle_runtime():
    requests = []
    health = dict(service='video-factory-h3ir-qwen4b-v1', profile='h3ir-qwen4b-32k',
                  model_id='Qwen/Qwen3-VL-4B-Instruct', offline=True, busy=False, loaded=False,
                  script_sha256='a'*64, inference_phase='idle', gpu_release_status='confirmed',
                  last_gpu_cleanup={'released': True})

    def reply(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            raise httpx.ConnectError('[WinError 10061] connection refused', request=request)
        if len(requests) == 2:
            return httpx.Response(200, json={**health, 'busy': True})
        if len(requests) == 3:
            return httpx.Response(200, json={**health, 'last_gpu_cleanup': {'released': False}})
        if len(requests) == 4:
            return httpx.Response(200, json={**health, 'offline': False})
        return httpx.Response(200, json=health)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            for code in ('context_ir_qwen_starting', 'context_ir_qwen_starting',
                         'context_ir_qwen_release_unknown', 'context_ir_qwen_identity'):
                with pytest.raises(AppError) as blocked:
                    await require_context_qwen(client, 'http://127.0.0.1:8188')
                assert blocked.value.code == code
            await require_context_qwen(client, 'http://127.0.0.1:8188')

    asyncio.run(run())
    assert requests == ['http://127.0.0.1:8772/health'] * 5


@pytest.mark.parametrize('code,timeout', [
    ('context_ir_qwen_starting', 'context_ir_qwen_unavailable'),
    ('local_gpu_busy', 'context_ir_gpu_wait_timeout'),
])
def test_context_restore_wait_is_bounded_and_preserves_generation_budget(code, timeout):
    entry = dict(status='pending', attempts=3, child_id=None, history=[{'child_id': 'previous'}])
    _defer_context_dispatch(entry, code, 'restoring', now=100)
    assert (entry['status'], entry['retry_after'], entry['context_dispatch_wait']['since']) == ('retrying', 105, 100)
    _defer_context_dispatch(entry, code, 'still restoring', now=215)
    assert (entry['status'], entry['retry_after'], entry['context_dispatch_wait']['since']) == ('retrying', 220, 100)
    _defer_context_dispatch(entry, code, 'not responding', now=220)
    assert (entry['status'], entry['code']) == ('exception', timeout)
    assert entry['attempts'] == 3 and entry['child_id'] is None and entry['history'] == [{'child_id': 'previous'}]

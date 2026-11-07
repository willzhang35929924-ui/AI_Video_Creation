"""Provider busy compatibility; this does not claim durable direct-job queuing."""
import asyncio

import httpx
import pytest

from app.core.errors import AppError
from app.services import h3_context_provider as provider
from app.services.h3_workflow_router import freeze_workflow
from test_context_cold_start import healthy,provider_request


def mock_client(monkeypatch,reply):
    real=httpx.AsyncClient
    if hasattr(provider,'context_client'):
        monkeypatch.setattr(provider,'context_client',lambda *a,**kw:real(transport=httpx.MockTransport(reply),**kw))
    else:
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(reply),**kw))


def test_direct_provider_busy_preserves_none_without_upload_or_prompt(settings,tmp_path,monkeypatch):
    calls=[]
    def reply(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/queue':
            return httpx.Response(200,json={'queue_running':[[1,'existing-provider-id',{}]],'queue_pending':[]})
        return healthy(request)
    mock_client(monkeypatch,reply)
    monkeypatch.setattr(provider,'PROJECT_ROOT',tmp_path)
    settings.comfyui_base_url='http://localhost:8188'
    result=asyncio.run(provider.ContextIRH3Provider(settings,freeze_workflow('fast')).submit(provider_request(tmp_path)))
    assert result is None
    assert calls==[('GET','/queue'),('GET','/object_info')]
    assert not (tmp_path/'logs/jobs/isolated_job/submitted_prompt.json').exists()


def test_pipeline_readiness_busy_still_raises_for_its_bounded_wait():
    def reply(request):
        if request.url.path=='/queue':
            return httpx.Response(200,json={'queue_running':[[1,'existing-provider-id',{}]],'queue_pending':[]})
        return healthy(request)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(AppError) as error:
                await provider.require_context_ready(client,'http://localhost:8188')
            assert error.value.code=='local_gpu_busy'
    asyncio.run(exercise())


@pytest.mark.parametrize('code',['context_ir_qwen_starting','context_ir_nodes_missing','context_ir_submission_unknown'])
def test_provider_busy_compatibility_does_not_swallow_other_errors(settings,tmp_path,monkeypatch,code):
    mock_client(monkeypatch,healthy)
    monkeypatch.setattr(provider,'PROJECT_ROOT',tmp_path)
    async def reject(*a,**kw):raise AppError(503,code,'Must preserve exact outcome')
    monkeypatch.setattr(provider,'require_context_ready',reject)
    settings.comfyui_base_url='http://localhost:8188'
    with pytest.raises(AppError) as error:
        asyncio.run(provider.ContextIRH3Provider(settings,freeze_workflow('fast')).submit(provider_request(tmp_path)))
    assert error.value.code==code

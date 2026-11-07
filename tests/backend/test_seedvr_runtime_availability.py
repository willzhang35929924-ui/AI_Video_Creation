import asyncio
from types import SimpleNamespace

import httpx

from app.models.quality import QualityRender
from app.services import studio_pipeline, video_quality
from test_video_quality import context


def test_seedvr_native_wait_is_actionable_and_resumes_without_duplicate_dispatch(context, settings, monkeypatch):
    session, factory, _, shot, source = context
    render = QualityRender(source_video_id=source.id, snapshot_json={'settings': {'processing': 'seedvr2'}})
    unknown = QualityRender(source_video_id=source.id, status='unknown', snapshot_json={
        'settings': {'processing': 'seedvr2'}, 'native_execution': {'prompt_id': 'existing', 'inference_submitted': None}})
    session.add_all([render, unknown])
    session.commit()

    readiness = {'8188_busy': False, '8189_online': False}
    mutations = []
    dispatched = []
    idle = {'queue_running': [], 'queue_pending': []}

    def request(req):
        if req.url.port == 8189 and not readiness['8189_online']:
            raise httpx.ConnectError('Native port refused', request=req) from ConnectionRefusedError(10061, 'refused')
        if req.method == 'POST':
            mutations.append((req.url.port, req.url.path))
            return httpx.Response(200, json={})
        if req.url.path == '/queue':
            return httpx.Response(200, json={'queue_running': ['peer work'], 'queue_pending': []}
                                  if req.url.port == 8188 and readiness['8188_busy'] else idle)
        assert req.url.port == 8189 and req.url.path == '/system_stats'
        return httpx.Response(200, json={'devices': [{'type': 'cuda', 'vram_total': 8 * 1024**3,
                                                     'vram_free': 6 * 1024**3, 'torch_vram_total': 0}]})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(request), **kwargs))
    monkeypatch.setattr(video_quality, 'run_render', lambda _settings, _factory, identifier, _receipt: dispatched.append(identifier))
    quality = {'id': 'quality', 'dependencies': ['raw'], 'child_id': render.id, 'target_id': shot.id}
    run = SimpleNamespace(nodes_json=[{'id': 'raw', 'video_id': source.id}, quality])

    assert asyncio.run(video_quality.run_ai_locked(settings, factory, render.id)) is False
    session.refresh(render)
    assert render.status == 'queued' and '请启动项目原生 ComfyUI' in render.error_message
    assert render.snapshot_json == {'settings': {'processing': 'seedvr2'}}
    assert render.result_json == {} and not mutations and not dispatched
    studio_pipeline._quality(session, settings, run, quality)
    assert quality['status'] == 'running' and quality['error'] == render.error_message

    readiness.update({'8188_busy': True, '8189_online': True})
    assert asyncio.run(video_quality.run_ai_locked(settings, factory, render.id)) is False
    assert not mutations and not dispatched
    readiness['8188_busy'] = False
    assert asyncio.run(video_quality.run_ai_locked(settings, factory, unknown.id)) is False
    assert not mutations and not dispatched
    assert asyncio.run(video_quality.run_ai_locked(settings, factory, render.id)) is True
    assert mutations == [(8189, '/free')] and dispatched == [render.id]

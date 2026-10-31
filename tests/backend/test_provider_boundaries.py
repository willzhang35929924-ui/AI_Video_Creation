from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.services.providers import ComfyUIH3Provider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str):
        if url.endswith("/system_stats"):
            return FakeResponse({"system": {"comfyui_version": "test-version"}})
        return FakeResponse({"MiniMaxH3ReferenceVideo": {"input": {}}})


def test_comfy_health_never_claims_ready_without_native_r2v_and_models(monkeypatch) -> None:
    provider = ComfyUIH3Provider(Settings(h3_demo_mode=False))
    monkeypatch.setattr(provider, "_load_workflow", lambda: ({}, {"name": "verified-local-graph", "mode": "reference_to_video", "reference_paths": [["1", "inputs", "image"]]}))
    monkeypatch.setattr("app.services.providers.httpx.AsyncClient", FakeClient)

    health = asyncio.run(provider.health())

    assert health.connected is True
    assert health.version == "test-version"
    assert health.workflow_name == "verified-local-graph"
    assert health.ready is False
    assert health.reference_to_video_available is False
    assert "原生 MiniMaxH3ReferenceToVideo" in (health.error or "")


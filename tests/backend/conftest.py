from __future__ import annotations

import sys
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Starlette's blocking TestClient can deadlock on the Windows Proactor loop in
# restricted CI/sandbox environments; use the selector loop for deterministic
# in-process API tests while production uvicorn keeps its normal policy.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services import gpu_gate  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_owned_model_health(monkeypatch):
    """Keep isolated tests away from the workstation's production GPU.

    Gate-specific tests can restore the returned check while replacing its HTTP
    client locally. This does not patch httpx for unrelated application code.
    """
    real_check = gpu_gate.owned_model_busy

    async def isolated_idle():
        return False

    monkeypatch.setattr(gpu_gate, 'owned_model_busy', isolated_idle)
    return real_check


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        media_root=tmp_path / "media",
        h3_demo_mode=True,
        seed_demo_data=False,
        allow_external_doc_import=False,
        import_max_mb=2,
    )


@pytest.fixture()
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


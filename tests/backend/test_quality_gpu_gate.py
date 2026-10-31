"""Controlled scheduling contracts: isolated SQLite only; no network/CUDA/inference.

HTTP responses and the render entry point are explicitly substituted. These
tests verify the production gate's ordering and failure records, not GPU VRAM
release or Real-ESRGAN output on real hardware.
"""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest

from app import models as m
from app.core.db import Base, build_engine, build_session_factory
from app.models.quality import QualityRender
from app.services import video_quality as quality
from app.services.gpu_gate import gpu_submission


GIB = 1024 ** 3


def cuda_stats(*, reserved=32 * 1024 ** 2, available=6 * GIB, total=10 * GIB):
    return {"devices": [{"type": "cuda", "vram_total": total,
                         "torch_vram_total": reserved, "vram_free": available}]}


class ControlledSession:
    """Minimal in-memory row boundary; never opens a SQL connection."""

    def __init__(self):
        self.item = SimpleNamespace(id="explicit-contract-render", status="queued",
                                    error_message=None, result_json={})
        self.commits = 0

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def scalar(self, _statement):
        return None  # No active production work in this controlled case.

    def get(self, model, identifier):
        assert model is QualityRender and identifier == self.item.id
        return self.item

    def commit(self):
        self.commits += 1


class ControlledComfy:
    def __init__(self, events):
        self.events = events
        self.observations = [cuda_stats(), cuda_stats()]
        self.stats_reads = 0
        self.fail_at = None
        self.queue = {"queue_running": [], "queue_pending": []}
        self.on_first_stats = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def response(self, method, url, payload):
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))

    async def get(self, url):
        endpoint = url.rsplit("/", 1)[-1]
        self.events.append(endpoint)
        if endpoint == self.fail_at:
            raise httpx.ConnectError("Explicit controlled connection failure", request=httpx.Request("GET", url))
        if endpoint == "queue":
            return self.response("GET", url, self.queue)
        assert endpoint == "system_stats"
        if not self.stats_reads and self.on_first_stats:
            self.on_first_stats()
        current = self.observations[min(self.stats_reads, len(self.observations) - 1)]
        self.stats_reads += 1
        return self.response("GET", url, current)

    async def post(self, url, *, json):
        assert url.endswith("/free")
        assert json == {"unload_models": True, "free_memory": True}
        self.events.append("free")
        if self.fail_at == "free":
            return httpx.Response(503, request=httpx.Request("POST", url))
        return self.response("POST", url, {})


@pytest.fixture
def controlled_gate(monkeypatch):
    events = []
    session = ControlledSession()
    comfy = ControlledComfy(events)
    clock = SimpleNamespace(now=0.0)
    helper_calls = []
    real_sleep = asyncio.sleep

    async def controlled_sleep(delay):
        assert delay == .25
        clock.now += delay
        await real_sleep(0)  # Yield to the competing gate without wall-clock waits.

    def client(**kwargs):
        assert kwargs == {"timeout": 10, "trust_env": False}
        return comfy

    def render(settings, factory, identifier, evidence):
        assert factory is session and identifier == session.item.id
        events.append("helper")
        helper_calls.append(copy.deepcopy(evidence))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(quality, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(quality.asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(quality, "run_render", render)
    return SimpleNamespace(settings=SimpleNamespace(comfyui_base_url="http://controlled.invalid"),
                           session=session, comfy=comfy, clock=clock,
                           events=events, helper_calls=helper_calls)


def invoke(case):
    return asyncio.run(quality.run_ai_locked(case.settings, case.session, case.session.item.id))


def assert_pre_dispatch_failure(case):
    assert case.session.item.status == "failed"
    assert case.session.item.result_json["inference_submitted"] is False
    assert case.session.item.result_json["source_not_modified_by_worker"] is True
    assert case.session.item.error_message
    assert case.helper_calls == []
    assert "helper" not in case.events
    assert case.session.commits == 1


def test_release_requires_two_consecutive_observations_and_holds_shared_gate(controlled_gate):
    case = controlled_gate
    # A ready reading followed by high reserved memory must reset stability.
    case.comfy.observations = [cuda_stats(reserved=2 * GIB), cuda_stats(),
                               cuda_stats(reserved=GIB), cuda_stats(), cuda_stats()]

    async def scenario():
        contenders = []

        @gpu_submission
        async def other_local_submission():
            case.events.append("competing_submission")

        case.comfy.on_first_stats = lambda: contenders.append(asyncio.create_task(other_local_submission()))
        assert await quality.run_ai_locked(case.settings, case.session, case.session.item.id) is True
        await asyncio.gather(*contenders)

    asyncio.run(scenario())
    assert case.events == ["queue", "free", *("system_stats" for _ in range(5)),
                           "helper", "competing_submission"]
    assert len(case.helper_calls) == 1
    evidence = case.helper_calls[0]
    assert evidence["ack_is_not_unload_completion"] is True
    assert len(evidence["observations"]) == 5
    assert case.session.item.status == "queued"  # The substituted helper claims no output.


@pytest.mark.parametrize("stats", [
    cuda_stats(reserved=GIB),  # Empty queue/HTTP200 does not prove unloaded cache.
    cuda_stats(available=4 * GIB),  # Low allocator use alone does not prove device headroom.
    {"devices": [{"type": "cuda", "vram_total": 10 * GIB, "vram_free": 8 * GIB}]},
    {"devices": []},
])
def test_free_ack_without_verified_release_fails_within_deadline(controlled_gate, stats):
    case = controlled_gate
    case.comfy.observations = [stats]
    assert invoke(case) is True  # The queue entry was handled as an explicit failure.
    assert_pre_dispatch_failure(case)
    assert case.events[:2] == ["queue", "free"]
    assert case.clock.now == pytest.approx(30)
    assert len(case.session.item.result_json["gpu_release_check"]["observations"]) == 120


@pytest.mark.parametrize("endpoint", ["queue", "free", "system_stats"])
def test_http_failure_never_starts_helper_and_records_no_inference(controlled_gate, endpoint):
    case = controlled_gate
    case.comfy.fail_at = endpoint
    assert invoke(case) is True
    assert_pre_dispatch_failure(case)
    assert case.clock.now == 0
    if endpoint == "queue":
        assert case.events == ["queue"]
    elif endpoint == "free":
        assert case.events == ["queue", "free"]
    else:
        assert case.events == ["queue", "free", "system_stats"]


def test_one_ready_observation_at_deadline_is_not_enough(controlled_gate):
    case = controlled_gate
    case.comfy.observations = [cuda_stats(reserved=GIB)] * 119 + [cuda_stats()]
    assert invoke(case) is True
    assert_pre_dispatch_failure(case)
    assert case.comfy.stats_reads == 120
    assert case.session.item.result_json["gpu_release_check"]["observations"][-1]["comfy_torch_reserved"] < GIB


@pytest.mark.parametrize("queue_key", ["queue_running", "queue_pending"])
def test_existing_comfy_work_defers_without_free_or_inference(controlled_gate, queue_key):
    case = controlled_gate
    case.comfy.queue[queue_key] = [[0, "controlled-existing-task"]]
    assert invoke(case) is False
    assert case.events == ["queue"]
    assert case.session.item.status == "queued"
    assert case.session.item.result_json == {}
    assert case.session.commits == 0 and not case.helper_calls


@pytest.fixture
def sqlite_gate(settings, controlled_gate, monkeypatch):
    """Real SQL selection against temporary rows; HTTP/render remain controlled."""
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        project = m.Project(name="Isolated provider occupancy contract")
        character = m.Character(slug="occupancy-contract", display_name="Contract adult")
        session.add_all([project, character]); session.flush()
        version = m.CharacterVersion(character_id=character.id, version=1, identity_json={})
        shot = m.StoryboardShot(project_id=project.id, order=1, scene_title="Contract only")
        session.add_all([version, shot]); session.flush()
        batch = m.CharacterBatch(character_id=character.id, character_version_id=version.id,
                                 kind="candidates", status="generated", snapshot_json={})
        source_job = m.GenerationJob(project_id=project.id, shot_id=shot.id, provider="comfyui_h3",
                                     status="generated", prompt="Not model evidence")
        session.add_all([batch, source_job]); session.flush()
        video = m.VideoRevision(project_id=project.id, shot_id=shot.id,
                                generation_job_id=source_job.id, revision=1,
                                relative_path="contract-only-never-opened.mp4", metadata_json={})
        session.add(video); session.flush()
        render = QualityRender(source_video_id=video.id, status="queued", snapshot_json={}, result_json={})
        session.add(render); session.commit()
        ids = SimpleNamespace(project=project.id, shot=shot.id, batch=batch.id, render=render.id)

    rows = []
    kinds = {"video": m.GenerationJob, "image": m.CharacterGenerationJob, "visual": m.ProductionVisual}

    def seed(kind, provider, status):
        with factory() as session:
            if kind == "video":
                row = m.GenerationJob(project_id=ids.project, shot_id=ids.shot, provider=provider,
                                       status=status, prompt="Isolated occupancy row; no submission")
            elif kind == "image":
                row = m.CharacterGenerationJob(batch_id=ids.batch, asset_type="front_face", seed=1,
                    prompt="Isolated occupancy row; no submission", status=status,
                    snapshot_json={"service_snapshot": {"provider": provider}})
            else:
                row = m.ProductionVisual(project_id=ids.project, shot_id=ids.shot, kind="keyframe",
                    prompt="Isolated occupancy row; no submission", status=status,
                    snapshot_json={"service_snapshot": {"provider": provider}})
            session.add(row); session.commit()
            rows.append((kinds[kind], row.id, status))

    def render_entry(_settings, actual_factory, identifier, evidence):
        assert actual_factory is factory and identifier == ids.render
        controlled_gate.events.append("helper")
        controlled_gate.helper_calls.append(copy.deepcopy(evidence))

    def unchanged():
        with factory() as session:
            assert session.get(QualityRender, ids.render).status == "queued"
            for model, identifier, status in rows:
                assert session.get(model, identifier).status == status

    monkeypatch.setattr(quality, "run_render", render_entry)
    try:
        yield SimpleNamespace(settings=settings, factory=factory, ids=ids, seed=seed,
                              events=controlled_gate.events, helper_calls=controlled_gate.helper_calls,
                              unchanged=unchanged)
    finally:
        engine.dispose()


ACTIVE_STATES = ["queued", "submitting", "submitted", "running", "cancelling"]


@pytest.mark.parametrize("kind,provider", [
    ("video", "video_api"), ("video", "cloud_video_jobs_v1"),
    ("image", "openai_compatible"), ("visual", "openai_compatible"),
])
def test_cloud_active_rows_do_not_occupy_local_gpu(sqlite_gate, kind, provider):
    case = sqlite_gate
    for status in ACTIVE_STATES:
        case.seed(kind, provider, status)
    assert asyncio.run(quality.run_ai_locked(case.settings, case.factory, case.ids.render)) is True
    # Local settings additionally verify both known same-card Comfy queues.
    assert case.events == ["queue", "queue", "queue", "free", "system_stats", "system_stats", "helper"]
    assert len(case.helper_calls) == 1
    case.unchanged()


@pytest.mark.parametrize("kind,provider", [
    ("video", "comfyui_h3"), ("image", "comfyui"), ("visual", "comfyui"),
])
@pytest.mark.parametrize("status", ACTIVE_STATES)
def test_local_active_rows_still_defer_before_comfy_free(sqlite_gate, kind, provider, status):
    case = sqlite_gate
    # A cloud row must not hide a later local row in SQL provider filtering.
    case.seed(kind, "cloud_video_jobs_v1" if kind == "video" else "openai_compatible", "running")
    case.seed(kind, provider, status)
    assert asyncio.run(quality.run_ai_locked(case.settings, case.factory, case.ids.render)) is False
    assert case.events == [] and not case.helper_calls
    case.unchanged()


def test_finished_local_rows_do_not_occupy_local_gpu(sqlite_gate):
    case = sqlite_gate
    for kind, provider in [("video", "comfyui_h3"), ("image", "comfyui"), ("visual", "comfyui")]:
        for status in ["generated", "failed", "cancelled"]:
            case.seed(kind, provider, status)
    assert asyncio.run(quality.run_ai_locked(case.settings, case.factory, case.ids.render)) is True
    assert case.events[-1] == "helper" and len(case.helper_calls) == 1
    case.unchanged()

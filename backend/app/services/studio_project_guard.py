"""Serialize Studio graph writers in the single application worker.

Provider tasks run outside this guard. Only graph advancement and repair own
it, including a provider POST's await, so an old graph cannot undo a repair.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _Guard:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task | None = None


@asynccontextmanager
async def project_guard(app, project_id):
    guards = getattr(app.state, '_studio_project_guards', None)
    if guards is None:
        guards = {}
        app.state._studio_project_guards = guards
    guard = guards.setdefault(project_id, _Guard())
    task = asyncio.current_task()
    if guard.owner is task:
        # Repair advances the same graph through the normal pipeline.
        yield True
        return
    async with guard.lock:
        guard.owner = task
        try:
            yield False
        finally:
            guard.owner = None

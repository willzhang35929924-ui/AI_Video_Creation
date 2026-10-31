"""Project mutation serialization without a database, network or inference."""
import asyncio
from types import SimpleNamespace

import pytest

from app.services.studio_project_guard import project_guard


def run_bounded(coroutine):
    async def run():
        return await asyncio.wait_for(coroutine,timeout=5)
    return asyncio.run(run())


def test_same_project_tasks_serialize_until_the_owner_exits():
    async def scenario():
        app=SimpleNamespace(state=SimpleNamespace())
        owner_entered=asyncio.Event();release_owner=asyncio.Event()
        waiter_started=asyncio.Event();waiter_entered=asyncio.Event()
        order=[]
        async def owner():
            async with project_guard(app,'project-a') as nested:
                assert nested is False
                order.append('owner entered');owner_entered.set()
                await release_owner.wait()
                order.append('owner leaving')
        async def waiter():
            waiter_started.set()
            async with project_guard(app,'project-a') as nested:
                assert nested is False
                order.append('waiter entered');waiter_entered.set()
        owner_task=asyncio.create_task(owner())
        await owner_entered.wait()
        waiter_task=asyncio.create_task(waiter())
        await waiter_started.wait();await asyncio.sleep(0)
        assert not waiter_entered.is_set()
        release_owner.set()
        await asyncio.gather(owner_task,waiter_task)
        assert order==['owner entered','owner leaving','waiter entered']
    run_bounded(scenario())


def test_nested_owner_is_reentrant_but_a_child_task_is_a_distinct_owner():
    async def scenario():
        app=SimpleNamespace(state=SimpleNamespace())
        child_started=asyncio.Event();child_entered=asyncio.Event()
        async def child():
            child_started.set()
            async with project_guard(app,'project-a') as nested:
                assert nested is False
                child_entered.set()
        async with project_guard(app,'project-a') as outer:
            assert outer is False
            async with project_guard(app,'project-a') as nested:
                assert nested is True
                async with project_guard(app,'project-a') as twice_nested:
                    assert twice_nested is True
            # ContextVars are inherited when spawning; that must not let a
            # different asyncio task bypass the current task's ownership.
            child_task=asyncio.create_task(child())
            await child_started.wait();await asyncio.sleep(0)
            assert not child_entered.is_set()
            async with project_guard(app,'project-a') as still_nested:
                assert still_nested is True
        await child_task
        assert child_entered.is_set()
        async with project_guard(app,'project-a') as new_outer:
            assert new_outer is False
    run_bounded(scenario())


def test_cancelled_waiter_keeps_owner_intact_and_cancelled_owner_releases_lock():
    async def scenario():
        app=SimpleNamespace(state=SimpleNamespace())
        owner_entered=asyncio.Event();check_nested=asyncio.Event();nested_checked=asyncio.Event()
        cancelled_waiter_started=asyncio.Event();next_waiter_started=asyncio.Event();next_waiter_entered=asyncio.Event()
        async def owner():
            async with project_guard(app,'project-a') as outer:
                assert outer is False
                owner_entered.set()
                await check_nested.wait()
                async with project_guard(app,'project-a') as nested:
                    assert nested is True
                nested_checked.set()
                await asyncio.Event().wait()
        async def cancelled_waiter():
            cancelled_waiter_started.set()
            async with project_guard(app,'project-a'):
                pytest.fail('The cancelled waiter must never enter the guarded section')
        async def next_waiter():
            next_waiter_started.set()
            async with project_guard(app,'project-a') as outer:
                assert outer is False
                next_waiter_entered.set()
        owner_task=asyncio.create_task(owner())
        await owner_entered.wait()
        waiter_task=asyncio.create_task(cancelled_waiter())
        await cancelled_waiter_started.wait();await asyncio.sleep(0)
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):await waiter_task
        check_nested.set();await nested_checked.wait()
        following_task=asyncio.create_task(next_waiter())
        await next_waiter_started.wait();await asyncio.sleep(0)
        assert not next_waiter_entered.is_set(),'Cancelling a waiter must not release another task\'s lock'
        owner_task.cancel()
        with pytest.raises(asyncio.CancelledError):await owner_task
        await following_task
        assert next_waiter_entered.is_set()
        async with project_guard(app,'project-a') as new_outer:
            assert new_outer is False
    run_bounded(scenario())


def test_distinct_projects_can_be_guarded_concurrently():
    async def scenario():
        app=SimpleNamespace(state=SimpleNamespace())
        other_entered=asyncio.Event();release_other=asyncio.Event()
        async def other_project():
            async with project_guard(app,'project-b') as nested:
                assert nested is False
                other_entered.set()
                await release_other.wait()
        async with project_guard(app,'project-a') as nested:
            assert nested is False
            other_task=asyncio.create_task(other_project())
            await other_entered.wait()
            assert not other_task.done(),'The independent project is still holding its own guard'
            async with project_guard(app,'project-a') as own_nested:
                assert own_nested is True
        release_other.set();await other_task
    run_bounded(scenario())

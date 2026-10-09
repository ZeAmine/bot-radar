"""Le drainage attend le travail réel et les nettoyages, avec une échéance commune."""

import asyncio
import logging

import pytest

from app.core import tasks


@pytest.fixture
async def registry(monkeypatch):
    registered = set()
    monkeypatch.setattr(tasks, "_background_tasks", registered)
    yield registered
    for task in tuple(registered):
        if not task.done():
            task.cancel()
    await asyncio.gather(*registered, return_exceptions=True)


async def checkpoint():
    """Exécute les callbacks déjà prêts, sans attendre une durée arbitraire."""
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


async def test_parent_child_is_still_awaited_after_parent_has_finished(registry):
    release_parent, child_started, release_child, child_finished = (
        asyncio.Event() for _ in range(4)
    )

    async def child():
        child_started.set()
        await release_child.wait()
        child_finished.set()

    async def parent():
        await release_parent.wait()
        tasks.safe_create_task(child(), name="synthetic-child")

    parent_task = tasks.safe_create_task(parent(), name="synthetic-parent")
    draining = asyncio.create_task(tasks.wait_all_background_tasks())
    asyncio.get_running_loop().call_soon(release_parent.set)
    try:
        await child_started.wait()
        await parent_task
        await checkpoint()
        assert not draining.done(), "Le drainage est revenu alors que l'enfant reste actif"
        release_child.set()
        await draining
        assert child_finished.is_set()
        assert not registry
    finally:
        release_child.set()
        await draining


async def test_simple_task_finishes_before_drain_returns(registry):
    finished = asyncio.Event()

    async def work():
        finished.set()

    tasks.safe_create_task(work())
    await tasks.wait_all_background_tasks()
    assert finished.is_set()
    assert not registry


async def test_multiple_generations_finish_and_clear_registry(registry):
    entered = [asyncio.Event() for _ in range(4)]
    release = [asyncio.Event() for _ in range(4)]
    finished = []

    async def generation(index):
        entered[index].set()
        await release[index].wait()
        if index < 3:
            tasks.safe_create_task(generation(index + 1))
        finished.append(index)

    tasks.safe_create_task(generation(0))
    draining = asyncio.create_task(tasks.wait_all_background_tasks())
    try:
        for index in range(4):
            await entered[index].wait()
            await checkpoint()
            assert not draining.done()
            release[index].set()
        await draining
        assert finished == [0, 1, 2, 3]
        assert not registry
    finally:
        for event in release:
            event.set()
        await draining


async def test_unrelated_task_created_during_wait_is_drained(registry):
    first_entered, first_release, late_entered, late_release, late_finished = (
        asyncio.Event() for _ in range(5)
    )

    async def first():
        first_entered.set()
        await first_release.wait()

    async def late():
        late_entered.set()
        await late_release.wait()
        late_finished.set()

    initial = tasks.safe_create_task(first())
    await first_entered.wait()
    draining = asyncio.create_task(tasks.wait_all_background_tasks())
    asyncio.get_running_loop().call_soon(lambda: tasks.safe_create_task(late()))
    try:
        await late_entered.wait()
        first_release.set()
        await initial
        await checkpoint()
        assert not draining.done()
        late_release.set()
        await draining
        assert late_finished.is_set()
        assert not registry
    finally:
        first_release.set()
        late_release.set()
        await draining


async def test_task_exception_is_logged_and_registry_is_cleaned(registry, caplog):
    async def failing():
        raise ValueError("synthetic failure")

    with caplog.at_level(logging.ERROR, logger="radar.tasks"):
        tasks.safe_create_task(failing(), name="synthetic-failure")
        await tasks.wait_all_background_tasks()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info[0] is ValueError
    assert "synthetic-failure" in errors[0].message
    assert not registry


async def test_deadline_is_not_renewed_when_parent_spawns_child(registry, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(tasks, "monotonic", lambda: clock[0])
    release_parent, child_entered, child_cleaned = (asyncio.Event() for _ in range(3))
    child_tasks = []

    async def child():
        child_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            child_cleaned.set()

    async def parent():
        await release_parent.wait()
        clock[0] = 4.0
        child_tasks.append(tasks.safe_create_task(child()))

    async def trigger_registry_change():
        return None

    tasks.safe_create_task(parent())
    draining = asyncio.create_task(tasks.wait_all_background_tasks(timeout=5.0))
    asyncio.get_running_loop().call_soon(release_parent.set)
    await child_entered.wait()
    clock[0] = 6.0
    tasks.safe_create_task(trigger_registry_change())
    # Garde de test uniquement : les événements et l'horloge contrôlent le scénario.
    await asyncio.wait_for(draining, timeout=1.0)
    assert child_tasks[0].cancelled()
    assert child_cleaned.is_set()
    assert not registry


async def test_expiration_awaits_finally_and_reports_unfinished_cleanup(registry, caplog):
    entered, cleanup_entered, release_cleanup, cleaned = (asyncio.Event() for _ in range(4))

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            await release_cleanup.wait()
            cleaned.set()

    work_task = tasks.safe_create_task(work(), name="synthetic-rollback")
    await entered.wait()
    with caplog.at_level(logging.WARNING, logger="radar.tasks"):
        draining = asyncio.create_task(tasks.wait_all_background_tasks(timeout=0.0))
        try:
            await cleanup_entered.wait()
            await checkpoint()
            assert not draining.done()
            assert not cleaned.is_set()
            assert any("synthetic-rollback" in r.message for r in caplog.records)
            release_cleanup.set()
            await draining
        finally:
            release_cleanup.set()
            await draining
    assert work_task.cancelled()
    assert cleaned.is_set()
    assert not registry


@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_caller_cancellation_is_propagated_after_cleanup(registry, repeat_cancel):
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    task = tasks.safe_create_task(work())
    await entered.wait()
    draining = asyncio.create_task(tasks.wait_all_background_tasks())
    await checkpoint()
    draining.cancel()
    try:
        await cleaning.wait()
        if repeat_cancel:
            draining.cancel()
        await checkpoint()
        assert not draining.done()
        assert task.cancelling() == 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await draining
    assert cleaned.is_set()
    assert not registry


async def test_child_created_during_cancellation_cannot_escape_drain(registry):
    started, child_cleaned = asyncio.Event(), asyncio.Event()
    children = []

    async def child():
        try:
            await asyncio.Event().wait()
        finally:
            child_cleaned.set()

    async def parent():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            children.append(tasks.safe_create_task(child()))

    tasks.safe_create_task(parent())
    await started.wait()
    await asyncio.wait_for(tasks.wait_all_background_tasks(timeout=0.0), timeout=1.0)
    assert children[0].cancelled()
    assert child_cleaned.is_set()
    assert not registry


async def test_drain_never_waits_for_its_own_registered_task(registry):
    draining = tasks.safe_create_task(tasks.wait_all_background_tasks())
    await asyncio.wait_for(draining, timeout=1.0)
    assert not registry

"""Tests unitaires pour le cycle de vie unifié de l'application FastAPI."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot
from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN, SchedulerEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from pydantic import SecretStr

from app.core.config import settings
from app.main import lifespan


def scheduler_double():
    """Le double reproduit l'événement public émis à la fin du shutdown."""
    scheduler = MagicMock()
    listeners = []
    scheduler.add_listener.side_effect = lambda listener, _mask: listeners.append(listener)
    scheduler.shutdown.side_effect = lambda **_kwargs: [
        listener(SchedulerEvent(EVENT_SCHEDULER_SHUTDOWN)) for listener in listeners
    ]
    scheduler._executors = {}
    return scheduler


@pytest.mark.parametrize(
    "missing_secret",
    ["TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "INGEST_API_TOKEN", "INTERNAL_API_TOKEN"],
)
async def test_invalid_production_secrets_prevent_telegram_and_producers(
    monkeypatch, missing_secret,
):
    """Un secret absent refuse le lifespan avant Telegram, scheduler et poller."""
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "VINTED_POLLER_ENABLED", True)
    for name in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "INGEST_API_TOKEN", "INTERNAL_API_TOKEN",
    ):
        monkeypatch.setattr(settings, name, SecretStr("synthetic"))
    monkeypatch.setattr(settings, missing_secret, SecretStr(""))

    with (
        patch("app.main.setup_logging"),
        patch.object(Bot, "set_my_commands", new_callable=AsyncMock) as telegram,
        patch("app.tasks.scheduler.scheduler.start") as start_scheduler,
        patch("app.tasks.vinted_poller.run_vinted_poller", new_callable=AsyncMock) as poller,
        patch("app.core.tasks.safe_create_task") as create_task,
    ):
        with pytest.raises(RuntimeError, match=missing_secret):
            async with lifespan(FastAPI()):
                pytest.fail("Le lifespan invalide ne doit pas accepter de requêtes")

        telegram.assert_not_awaited()
        start_scheduler.assert_not_called()
        poller.assert_not_called()
        create_task.assert_not_called()


@pytest.mark.asyncio
async def test_unified_lifespan_starts_and_stops_services():
    """Vérifie que le lifespan FastAPI démarre et arrête proprement APScheduler et le poller."""
    mock_scheduler = scheduler_double()
    mock_bot = MagicMock()
    mock_poller_task = AsyncMock()

    test_app = FastAPI()

    with (
        patch("app.main.setup_logging"),
        patch("app.bot.bot_instance.bot", mock_bot),
        patch("app.bot.bot_instance.setup_bot_commands", new_callable=AsyncMock) as mock_setup_cmds,
        patch("app.tasks.scheduler.scheduler", mock_scheduler),
        patch("app.tasks.vinted_poller.run_vinted_poller", mock_poller_task),
        patch("app.core.tasks.safe_create_task") as mock_create_task,
        patch("app.core.http_client.close_shared_http_client", new_callable=AsyncMock),
        patch("app.core.redis.close_redis", new_callable=AsyncMock),
        patch("app.core.tasks.wait_all_background_tasks", new_callable=AsyncMock),
        patch("app.core.config.settings.VINTED_POLLER_ENABLED", True),
        patch("app.main.validate_production_secrets"),
    ):
        async def poller_work():
            await asyncio.Event().wait()

        mock_task = asyncio.create_task(poller_work())
        mock_task.cancel = MagicMock(wraps=mock_task.cancel)
        mock_create_task.return_value = mock_task
        # La coroutine d'entrée n'est pas lancée par ce double ; ne pas l'abandonner.
        mock_create_task.side_effect = lambda coro: (coro.close(), mock_task)[1]

        async with lifespan(test_app):
            mock_setup_cmds.assert_called_once_with(mock_bot)
            mock_scheduler.start.assert_called_once()
            mock_create_task.assert_called_once()

        mock_scheduler.shutdown.assert_called_once_with(wait=False)
        mock_task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_starts_scheduler_when_poller_disabled():
    """Le refresh des prix et le nettoyage restent planifiés si le poller est coupé."""
    mock_scheduler = scheduler_double()
    test_app = FastAPI()

    with (
        patch("app.main.setup_logging"),
        patch("app.bot.bot_instance.bot", MagicMock()),
        patch("app.bot.bot_instance.setup_bot_commands", new_callable=AsyncMock),
        patch("app.tasks.scheduler.scheduler", mock_scheduler),
        patch("app.core.tasks.safe_create_task") as mock_create_task,
        patch("app.core.http_client.close_shared_http_client", new_callable=AsyncMock),
        patch("app.core.redis.close_redis", new_callable=AsyncMock),
        patch("app.core.tasks.wait_all_background_tasks", new_callable=AsyncMock),
        patch("app.core.config.settings.VINTED_POLLER_ENABLED", False),
        patch("app.main.validate_production_secrets"),
    ):
        async with lifespan(test_app):
            mock_scheduler.start.assert_called_once()
            mock_create_task.assert_not_called()


async def test_scheduler_cleanup_finishes_before_drain_and_client_closure(monkeypatch):
    """Un job annulé peut produire un enfant dans son finally : les clients restent ouverts."""
    from app.core import tasks

    registered = set()
    monkeypatch.setattr(tasks, "_background_tasks", registered)
    scheduler = AsyncIOScheduler()
    entered, stop, cleaning, release_cleanup, child_finished = (
        asyncio.Event() for _ in range(5)
    )
    state = {"http_closed": False, "redis_closed": False}
    order = []

    async def child():
        assert not state["http_closed"]
        assert not state["redis_closed"]
        order.append("child")
        child_finished.set()

    async def producer():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()
            assert not state["http_closed"]
            assert not state["redis_closed"]
            tasks.safe_create_task(child())
            order.append("producer-cleaned")

    async def close_http():
        assert child_finished.is_set()
        state["http_closed"] = True
        order.append("http-closed")

    async def close_redis():
        state["redis_closed"] = True
        order.append("redis-closed")

    scheduler.add_job(producer, "date", run_date=datetime.now(UTC))
    with (
        patch("app.main.setup_logging"),
        patch("app.main.validate_production_secrets"),
        patch("app.bot.bot_instance.setup_bot_commands", new_callable=AsyncMock),
        patch("app.tasks.scheduler.scheduler", scheduler),
        patch("app.core.config.settings.VINTED_POLLER_ENABLED", False),
        patch("app.core.http_client.close_shared_http_client", close_http),
        patch("app.core.redis.close_redis", close_redis),
    ):
        async def application():
            async with lifespan(FastAPI()):
                await entered.wait()
                await stop.wait()

        running = asyncio.create_task(application())
        await entered.wait()
        stop.set()
        try:
            await cleaning.wait()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
            assert not running.done()
            assert not state["http_closed"]
            assert not state["redis_closed"]
            release_cleanup.set()
            await running
        finally:
            release_cleanup.set()
            await running
    assert order == ["producer-cleaned", "child", "http-closed", "redis-closed"]
    assert not scheduler.running
    assert not registered


async def test_cancelling_shutdown_does_not_recancel_the_poller_cleanup(monkeypatch):
    """L'annulation du lifespan remonte sans interrompre une seconde fois le finally."""
    from app.core import tasks

    registered = set()
    monkeypatch.setattr(tasks, "_background_tasks", registered)
    scheduler = AsyncIOScheduler()
    entered, stop, cleaning, release, cleaned = (asyncio.Event() for _ in range(5))
    closed = []

    async def poller(_stop_event):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            assert not closed
            cleaned.set()

    async def close_http():
        closed.append("http")

    async def close_redis():
        closed.append("redis")

    with (
        patch("app.main.setup_logging"),
        patch("app.main.validate_production_secrets"),
        patch("app.bot.bot_instance.setup_bot_commands", new_callable=AsyncMock),
        patch("app.tasks.scheduler.scheduler", scheduler),
        patch("app.tasks.vinted_poller.run_vinted_poller", poller),
        patch("app.core.config.settings.VINTED_POLLER_ENABLED", True),
        patch("app.core.http_client.close_shared_http_client", close_http),
        patch("app.core.redis.close_redis", close_redis),
    ):
        async def application():
            async with lifespan(FastAPI()):
                await entered.wait()
                await stop.wait()

        running = asyncio.create_task(application())
        await entered.wait()
        poller_task = next(iter(registered))
        stop.set()
        await cleaning.wait()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        running.cancel()
        try:
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
            assert not running.done()
            assert not poller_task.done()
            assert poller_task.cancelling() == 1
            assert not closed
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await running
            await asyncio.wait([poller_task])
    assert cleaned.is_set()
    assert closed == ["http", "redis"]
    assert not registered


@pytest.mark.parametrize("stage", ["drain", "http"])
async def test_shutdown_cancellation_still_drains_and_closes_clients(monkeypatch, stage):
    from app.core import tasks

    registered = set()
    monkeypatch.setattr(tasks, "_background_tasks", registered)
    scheduler = AsyncIOScheduler()
    entered, stop, release_work, drain_started, http_started, release_http = (
        asyncio.Event() for _ in range(6)
    )
    closed = []
    work_finished = asyncio.Event()
    original_drain = tasks.wait_all_background_tasks

    async def work():
        entered.set()
        await release_work.wait()
        work_finished.set()

    async def observed_drain(timeout):
        drain_started.set()
        await original_drain(timeout=timeout)

    async def close_http():
        assert work_finished.is_set()
        http_started.set()
        await release_http.wait()
        closed.append("http")

    async def close_redis():
        assert closed == ["http"]
        closed.append("redis")

    with (
        patch("app.main.setup_logging"),
        patch("app.main.validate_production_secrets"),
        patch("app.bot.bot_instance.setup_bot_commands", new_callable=AsyncMock),
        patch("app.tasks.scheduler.scheduler", scheduler),
        patch("app.core.config.settings.VINTED_POLLER_ENABLED", False),
        patch("app.core.tasks.wait_all_background_tasks", observed_drain),
        patch("app.core.http_client.close_shared_http_client", close_http),
        patch("app.core.redis.close_redis", close_redis),
    ):
        async def application():
            async with lifespan(FastAPI()):
                tasks.safe_create_task(work())
                await stop.wait()

        running = asyncio.create_task(application())
        await entered.wait()
        stop.set()
        await drain_started.wait()
        if stage == "http":
            release_work.set()
            await http_started.wait()
        running.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        running.cancel()
        assert not running.done()
        assert not closed
        release_work.set()
        release_http.set()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert work_finished.is_set()
    assert closed == ["http", "redis"]
    assert not registered

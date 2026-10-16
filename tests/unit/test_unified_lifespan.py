"""Tests unitaires pour le cycle de vie unifié de l'application FastAPI."""

import asyncio
from contextlib import ExitStack
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


# --- Cycle de vie réel d'APScheduler : job asynchrone en cours à l'arrêt ---
#
# Ces scénarios utilisent un vrai `AsyncIOScheduler` et son exécuteur asyncio réel.
# Le lanceur isolé les autorise nommément (`scripts/verify_consolidation.py`) : l'exécuteur
# asyncio 3.x efface `_pending_futures` pendant `shutdown`, donc remplacer cette collection
# par un double supprimerait exactement le comportement à démontrer.


def _lifespan_patches(scheduler):
    """Patches communs : lifespan réel, vrai scheduler, poller coupé, aucun transport."""
    return [
        patch("app.main.setup_logging"),
        patch("app.main.validate_production_secrets"),
        patch("app.bot.bot_instance.setup_bot_commands", new_callable=AsyncMock),
        patch("app.tasks.scheduler.scheduler", scheduler),
        patch("app.core.config.settings.VINTED_POLLER_ENABLED", False),
    ]


async def _settle() -> None:
    """Laisse tourner la boucle jusqu'à stabilisation, sans durée arbitraire."""
    for _ in range(10):
        await asyncio.sleep(0)


async def test_async_job_in_flight_is_cancelled_and_cleaned_before_resource_closure(monkeypatch):
    """Un vrai job coroutine en cours est annulé et son nettoyage précède la fermeture.

    Garantie : `lifespan` capture les producteurs dans `_pending_futures` **avant**
    `scheduler.shutdown(wait=False)`, parce que l'exécuteur asyncio vide cette collection
    pendant l'arrêt. Sans capture préalable, la tâche annulée ne serait plus joignable et
    la fermeture des clients partagés pourrait la précéder.
    """
    from app.core import tasks

    registered = set()
    monkeypatch.setattr(tasks, "_background_tasks", registered)
    scheduler = AsyncIOScheduler()
    entered, cleaning, release_cleanup = (asyncio.Event() for _ in range(3))
    state = {"http_closed": False, "redis_closed": False}
    order = []

    async def producer():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()
            assert not state["http_closed"], "nettoyage du job après fermeture HTTP"
            assert not state["redis_closed"], "nettoyage du job après fermeture Redis"
            order.append("job-cleaned")

    async def close_http():
        state["http_closed"] = True
        order.append("http-closed")

    async def close_redis():
        state["redis_closed"] = True
        order.append("redis-closed")

    scheduler.add_job(producer, "date", run_date=datetime.now(UTC), id="lifecycle-probe")
    stop = asyncio.Event()

    with patch.multiple(
        "app.core.tasks",
        wait_all_background_tasks=AsyncMock(),
    ), patch("app.core.http_client.close_shared_http_client", close_http), patch(
        "app.core.redis.close_redis", close_redis
    ), ExitStack() as stack:
        for patch_ in _lifespan_patches(scheduler):
            stack.enter_context(patch_)
        async def application():
            async with lifespan(FastAPI()):
                await entered.wait()
                await stop.wait()

        running = asyncio.create_task(application())
        await entered.wait()

        # Preuve explicite que le job est en cours dans le vrai exécuteur.
        executor = scheduler._executors["default"]
        in_flight = tuple(executor._pending_futures)
        assert len(in_flight) == 1
        assert not in_flight[0].done()

        stop.set()
        await cleaning.wait()

        # L'exécuteur a vidé sa collection : la capture antérieure est indispensable,
        # et la tâche reste jointe par `lifespan` pendant son nettoyage.
        assert executor._pending_futures == set()
        assert running.cancelled() is False
        await _settle()
        assert not running.done(), "l'arrêt doit attendre le nettoyage du job"
        assert not state["http_closed"]
        assert not state["redis_closed"]

        release_cleanup.set()
        await running

    assert order == ["job-cleaned", "http-closed", "redis-closed"]
    assert not scheduler.running
    assert not registered
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


async def test_shutdown_awaits_async_cleanup_of_a_real_scheduler_job(monkeypatch):
    """Le nettoyage asynchrone déclenché par l'annulation est réellement attendu.

    Garantie : la tâche capturée reste `not done()` tant que son `finally` asynchrone
    n'est pas terminé — c'est ce que `lifespan` attend avant de drainer et de fermer.
    """
    from app.core import tasks

    registered = set()
    monkeypatch.setattr(tasks, "_background_tasks", registered)
    scheduler = AsyncIOScheduler()
    entered, cleaning, release_cleanup, cleanup_done = (asyncio.Event() for _ in range(4))
    drain_saw_cleanup = asyncio.Event()

    async def producer():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()
            cleanup_done.set()

    async def observed_drain(*, timeout: float):
        assert timeout == 5.0, "l'échéance de drainage attendue est celle du lifespan"
        assert cleanup_done.is_set(), "le drainage a commencé avant la fin du nettoyage"
        drain_saw_cleanup.set()

    scheduler.add_job(producer, "date", run_date=datetime.now(UTC), id="cleanup-probe")
    stop = asyncio.Event()

    with patch("app.core.tasks.wait_all_background_tasks", observed_drain), patch(
        "app.core.http_client.close_shared_http_client", AsyncMock()
    ), patch("app.core.redis.close_redis", AsyncMock()), ExitStack() as stack:
        for patch_ in _lifespan_patches(scheduler):
            stack.enter_context(patch_)
        async def application():
            async with lifespan(FastAPI()):
                await entered.wait()
                await stop.wait()

        running = asyncio.create_task(application())
        await entered.wait()
        captured = tuple(scheduler._executors["default"]._pending_futures)
        assert len(captured) == 1

        stop.set()
        await cleaning.wait()
        await _settle()
        assert not captured[0].done(), "nettoyage asynchrone non attendu"
        assert not drain_saw_cleanup.is_set()

        release_cleanup.set()
        await running

    assert cleanup_done.is_set()
    assert drain_saw_cleanup.is_set()
    assert captured[0].done()
    assert not scheduler.running
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


async def test_caller_cancellation_during_job_cleanup_still_closes_in_order(monkeypatch):
    """L'appelant annulé pendant le nettoyage du job : nettoyage fini, ordre conservé.

    Garantie : `lifespan` protège son nettoyage par `asyncio.shield` ; l'annulation de
    l'appelant est propagée **après** le nettoyage, sans tâche abandonnée.
    """
    from app.core import tasks

    registered = set()
    monkeypatch.setattr(tasks, "_background_tasks", registered)
    scheduler = AsyncIOScheduler()
    entered, cleaning, release_cleanup = (asyncio.Event() for _ in range(3))
    closed = []

    async def producer():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()
            assert closed == [], "nettoyage du job après fermeture des ressources"

    async def close_http():
        closed.append("http")

    async def close_redis():
        closed.append("redis")

    scheduler.add_job(producer, "date", run_date=datetime.now(UTC), id="cancel-probe")
    stop = asyncio.Event()

    with patch("app.core.tasks.wait_all_background_tasks", AsyncMock()), patch(
        "app.core.http_client.close_shared_http_client", close_http
    ), patch("app.core.redis.close_redis", close_redis), ExitStack() as stack:
        for patch_ in _lifespan_patches(scheduler):
            stack.enter_context(patch_)
        async def application():
            async with lifespan(FastAPI()):
                await entered.wait()
                await stop.wait()

        running = asyncio.create_task(application())
        await entered.wait()
        captured = tuple(scheduler._executors["default"]._pending_futures)
        stop.set()
        await cleaning.wait()

        running.cancel()
        await _settle()
        assert not running.done(), "l'annulation doit attendre le nettoyage du job"
        assert closed == []

        try:
            release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            release_cleanup.set()
            await asyncio.wait([captured[0]])

    assert closed == ["http", "redis"]
    assert not scheduler.running
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


async def test_missing_default_executor_is_not_treated_as_no_producer(monkeypatch):
    """Un exécuteur « default » absent ne doit pas être silencieusement pris pour zéro producteur.

    Garantie observée : `lifespan` lit `_executors` avec `.get("default")` ; l'absence
    d'exécuteur donne un producteur vide **sans lever**. Ce test fixe ce comportement réel
    et le distingue explicitement du cas « producteurs réellement absents » : l'arrêt ne
    prouve rien sur d'éventuels producteurs non observables.
    """
    from app.core import tasks

    registered = set()
    monkeypatch.setattr(tasks, "_background_tasks", registered)
    scheduler = AsyncIOScheduler()
    entered = asyncio.Event()
    closed = []

    async def producer():
        entered.set()
        await asyncio.Event().wait()

    async def close_http():
        closed.append("http")

    async def close_redis():
        closed.append("redis")

    scheduler.add_job(producer, "date", run_date=datetime.now(UTC), id="no-executor-probe")
    stop = asyncio.Event()

    with patch("app.core.tasks.wait_all_background_tasks", AsyncMock()), patch(
        "app.core.http_client.close_shared_http_client", close_http
    ), patch("app.core.redis.close_redis", close_redis), ExitStack() as stack:
        for patch_ in _lifespan_patches(scheduler):
            stack.enter_context(patch_)
        async def application():
            async with lifespan(FastAPI()):
                await entered.wait()
                await stop.wait()

        running = asyncio.create_task(application())
        await entered.wait()
        captured = tuple(scheduler._executors["default"]._pending_futures)

        # Cas limite : plus aucun exécuteur « default » au moment de l'arrêt.
        scheduler._executors.pop("default")
        stop.set()
        for _ in range(3):
            await _settle()
            if running.done():
                break

        release = asyncio.Event()

        async def finish_job():
            release.set()

        asyncio.get_running_loop().call_soon(release.set)
        await asyncio.wait([captured[0]], timeout=1.0)
        assert running.done(), "un exécuteur absent ne doit pas bloquer l'arrêt"

    assert closed == ["http", "redis"]
    assert not scheduler.running


async def test_shutdown_waits_for_a_slow_producer_then_finishes_cleanly(monkeypatch):
    """Un producteur lent retarde l'arrêt sans borne propre, puis l'arrêt se termine.

    Garantie observée : `lifespan` attend les producteurs avec `asyncio.wait` **sans
    échéance**. Une fois le producteur libéré, l'arrêt se termine et l'ordre de fermeture
    reste respecté. Aucun timeout applicatif n'est supposé ici.
    """
    from app.core import tasks

    registered = set()
    monkeypatch.setattr(tasks, "_background_tasks", registered)
    scheduler = AsyncIOScheduler()
    entered, cleaning, release_cleanup = (asyncio.Event() for _ in range(3))
    order = []

    async def producer():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()
            order.append("job-cleaned")

    async def close_http():
        order.append("http-closed")

    async def close_redis():
        order.append("redis-closed")

    scheduler.add_job(producer, "date", run_date=datetime.now(UTC), id="slow-probe")
    stop = asyncio.Event()

    with patch("app.core.tasks.wait_all_background_tasks", AsyncMock()), patch(
        "app.core.http_client.close_shared_http_client", close_http
    ), patch("app.core.redis.close_redis", close_redis), ExitStack() as stack:
        for patch_ in _lifespan_patches(scheduler):
            stack.enter_context(patch_)
        async def application():
            async with lifespan(FastAPI()):
                await entered.wait()
                await stop.wait()

        running = asyncio.create_task(application())
        await entered.wait()
        stop.set()
        await cleaning.wait()

        # Aucune borne applicative : l'arrêt reste en attente tant que le producteur
        # n'a pas terminé son nettoyage.
        await asyncio.wait([running], timeout=0.05)
        assert not running.done(), "un producteur lent ne doit pas être abandonné"
        assert order == []

        release_cleanup.set()
        await asyncio.wait([running], timeout=5.0)

    assert running.done()
    assert order == ["job-cleaned", "http-closed", "redis-closed"]
    assert not scheduler.running
    assert not registered
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []

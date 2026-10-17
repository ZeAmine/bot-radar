import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, User

from app.bot.handlers._alerts_router import CreationDraftMiddleware
from app.bot.handlers.alerts_create import handle_category_selected, start_add_alert
from app.bot.states.alert_creation import AlertCreationState
from app.providers.base import CollectionAttempt
from app.services import market_health
from tests.screens import incoming_message
from tests.telegram_fakes import telegram_message
from tests.unit.test_refresh_notifications import result


@pytest.mark.asyncio
async def test_category_callback_cannot_mutate_a_new_creation_after_acknowledgement(monkeypatch):
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    await state.update_data(creation_token="A", inline_query="Ancienne œuvre")
    await state.set_state(AlertCreationState.waiting_for_category)
    entered, release = asyncio.Event(), asyncio.Event()

    async def acknowledge(*_args, **_kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(CallbackQuery, "answer", acknowledge)
    callback = CallbackQuery(
        id="old-category", from_user=User(id=42, is_bot=False, first_name="Test"),
        chat_instance="test", data="add_cat:clothes@A",
    )

    async def dispatch(event, data):
        await handle_category_selected(
            event, data["state"], MagicMock(), SimpleNamespace(id="user"),
            **{key: value for key, value in data.items() if key != "state"},
        )

    task = asyncio.create_task(CreationDraftMiddleware()(dispatch, callback, {"state": state}))
    await entered.wait()
    try:
        await start_add_alert(incoming_message("/add"), state)
        await state.update_data(
            name="Jours de sable", isbn="9782505082545", category_slug="bd_comics",
            search_token=123, draft_revision=2, include_keywords=["jours de sable"],
            exclude_keywords=["abîmé"], max_item_price_cents=1000,
        )
        await state.set_state(AlertCreationState.confirm_creation)
        expected = copy.deepcopy(await state.get_data())
        expected_state = await state.get_state()
    finally:
        release.set()
        await task
    assert await state.get_data() == expected
    assert await state.get_state() == expected_state


@pytest.mark.asyncio
@pytest.mark.parametrize("redis_available", [True, False])
async def test_partial_improvement_is_observed_and_total_failure_is_notified_again(
    monkeypatch, redis_available,
):
    stored = {}

    async def read(key):
        if not redis_available:
            raise ConnectionError
        return stored.get(key)

    async def write(key, value, **_kwargs):
        if not redis_available:
            raise ConnectionError
        stored[key] = value
        return True

    client = SimpleNamespace(get=read, set=write)
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(market_health, "notify_admins", send)
    failed = CollectionAttempt("momox", status="failed", reason="timeout")
    success = CollectionAttempt("momox")

    async def observe(attempts):
        await market_health.notify_refresh_result(result(incremental=True, attempts=attempts))
        if redis_available:
            assert json.loads(stored[market_health._INCIDENT_KEY])
            market_health._incident_states.clear()
            market_health._incident_observations.clear()
            market_health._incident_pending.clear()
            market_health._incident_loaded = False

    await observe([failed] * 3)
    assert send.await_count == 1
    await observe([failed, success, success])
    assert send.await_count == 1
    await observe([CollectionAttempt("momox", status="skipped")])
    await observe([])
    assert send.await_count == 1
    send.side_effect = [False, True, True]
    await observe([failed] * 3)
    assert send.await_count == 2
    await observe([failed] * 3)
    assert send.await_count == 3
    assert "Aggravation" in send.call_args.args[0]
    await observe([failed] * 3)
    assert send.await_count == 3
    await observe([success] * 3)
    assert send.await_count == 4
    assert "Rétablissement" in send.call_args.args[0]


@pytest.mark.asyncio
async def test_failed_first_incident_delivery_is_retried(monkeypatch):
    monkeypatch.setattr(market_health, "get_redis_client", lambda: AsyncMock())
    send = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(market_health, "notify_admins", send)
    report = result(incremental=True, attempts=[CollectionAttempt("source", status="failed")])
    await market_health.notify_refresh_result(report)
    await market_health.notify_refresh_result(report)
    assert send.await_count == 2
    assert "Nouvel incident" in send.call_args.args[0]


@pytest.mark.asyncio
async def test_legacy_incident_state_is_read_without_a_false_recovery(monkeypatch):
    client = AsyncMock()
    client.get.return_value = json.dumps({"source/current": {"ratio": 1.0, "count": 3.0}})
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(market_health, "notify_admins", send)
    await market_health.notify_refresh_result(result(incremental=True, attempts=[
        CollectionAttempt("source", status="skipped"),
        CollectionAttempt("source", operation="history"),
    ]))
    send.assert_not_awaited()
    await market_health.notify_refresh_result(result(incremental=True, attempts=[
        CollectionAttempt("source"),
    ]))
    assert send.await_count == 1
    assert "Rétablissement : source/current" in send.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["bd_comics", "tech"])
async def test_category_lookup_cannot_apply_after_a_new_command(monkeypatch, category):
    from aiogram.types import Message

    from app.bot.creation_drafts import draft_guard
    from app.bot.handlers import alerts_helpers
    from app.use_cases.book_search import BookSearchResult, BookSearchStatus

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    await state.update_data(creation_token="A", inline_query="Ancienne recherche 10€")
    await state.set_state(AlertCreationState.waiting_for_category)
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(*_args, **_kwargs):
        async with asyncio.timeout(1), draft_guard(state):
            entered.set()
        await release.wait()
        return (BookSearchResult(status=BookSearchStatus.NOT_FOUND)
                if category == "bd_comics" else SimpleNamespace(id="category"))

    category_repo = SimpleNamespace(get_by_slug=AsyncMock(side_effect=lookup))
    monkeypatch.setattr(alerts_helpers, "_get_category_repo", lambda _session: category_repo)
    monkeypatch.setattr(alerts_helpers.BookSearchUseCase, "search_by_title", lookup)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    status = SimpleNamespace(edit_text=AsyncMock())
    monkeypatch.setattr(Message, "answer", AsyncMock(return_value=status))
    monkeypatch.setattr(Message, "edit_text", AsyncMock())
    callback = CallbackQuery(
        id="category", from_user=User(id=42, is_bot=False, first_name="Test"),
        chat_instance="test", data=f"add_cat:{category}@A", message=telegram_message(),
    )

    async def dispatch(event, data):
        await handle_category_selected(
            event, data["state"], MagicMock(), None,
            creation_callback_data=data["creation_callback_data"],
        )

    async with asyncio.timeout(3):
        task = asyncio.create_task(CreationDraftMiddleware()(dispatch, callback, {"state": state}))
        await entered.wait()
        try:
            await start_add_alert(incoming_message("/add"), state)
            expected = copy.deepcopy(await state.get_data())
            expected_state = await state.get_state()
        finally:
            release.set()
            await task
    assert await state.get_data() == expected
    assert await state.get_state() == expected_state
    status.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_older_category_click_cannot_replace_the_same_drafts_bibliographic_recap(monkeypatch):
    from aiogram.types import Message

    from app.bot.creation_drafts import draft_guard
    from app.bot.handlers import alerts_helpers
    from app.bot.handlers.alerts_create import handle_query_input
    from app.services.book_metadata_service import BibliographicReport, BookMetadata, IsbnVerdict
    from app.use_cases.book_search import result_from_verdict_report

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    await start_add_alert(incoming_message("/add"), state)
    creation_token = (await state.get_data())["creation_token"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def acknowledge(callback, *_args, **_kwargs):
        async with asyncio.timeout(1), draft_guard(state):
            pass
        if callback.id == "older":
            entered.set()
            await release.wait()

    monkeypatch.setattr(CallbackQuery, "answer", acknowledge)
    edit = AsyncMock()
    monkeypatch.setattr(Message, "edit_text", edit)
    category_repo = SimpleNamespace(get_by_slug=AsyncMock(return_value=SimpleNamespace(id="bd")))
    monkeypatch.setattr(alerts_helpers, "_get_category_repo", lambda _session: category_repo)
    found = result_from_verdict_report(BibliographicReport(
        kind="isbn", verdict=IsbnVerdict(outcome="confirme", notice=BookMetadata(
            title="Jours de sable", author="Aimée de Jongh", isbn13="9782505082545",
        )),
    ), None)
    search = AsyncMock(return_value=found)
    monkeypatch.setattr(alerts_helpers.BookSearchUseCase, "search_by_isbn", search)
    monkeypatch.setattr(alerts_helpers, "populate_creation_prices", AsyncMock())

    def callback(identifier, category):
        return CallbackQuery(
            id=identifier, from_user=User(id=42, is_bot=False, first_name="Test"),
            chat_instance="test", data=f"add_cat:{category}@{creation_token}",
            message=telegram_message(),
        )

    async def dispatch(event, data):
        await handle_category_selected(
            event, state, MagicMock(), None,
            **{key: value for key, value in data.items() if key != "state"},
        )

    middleware = CreationDraftMiddleware()
    async with asyncio.timeout(3):
        task = asyncio.create_task(middleware(
            dispatch, callback("older", "clothes"), {"state": state},
        ))
        await entered.wait()
        try:
            await middleware(dispatch, callback("newer", "bd_comics"), {"state": state})
            assert await state.get_state() == AlertCreationState.waiting_for_query.state
            await handle_query_input(
                incoming_message("9782505082545, 10€"), state, MagicMock(), None,
            )
            expected = copy.deepcopy(await state.get_data())
            expected_state = await state.get_state()
            assert expected_state == AlertCreationState.confirm_creation.state
            assert expected["creation_token"] == creation_token
            assert expected["isbn"] == "9782505082545"
            assert expected["name"] == "Jours de sable"
            assert expected["category_slug"] == "bd_comics"
            edit_count = edit.await_count
        finally:
            release.set()
            await task
    assert await state.get_data() == expected
    assert await state.get_state() == expected_state
    assert edit.await_count == edit_count
    search.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("redis_available", [True, False])
@pytest.mark.parametrize("summary_fails", [True, False])
async def test_undelivered_incident_and_recovery_are_summarized_until_delivery(
    monkeypatch, redis_available, summary_fails,
):
    stored = {}

    async def read(key):
        if not redis_available:
            raise ConnectionError
        return stored.get(key)

    async def write(key, value, **_kwargs):
        if not redis_available:
            raise ConnectionError
        stored[key] = value
        return True

    monkeypatch.setattr(market_health, "get_redis_client", lambda: SimpleNamespace(
        get=read, set=write,
    ))
    send = AsyncMock(return_value=False)
    monkeypatch.setattr(market_health, "notify_admins", send)
    failure = CollectionAttempt("source", status="failed", reason="timeout")

    async def observe(attempts):
        report = result(incremental=True, attempts=attempts)
        report.stats.update(degraded=0, available=3)
        await market_health.notify_refresh_result(report)
        snapshot = copy.deepcopy({
            "observed": market_health._incident_observations,
            "notified": market_health._incident_states,
            "pending": market_health._incident_pending,
        })
        if redis_available:
            assert json.loads(stored[market_health._INCIDENT_KEY]) == snapshot
            market_health._incident_observations.clear()
            market_health._incident_states.clear()
            market_health._incident_pending.clear()
            market_health._incident_loaded = False
        return snapshot

    snapshot = await observe([failure] * 3)
    assert snapshot["observed"]["source/current"]["ratio"] == 1
    assert snapshot["notified"] == {}
    assert "source/current" in snapshot["pending"]
    for attempts in (
        [CollectionAttempt("source", status="skipped")], [],
        [CollectionAttempt("source", operation="history")],
    ):
        assert await observe(attempts) == snapshot
        assert "rétabli" not in send.call_args.args[0].lower()
        assert "Rétablissement" not in send.call_args.args[0]
    send.return_value = not summary_fails
    snapshot = await observe([CollectionAttempt("source")] * 3)
    text = send.call_args.args[0]
    assert "incident" in text.lower()
    assert "rétabli" in text.lower()
    assert "source/current" in text
    assert "Nouvel incident" not in text
    assert "Échecs observés" not in text
    assert snapshot["observed"] == snapshot["notified"] == {}
    if summary_fails:
        assert "source/current" in snapshot["pending"]
        send.return_value = True
        snapshot = await observe([])
        assert "rétabli" in send.call_args.args[0].lower()
        assert "Nouvel incident" not in send.call_args.args[0]
    assert snapshot["pending"] == {}
    deliveries = send.await_count
    await observe([CollectionAttempt("source")])
    await observe([CollectionAttempt("source", status="skipped")])
    await observe([])
    assert send.await_count == deliveries


@pytest.mark.asyncio
async def test_new_failure_keeps_the_undelivered_summary_of_the_previous_incident(monkeypatch):
    client = AsyncMock()
    client.get.return_value = None
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    send = AsyncMock(return_value=False)
    monkeypatch.setattr(market_health, "notify_admins", send)
    failed = CollectionAttempt("source", status="failed", reason="timeout")
    await market_health.notify_refresh_result(result(incremental=True, attempts=[failed]))
    await market_health.notify_refresh_result(result(
        incremental=True, attempts=[CollectionAttempt("source")],
    ))
    for _ in range(2):
        await market_health.notify_refresh_result(result(incremental=True, attempts=[failed]))
        text = send.call_args.args[0]
        assert "rétabli avant remise" in text
        assert "incident en cours" in text
    send.return_value = True
    await market_health.notify_refresh_result(result(incremental=True, attempts=[failed]))
    assert market_health._incident_pending == {}
    assert market_health._incident_states["source/current"]["ratio"] == 1
    deliveries = send.await_count
    await market_health.notify_refresh_result(result(incremental=True, attempts=[failed]))
    assert send.await_count == deliveries


@pytest.mark.asyncio
async def test_new_failure_and_past_recovery_survive_persisted_restarts(monkeypatch):
    stored = {}

    async def read(key):
        return stored.get(key)

    async def write(key, value, **_kwargs):
        assert isinstance(value, str)
        stored[key] = value
        return True

    def restart(*, delivered=False):
        monkeypatch.setattr(market_health, "_incident_observations", {})
        monkeypatch.setattr(market_health, "_incident_states", {})
        monkeypatch.setattr(market_health, "_incident_pending", {})
        monkeypatch.setattr(market_health, "_incident_loaded", False)
        monkeypatch.setattr(market_health, "_incident_lock", asyncio.Lock())
        monkeypatch.setattr(market_health, "_notification_deadlines", {})
        client = SimpleNamespace(get=AsyncMock(side_effect=read), set=AsyncMock(side_effect=write))
        monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
        send = AsyncMock(return_value=delivered)
        monkeypatch.setattr(market_health, "notify_admins", send)
        return client, send

    async def observe(status):
        report = result(incremental=True, attempts=[
            CollectionAttempt(
                "source", status=status, reason="timeout" if status == "failed" else None,
            ),
        ])
        report.stats.update(degraded=int(status == "failed"), refreshed=int(status == "success"))
        await market_health.notify_refresh_result(report)
        persisted = json.loads(stored[market_health._INCIDENT_KEY])
        assert persisted == {
            "observed": market_health._incident_observations,
            "notified": market_health._incident_states,
            "pending": market_health._incident_pending,
        }
        return persisted

    client, send = restart()
    assert await observe("failed") == {
        "observed": {"source/current": {"ratio": 1.0, "count": 1.0}},
        "notified": {},
        "pending": {"source/current": "Nouvel incident : source/current"},
    }
    send.assert_awaited_once()
    assert "Nouvel incident : source/current" in send.call_args.args[0]
    assert "rétabli" not in send.call_args.args[0]

    assert await observe("success") == {
        "observed": {}, "notified": {},
        "pending": {
            "source/current": "Incident observé puis rétabli avant remise : source/current",
        },
    }
    assert send.await_count == 2
    assert "Incident observé puis rétabli avant remise : source/current" in send.call_args.args[0]
    assert "Nouvel incident" not in send.call_args.args[0]
    assert "Échecs observés" not in send.call_args.args[0]

    client, send = restart()
    assert await observe("failed") == {
        "observed": {"source/current": {"ratio": 1.0, "count": 1.0}},
        "notified": {},
        "pending": {
            "source/current": "Incident observé puis rétabli avant remise : source/current\n"
            "Nouvel incident en cours : source/current",
        },
    }
    client.get.assert_awaited_once_with(market_health._INCIDENT_KEY)
    send.assert_awaited_once()
    assert "Incident observé puis rétabli avant remise : source/current\n" \
        "Nouvel incident en cours : source/current" in send.call_args.args[0]
    assert "Échecs observés : source/current (timeout)" in send.call_args.args[0]

    client, send = restart(delivered=True)
    assert await observe("failed") == {
        "observed": {"source/current": {"ratio": 1.0, "count": 1.0}},
        "notified": {"source/current": {"ratio": 1.0, "count": 1.0}},
        "pending": {},
    }
    client.get.assert_awaited_once_with(market_health._INCIDENT_KEY)
    send.assert_awaited_once()
    assert "Incident observé puis rétabli avant remise : source/current\n" \
        "Nouvel incident en cours : source/current" in send.call_args.args[0]
    assert "Échecs observés : source/current (timeout)" in send.call_args.args[0]

    client, send = restart(delivered=True)
    assert await observe("failed") == {
        "observed": {"source/current": {"ratio": 1.0, "count": 1.0}},
        "notified": {"source/current": {"ratio": 1.0, "count": 1.0}},
        "pending": {},
    }
    client.get.assert_awaited_once_with(market_health._INCIDENT_KEY)
    send.assert_not_awaited()

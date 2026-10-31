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


def _redis_client(monkeypatch, stored, *, available=True):
    """Un Redis de test adossé à un dictionnaire ; injoignable quand on le demande."""

    async def read(key):
        if not available:
            raise ConnectionError
        return stored.get(key)

    async def write(key, value, **_kwargs):
        if not available:
            raise ConnectionError
        stored[key] = value
        return True

    client = SimpleNamespace(
        get=AsyncMock(side_effect=read), set=AsyncMock(side_effect=write),
    )
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    return client


def _forget_local_state(monkeypatch):
    """Ce qu'un redémarrage efface : l'état local, relu depuis l'état partagé."""
    monkeypatch.setattr(market_health, "_incident_sources", {})
    monkeypatch.setattr(market_health, "_incident_announced", set())
    monkeypatch.setattr(market_health, "_incident_pending", {})
    monkeypatch.setattr(market_health, "_incident_loaded", False)
    monkeypatch.setattr(market_health, "_incident_lock", asyncio.Lock())


def _restart(monkeypatch, stored, *, livraison=True):
    _forget_local_state(monkeypatch)
    client = _redis_client(monkeypatch, stored)
    send = AsyncMock(return_value=livraison)
    monkeypatch.setattr(market_health, "notify_admins", send)
    return client, send


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
async def test_durability_is_required_in_both_directions_across_restarts(
    monkeypatch, market_clock, redis_available,
):
    """Le contrat tient par Redis : un redémarrage ne rejoue ni ne perd un épisode.

    Deux passes ne disent rien, trois échecs sur une heure disent un incident ; une
    réponse isolée ne rétablit rien, trois réponses sur une heure si. Après un
    rétablissement, un nouvel épisode se dit : c'est un autre fait.
    """
    stored = {}
    client = _redis_client(monkeypatch, stored, available=redis_available)
    assert client is not None
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(market_health, "notify_admins", send)
    failure = CollectionAttempt("source", status="failed", reason="timeout")
    answer = CollectionAttempt("source")

    async def observe(attempts):
        market_clock.advance(30)
        await market_health.notify_refresh_result(result(attempts=attempts))
        if redis_available:
            _forget_local_state(monkeypatch)

    await observe([failure])
    await observe([failure])
    assert send.await_count == 0, "deux passes ne font pas une panne"
    await observe([failure])
    assert send.await_count == 1
    assert send.call_args.args[0].startswith("Nouvel incident : source/current")
    for attempts in (
        [CollectionAttempt("source", status="skipped", reason="pass_budget")],
        [],
        [failure],
        [CollectionAttempt("source", operation="history", status="failed", reason="timeout")],
    ):
        await observe(attempts)
    assert send.await_count == 1, "l'incident se tait, une opération voisine ne le rouvre pas"
    await observe([answer])
    await observe([answer])
    assert send.await_count == 1, "un retour non durable ne rétablit rien"
    await observe([answer])
    assert send.await_count == 2
    assert "Rétablissement : source/current" in send.call_args.args[0]
    await observe([answer])
    assert send.await_count == 2, "un rétablissement ne s'annonce qu'une fois"
    for _ in range(3):
        await observe([failure])
    assert send.await_count == 3, "un nouvel épisode se dit, malgré l'épisode passé"


@pytest.mark.asyncio
async def test_an_undelivered_incident_is_retried_after_a_restart(monkeypatch, market_clock):
    stored = {}
    _redis_client(monkeypatch, stored)
    send = AsyncMock(return_value=False)
    monkeypatch.setattr(market_health, "notify_admins", send)
    failure = CollectionAttempt("source", status="failed", reason="timeout")

    for _ in range(3):
        market_clock.advance(30)
        await market_health.notify_refresh_result(result(attempts=[failure]))
    assert send.await_count == 1
    assert json.loads(stored[market_health._INCIDENT_KEY])["pending"] != {}

    client, send = _restart(monkeypatch, stored)
    market_clock.advance(30)
    await market_health.notify_refresh_result(result(attempts=[failure]))
    client.get.assert_awaited_once_with(market_health._INCIDENT_KEY)
    assert send.await_count == 1
    assert "Nouvel incident : source/current" in send.call_args.args[0]
    assert json.loads(stored[market_health._INCIDENT_KEY])["pending"] == {}

    client, send = _restart(monkeypatch, stored)
    market_clock.advance(30)
    await market_health.notify_refresh_result(result(attempts=[failure]))
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_state_of_the_previous_format_is_not_resurrected(monkeypatch, market_clock):
    """L'ancien état (ratio/count) ne contient aucune observation datée : il s'ignore.

    Le lire comme un incident en cours produisait un « Rétablissement » fantôme sur
    une source qui n'avait jamais été vue en panne par la nouvelle règle.
    """
    client = AsyncMock()
    client.get.return_value = json.dumps({"source/current": {"ratio": 1.0, "count": 3.0}})
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(market_health, "notify_admins", send)

    market_clock.advance(30)
    await market_health.notify_refresh_result(result(attempts=[
        CollectionAttempt("source", status="skipped"),
        CollectionAttempt("source", operation="history"),
    ]))
    send.assert_not_awaited()
    assert "source/current" not in market_health._incident_sources
    market_clock.advance(30)
    await market_health.notify_refresh_result(result(attempts=[CollectionAttempt("source")]))
    send.assert_not_awaited()


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
async def test_an_undelivered_fact_keeps_its_evidence_until_delivery(
    monkeypatch, market_clock, redis_available,
):
    """Un fait non remis garde de quoi répondre « depuis quand » à la passe suivante."""
    stored = {}
    _redis_client(monkeypatch, stored, available=redis_available)
    send = AsyncMock(return_value=False)
    monkeypatch.setattr(market_health, "notify_admins", send)
    failure = CollectionAttempt("source", status="failed", reason="timeout")

    async def observe(attempts):
        market_clock.advance(30)
        await market_health.notify_refresh_result(result(attempts=attempts))
        snapshot = {
            "source": market_health._incident_sources["source/current"].to_json(),
            "announced": sorted(market_health._incident_announced),
            "pending": {name: list(value)
                        for name, value in market_health._incident_pending.items()},
        }
        if redis_available:
            persisted = json.loads(stored[market_health._INCIDENT_KEY])
            assert persisted["sources"]["source/current"] == snapshot["source"]
            assert persisted["announced"] == snapshot["announced"]
            assert persisted["pending"] == snapshot["pending"]
            _forget_local_state(monkeypatch)
        return snapshot

    for _ in range(3):
        snapshot = await observe([failure])
    assert snapshot["announced"] == ["source/current"]
    assert list(snapshot["pending"]) == ["source/current"]
    for attempts in (
        [CollectionAttempt("source", status="skipped")],
        [],
        [CollectionAttempt("source", operation="history")],
    ):
        assert await observe(attempts) == snapshot
        assert "Rétablissement" not in send.call_args.args[0]

    for _ in range(3):
        snapshot = await observe([CollectionAttempt("source")])
    lines = snapshot["pending"]["source/current"][1].splitlines()
    assert [line.split(" :")[0] for line in lines] == ["Nouvel incident", "Rétablissement"]

    send.return_value = True
    snapshot = await observe([CollectionAttempt("source")])
    assert send.call_args.args[0].splitlines() == lines
    assert snapshot["pending"] == {}
    assert snapshot["announced"] == []
    deliveries = send.await_count
    for _ in range(3):
        await observe([CollectionAttempt("source")])
    assert send.await_count == deliveries, "un rétablissement remis ne se répète pas"


@pytest.mark.asyncio
async def test_a_new_episode_is_appended_to_the_undelivered_one(monkeypatch, market_clock):
    """Trois faits non remis se lisent dans l'ordre, aucun n'écrase l'autre."""
    client = AsyncMock()
    client.get.return_value = None
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    send = AsyncMock(return_value=False)
    monkeypatch.setattr(market_health, "notify_admins", send)
    failure = CollectionAttempt("source", status="failed", reason="timeout")

    async def observe(attempts):
        market_clock.advance(30)
        await market_health.notify_refresh_result(result(attempts=attempts))

    for _ in range(3):
        await observe([failure])
    for _ in range(3):
        await observe([CollectionAttempt("source")])
    for _ in range(3):
        await observe([failure])
    lines = market_health._incident_pending["source/current"][1].splitlines()
    assert [line.split(" :")[0] for line in lines] == [
        "Nouvel incident", "Rétablissement", "Nouvel incident",
    ]
    send.return_value = True
    await observe([failure])
    assert send.call_args.args[0].splitlines() == lines
    assert market_health._incident_pending == {}


@pytest.mark.asyncio
async def test_the_persisted_state_is_the_whole_state(monkeypatch, market_clock):
    """Ce qui est écrit dans Redis suffit à reprendre : ni rejeu, ni perte."""
    stored = {}
    _redis_client(monkeypatch, stored)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(market_health, "notify_admins", send)
    failure = CollectionAttempt("source", status="failed", reason="rate_limited")

    for _ in range(3):
        market_clock.advance(30)
        await market_health.notify_refresh_result(result(attempts=[failure]))
    persisted = json.loads(stored[market_health._INCIDENT_KEY])
    assert persisted["version"] == 3
    assert persisted["announced"] == ["source/current"]
    assert persisted["pending"] == {}
    run = persisted["sources"]["source/current"]
    assert (run["outcome"], run["samples"], run["causes"]) == ("failed", 3, ["rate_limited"])
    assert run["last_at"] - run["first_at"] == 3600

    client, send = _restart(monkeypatch, stored)
    for _ in range(3):
        market_clock.advance(30)
        await market_health.notify_refresh_result(result(attempts=[CollectionAttempt("source")]))
    client.get.assert_awaited_once_with(market_health._INCIDENT_KEY)
    assert send.await_count == 1
    assert "Rétablissement : source/current" in send.call_args.args[0]
    persisted = json.loads(stored[market_health._INCIDENT_KEY])
    assert persisted["announced"] == []
    answered = persisted["sources"]["source/current"]
    assert (answered["outcome"], answered["samples"]) == ("answered", 3)

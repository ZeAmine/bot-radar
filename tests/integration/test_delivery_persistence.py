"""Frontières d'envoi et de persistance avec connexions indépendantes en CI PostgreSQL."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import tasks
from app.core.config import settings
from app.core.logging import JSONFormatter
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.schemas.listing import ListingIngestPayload
from app.services.telegram_service import send_listing_alert
from app.services.vinted_scraper import ScrapedVintedItem
from app.tasks import vinted_poller as poller
from app.use_cases import ingest_listing as ingestion
from app.use_cases.listing_ingestion_gate import ListingIngestionGate
from tests.database import postgres_replayed_database
from tests.telegram_fakes import telegram_message


@pytest.fixture
async def journey(monkeypatch):
    async with postgres_replayed_database() as db:
        db.maker.configure(autoflush=False)
        category = Category(slug="tech", label="Tech", emoji="T", is_active=True)
        db.session.add(category)
        await db.session.commit()
        bot = AsyncMock()
        deliveries = []

        async def send(chat_id, **_kwargs):
            deliveries.append(chat_id)
            return telegram_message(chat_id, len(deliveries))

        bot.send_message.side_effect = send
        monkeypatch.setattr(settings, "AI_ENABLED", False)
        monkeypatch.setattr(tasks, "_background_tasks", set())
        monkeypatch.setattr(tasks, "_cleanup_tasks", set())
        monkeypatch.setattr(ingestion, "_get_send_listing_alert", lambda: send_listing_alert)
        monkeypatch.setattr(ingestion, "listing_ingestion_gate", ListingIngestionGate())
        monkeypatch.setattr(poller, "AsyncSessionLocal", db.maker)
        alert_ids = []

        async def add(chat_id):
            user = User(telegram_user_id=chat_id, is_allowed=True)
            db.session.add(user)
            await db.session.flush()
            alert = Alert(
                user_id=user.id,
                category_id=category.id,
                name="Console",
                include_keywords=["console"],
                status="ACTIVE",
            )
            db.session.add(alert)
            await db.session.commit()
            alert_ids.append(alert.id)

        await add(601)
        value = SimpleNamespace(
            db=db,
            bot=bot,
            deliveries=deliveries,
            add=add,
            alert_ids=alert_ids,
            payload=ListingIngestPayload(
                source_name="vinted",
                external_id="sql-receipt",
                title="Console",
                url="https://example.invalid/item",
                category_slug="tech",
                item_price_cents=100,
            ),
        )
        assert db.session.bind is not None
        async with db.session.bind.connect() as reader_connection:
            postgres = reader_connection.dialect.name == "postgresql"
            reader_pid = (
                await reader_connection.scalar(text("SELECT pg_backend_pid()"))
                if postgres
                else None
            )

            async def states():
                async with AsyncSession(bind=reader_connection) as reader:
                    return {
                        alert_id: moment is not None
                        for alert_id, moment in (
                            await reader.execute(
                                select(ListingMatch.alert_id, ListingMatch.notified_at)
                            )
                        ).all()
                    }

            @asynccontextmanager
            async def writer():
                async with db.maker() as session:
                    if postgres:
                        assert await session.scalar(text("SELECT pg_backend_pid()")) != reader_pid
                    yield session

            value.states, value.writer = states, writer
            yield value
            await tasks.wait_all_background_tasks(timeout=0)


async def ingest(journey):
    async with journey.writer() as session:
        return await ingestion.ProcessListingUseCase(session, journey.bot).execute(
            journey.payload, {}
        )


async def test_none_receipt_never_marks_notified_and_stays_f01_eligible(journey):
    journey.bot.send_message.side_effect = None
    journey.bot.send_message.return_value = None
    response = await ingest(journey)
    assert response.needs_retry
    assert await journey.states() == {journey.alert_ids[0]: False}
    assert journey.payload.external_id not in await poller.load_initial_seen_ids()


@pytest.mark.parametrize("applied", [False, True])
async def test_confirmed_receipt_recovers_sql_without_resending(journey, applied):
    async with journey.writer() as session:
        original = session.commit
        calls = 0

        async def commit():
            nonlocal calls
            calls += 1
            if calls == 2:
                if applied:
                    await original()
                raise SQLAlchemyError("synthetic notification commit error")
            await original()

        session.commit = commit
        response = await ingestion.ProcessListingUseCase(session, journey.bot).execute(
            journey.payload, {}
        )
    assert response.matches[0].notification_sent
    assert journey.deliveries == [601]
    assert await journey.states() == {journey.alert_ids[0]: True}
    assert journey.payload.external_id in await poller.load_initial_seen_ids()
    assert calls == (3 if applied else 4)  # commit final toujours joué


async def test_second_sql_failure_stays_pending_and_f01_retries(journey):
    async with journey.writer() as session:
        original = session.commit
        calls = 0

        async def commit():
            nonlocal calls
            calls += 1
            if calls in (2, 3):
                raise SQLAlchemyError("synthetic SQL unavailable")
            await original()

        session.commit = commit
        with pytest.raises(SQLAlchemyError):
            await ingestion.ProcessListingUseCase(session, journey.bot).execute(journey.payload, {})
    assert calls == 3
    assert journey.deliveries == [601]
    assert await journey.states() == {journey.alert_ids[0]: False}
    assert journey.payload.external_id not in await poller.load_initial_seen_ids()
    await ingest(journey)
    assert journey.deliveries == [601, 601]


async def test_recovery_keeps_other_recipients_independent(journey):
    await journey.add(602)
    async with journey.writer() as session:
        original = session.commit
        calls = 0

        async def commit():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SQLAlchemyError("synthetic first recipient commit failed")
            await original()

        session.commit = commit
        response = await ingestion.ProcessListingUseCase(session, journey.bot).execute(
            journey.payload, {}
        )
    assert all(item.notification_sent for item in response.matches)
    assert sorted(journey.deliveries) == [601, 602]
    assert all((await journey.states()).values())


async def test_failure_of_second_recipient_preserves_first_durable_marker(journey):
    await journey.add(602)
    async with journey.writer() as session:
        original = session.commit
        calls = 0

        async def commit():
            nonlocal calls
            calls += 1
            if calls in (3, 4):
                raise SQLAlchemyError("synthetic second recipient commit error")
            await original()

        session.commit = commit
        with pytest.raises(SQLAlchemyError):
            await ingestion.ProcessListingUseCase(session, journey.bot).execute(journey.payload, {})
    assert len(journey.deliveries) == 2
    first, second = journey.deliveries
    mapping = dict(zip((601, 602), journey.alert_ids, strict=True))
    assert await journey.states() == {mapping[first]: True, mapping[second]: False}
    await ingest(journey)
    assert journey.deliveries == [first, second, second]


async def test_final_commit_error_does_not_reopen_durable_notification(journey, caplog):
    async with journey.writer() as session:
        original = session.commit
        calls = 0

        async def commit():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise SQLAlchemyError("synthetic final commit error")
            await original()

        session.commit = commit
        with caplog.at_level(logging.INFO), pytest.raises(SQLAlchemyError):
            await ingestion.ProcessListingUseCase(session, journey.bot).execute(journey.payload, {})
    assert await journey.states() == {journey.alert_ids[0]: True}
    records = [
        r for r in caplog.records if getattr(r, "persistence_stage", "") == "final_commit_failed"
    ]
    assert records
    assert all(r.persistence_result == "notified" for r in records)
    await ingest(journey)
    assert journey.deliveries == [601]


async def test_unknown_send_is_recollected_by_real_f01_path(journey, monkeypatch):
    async def lost(chat_id, **_kwargs):
        journey.deliveries.append(chat_id)
        raise TelegramNetworkError(
            method=SendMessage(chat_id=chat_id, text="synthetic"), message="synthetic response lost"
        )

    journey.bot.send_message.side_effect = lost
    assert (await ingest(journey)).needs_retry
    assert await journey.states() == {journey.alert_ids[0]: False}
    seen = await poller.load_initial_seen_ids()
    assert journey.payload.external_id not in seen
    item = ScrapedVintedItem(
        external_id=journey.payload.external_id,
        title="Console",
        url="https://example.invalid/item",
        item_price_cents=100,
        condition="Très bon état",
        photo_url=None,
    )
    monkeypatch.setattr(poller, "fetch_vinted_page", AsyncMock(return_value=[item]))
    monkeypatch.setattr(poller, "_ingest_semaphore", None)
    monkeypatch.setattr(ingestion, "default_bot", journey.bot)

    async def success(chat_id, **_kwargs):
        journey.deliveries.append(chat_id)
        return telegram_message(chat_id)

    journey.bot.send_message.side_effect = success
    assert await poller.poll_vinted_once(seen, AsyncMock(), category_slug="tech") == 1
    assert journey.deliveries == [601, 601]
    assert await journey.states() == {journey.alert_ids[0]: True}


async def test_sql_diagnostics_keep_correlation_and_receipt_without_payload(journey, caplog):
    async with journey.writer() as session:
        original = session.commit
        calls = 0

        async def commit():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SQLAlchemyError("RAW_BODY_SYNTHETIC https://example.invalid/sensitive_path")
            await original()

        session.commit = commit
        with caplog.at_level(logging.INFO):
            await ingestion.ProcessListingUseCase(session, journey.bot).execute(journey.payload, {})
    records = [
        json.loads(JSONFormatter().format(r))
        for r in caplog.records
        if getattr(r, "event", "") == "notification_delivery"
    ]
    assert len({r["attempt_id"] for r in records}) == 1
    assert len({r["alert_id"] for r in records}) == 1
    assert len({r["listing_id"] for r in records}) == 1
    assert {r["message_id"] for r in records} == {1}
    assert "committed" in {r["persistence_stage"] for r in records}
    assert "retry_marking" in {r["persistence_stage"] for r in records}
    output = json.dumps(records)
    assert "RAW_BODY_SYNTHETIC" not in output
    assert "sensitive_path" not in output


@pytest.mark.parametrize("boundary", ["send", "mark", "rollback", "read", "retry_commit", "final"])
@pytest.mark.parametrize("drain", [False, True])
async def test_cancellation_preserves_gate_cleanup_and_f01_state(
    journey, monkeypatch, boundary, drain
):
    entered, release_cleanup, cleanup_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    never = asyncio.Event()
    parent = None
    drain_observed = asyncio.Event()
    original_wait = asyncio.wait
    drainer = None

    async def observe_wait(pending, **kwargs):
        if asyncio.current_task() is drainer and tasks._cleanup_tasks.intersection(pending):
            drain_observed.set()
        return await original_wait(pending, **kwargs)

    monkeypatch.setattr(tasks.asyncio, "wait", observe_wait)
    async with journey.writer() as session:
        original_commit, original_mark, original_rollback = (session.commit, None, session.rollback)
        use = ingestion.ProcessListingUseCase(session, journey.bot)
        original_mark = use.listing_repo.mark_notified
        original_read = use.listing_repo.match_notification_state
        calls = 0

        async def send(chat_id, **_kwargs):
            journey.deliveries.append(chat_id)
            if boundary == "send":
                entered.set()
                await never.wait()
            return telegram_message(chat_id)

        async def commit():
            nonlocal calls
            calls += 1
            if calls == 2 and boundary in {"rollback", "read", "retry_commit"}:
                raise SQLAlchemyError("synthetic initial SQL error")
            if calls == 3 and boundary in {"retry_commit", "final"}:
                entered.set()
                await never.wait()
            await original_commit()

        async def mark(pairs):
            marked = await original_mark(pairs)
            if boundary == "mark":
                entered.set()
                await never.wait()
            return marked

        async def read(listing_id):
            if boundary == "read":
                entered.set()
                await never.wait()
            return await original_read(listing_id)

        async def rollback():
            assert parent is not None
            if boundary == "rollback" or parent.cancelling():
                cleanup_started.set()
                if boundary == "rollback":
                    entered.set()
                await release_cleanup.wait()
            await original_rollback()

        journey.bot.send_message.side_effect = send
        session.commit, session.rollback = commit, rollback
        use.listing_repo.mark_notified, use.listing_repo.match_notification_state = mark, read
        parent = tasks.safe_create_task(use.execute(journey.payload, {}), name="delivery_ingestion")
        drainer = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            if drain:
                drainer = asyncio.create_task(tasks.wait_all_background_tasks(timeout=0))
            else:
                parent.cancel()
            await asyncio.wait_for(cleanup_started.wait(), 3)
            if drainer is not None:
                await asyncio.wait_for(drain_observed.wait(), 3)
                assert not drainer.done()
            gate = ingestion.listing_ingestion_gate
            entry = gate._entries[("vinted", journey.payload.external_id)]
            assert entry.lock.locked()
            assert tasks._cleanup_tasks
            assert all(not task.cancelling() for task in tasks._cleanup_tasks)
            release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(parent, 3)
            if drainer is not None:
                await asyncio.wait_for(drainer, 3)
        finally:
            never.set()
            release_cleanup.set()
            if not parent.done():
                parent.cancel()
            await asyncio.gather(parent, return_exceptions=True)
            if drainer is not None:
                await asyncio.gather(drainer, return_exceptions=True)
    assert await journey.states() == {journey.alert_ids[0]: boundary == "final"}
    assert journey.deliveries == [601]
    assert not ingestion.listing_ingestion_gate._entries
    assert not tasks._background_tasks
    assert not tasks._cleanup_tasks


async def test_disappeared_match_does_not_claim_durable_marking(journey, monkeypatch, caplog):
    from sqlalchemy import delete

    original = ingestion.ListingRepository.mark_notified

    async def disappeared(repo, pairs):
        await repo.session.execute(delete(ListingMatch))
        return await original(repo, pairs)

    monkeypatch.setattr(ingestion.ListingRepository, "mark_notified", disappeared)
    with caplog.at_level(logging.INFO):
        await ingest(journey)
    assert await journey.states() == {}
    records = [r for r in caplog.records if getattr(r, "event", "") == "notification_delivery"]
    assert not any(getattr(r, "persistence_result", None) == "notified" for r in records)
    assert any(getattr(r, "persistence_stage", None) == "match_missing" for r in records)
    assert journey.deliveries == [601]

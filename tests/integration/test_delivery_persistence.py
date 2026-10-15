"""Frontières d'envoi et de persistance avec connexions indépendantes en CI PostgreSQL."""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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
        bot.id = 123456789
        deliveries = []

        async def send(chat_id, **_kwargs):
            deliveries.append(chat_id)
            return telegram_message(chat_id, len(deliveries))

        bot.send_message.side_effect = send
        monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [601, 602])
        monkeypatch.setattr(settings, "AI_ENABLED", False)
        monkeypatch.setattr(tasks, "_background_tasks", set())
        monkeypatch.setattr(tasks, "_cleanup_tasks", set())
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

            async def moments():
                async with AsyncSession(bind=reader_connection) as reader:
                    return dict(
                        (
                            await reader.execute(
                                select(ListingMatch.alert_id, ListingMatch.notified_at)
                            )
                        ).all()
                    )

            value.states, value.writer, value.moments = states, writer, moments
            yield value
            await tasks.wait_all_background_tasks(timeout=0)


async def ingest(journey):
    async with journey.writer() as session:
        return await ingestion.ProcessListingUseCase(session, journey.bot).execute(
            journey.payload, {}
        )


async def test_none_receipt_never_marks_notified_and_is_not_recollected(journey):
    journey.bot.send_message.side_effect = None
    journey.bot.send_message.return_value = None
    response = await ingest(journey)
    assert not response.needs_retry
    assert await journey.states() == {journey.alert_ids[0]: False}
    assert journey.payload.external_id in await poller.load_initial_seen_ids()


@pytest.mark.parametrize("applied", [False, True])
async def test_confirmed_receipt_recovers_sql_without_resending(journey, applied):
    async with journey.writer() as session:
        original = session.commit
        calls = 0
        applied_moments = None

        async def commit():
            nonlocal calls, applied_moments
            calls += 1
            if calls == 3:
                if applied:
                    await original()
                    applied_moments = await journey.moments()
                raise SQLAlchemyError("synthetic notification commit error")
            await original()

        session.commit = commit
        use = ingestion.ProcessListingUseCase(session, journey.bot)
        use.listing_repo.mark_notified = AsyncMock(wraps=use.listing_repo.mark_notified)
        response = await use.execute(journey.payload, {})
        assert use.listing_repo.mark_notified.await_count == (1 if applied else 2)
    assert response.matches[0].notification_sent
    assert journey.deliveries == [601]
    assert await journey.states() == {journey.alert_ids[0]: True}
    assert journey.payload.external_id in await poller.load_initial_seen_ids()
    if applied:
        assert applied_moments is not None
        assert await journey.moments() == applied_moments
    assert calls == (4 if applied else 5)  # commit final toujours joué


async def test_second_sql_failure_preserves_unknown_without_resending(journey):
    async with journey.writer() as session:
        original = session.commit
        calls = 0

        async def commit():
            nonlocal calls
            calls += 1
            if calls in (3, 4):
                raise SQLAlchemyError("synthetic SQL unavailable")
            await original()

        session.commit = commit
        with pytest.raises(SQLAlchemyError):
            await ingestion.ProcessListingUseCase(session, journey.bot).execute(journey.payload, {})
    assert calls == 4
    assert journey.deliveries == [601]
    assert await journey.states() == {journey.alert_ids[0]: False}
    assert journey.payload.external_id in await poller.load_initial_seen_ids()
    await ingest(journey)
    assert journey.deliveries == [601]


async def test_recovery_keeps_other_recipients_independent(journey):
    await journey.add(602)
    async with journey.writer() as session:
        original = session.commit
        calls = 0

        async def commit():
            nonlocal calls
            calls += 1
            if calls == 3:
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
            if calls in (5, 6):
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
    assert journey.deliveries == [first, second]


async def test_final_commit_error_does_not_reopen_durable_notification(journey, caplog):
    async with journey.writer() as session:
        original = session.commit
        calls = 0

        async def commit():
            nonlocal calls
            calls += 1
            if calls == 4:
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


async def test_unknown_send_is_not_recollected_by_real_poller_path(journey, monkeypatch):
    async def lost(chat_id, **_kwargs):
        journey.deliveries.append(chat_id)
        raise TelegramNetworkError(
            method=SendMessage(chat_id=chat_id, text="synthetic"), message="synthetic response lost"
        )

    journey.bot.send_message.side_effect = lost
    assert not (await ingest(journey)).needs_retry
    assert await journey.states() == {journey.alert_ids[0]: False}
    seen = await poller.load_initial_seen_ids()
    assert journey.payload.external_id in seen
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
    assert await poller.poll_vinted_once(seen, AsyncMock(), category_slug="tech") == 0
    assert journey.deliveries == [601]
    assert await journey.states() == {journey.alert_ids[0]: False}


async def test_sql_diagnostics_keep_correlation_and_receipt_without_payload(journey, caplog):
    async with journey.writer() as session:
        original = session.commit
        calls = 0

        async def commit():
            nonlocal calls
            calls += 1
            if calls == 3:
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
            if calls == 3 and boundary in {"rollback", "read", "retry_commit"}:
                raise SQLAlchemyError("synthetic initial SQL error")
            if calls == 4 and boundary in {"retry_commit", "final"}:
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


async def test_repeated_marking_keeps_durable_timestamp_strictly_unchanged(journey):
    await ingest(journey)
    before = await journey.moments()
    assert before[journey.alert_ids[0]] is not None
    async with journey.writer() as session:
        listing_id = await session.scalar(select(ListingMatch.listing_id))
        repo = ingestion.ListingRepository(session)
        for _ in range(2):
            assert await repo.mark_notified([(journey.alert_ids[0], listing_id)])
            await session.commit()
            assert await journey.moments() == before
    assert journey.deliveries == [601]


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="Entrelacement PostgreSQL requis"
)
@pytest.mark.parametrize("change", ["notified", "deleted"])
async def test_match_changes_between_pending_read_and_retry_update(
    journey, caplog, change, record_property
):
    from sqlalchemy import delete, update

    pending_read, release = asyncio.Event(), asyncio.Event()
    durable_at = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    async with journey.writer() as session:
        use = ingestion.ProcessListingUseCase(session, journey.bot)
        original_commit, original_read = session.commit, use.listing_repo.match_notification_state
        calls, active_pid = 0, None
        commit_stages = []

        async def commit():
            nonlocal calls
            calls += 1
            commit_stages.append(sys._getframe(1).f_code.co_name)
            if calls == 3:
                raise SQLAlchemyError("synthetic commit failure before application")
            await original_commit()

        async def read(listing_id):
            nonlocal active_pid
            result = await original_read(listing_id)
            if calls == 3:
                assert journey.alert_ids[0] in result[1]
                active_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                pending_read.set()
                await release.wait()
            return result

        session.commit, use.listing_repo.match_notification_state = commit, read
        use.listing_repo.mark_notified = AsyncMock(wraps=use.listing_repo.mark_notified)
        with caplog.at_level(logging.INFO):
            running = asyncio.create_task(use.execute(journey.payload, {}))
            try:
                await asyncio.wait_for(pending_read.wait(), 3)
                async with journey.writer() as other:
                    other_pid = await other.scalar(text("SELECT pg_backend_pid()"))
                    assert other_pid != active_pid
                    record_property("postgres_recipe", {"participants": [active_pid, other_pid]})
                    stmt = (
                        update(ListingMatch).values(notified_at=durable_at)
                        if change == "notified"
                        else delete(ListingMatch)
                    ).where(ListingMatch.alert_id == journey.alert_ids[0])
                    await other.execute(stmt)
                    await other.commit()
                release.set()
                response = await asyncio.wait_for(running, 3)
            finally:
                release.set()
                await asyncio.gather(running, return_exceptions=True)
        assert response.matches[0].notification_sent
        assert use.listing_repo.mark_notified.await_count == 2
        # Point 6 : la réservation durable précède le marquage (échoué puis repris).
        assert calls == 5
        assert commit_stages == [
            "_execute_serialized", "reserve", "_persist_confirmed_delivery",
            "_persist_confirmed_delivery", "_execute_serialized",
        ]
    assert journey.deliveries == [601]
    records = [r for r in caplog.records if getattr(r, "event", "") == "notification_delivery"]
    if change == "notified":
        assert await journey.moments() == {journey.alert_ids[0]: durable_at}
        assert any(getattr(r, "persistence_result", None) == "notified" for r in records)
    else:
        assert await journey.moments() == {}
        assert not any(getattr(r, "persistence_result", None) == "notified" for r in records)
        assert any(getattr(r, "persistence_stage", None) == "match_missing" for r in records)

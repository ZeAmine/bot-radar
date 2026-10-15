"""Contrat de reprise avec base synthétique et transports exclusivement simulés."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.schemas.listing import ListingIngestPayload
from app.services.telegram_service import send_listing_alert
from app.tasks import recover_notifications as recovery
from app.use_cases import ingest_listing as ingestion
from app.use_cases.listing_ingestion_gate import ListingIngestionGate
from tests.database import postgres_replayed_database
from tests.telegram_fakes import telegram_message


@pytest.fixture
async def journey(monkeypatch):
    async with postgres_replayed_database() as db:
        db.maker.configure(autoflush=False)
        user = User(telegram_user_id=601, is_allowed=True)
        category = Category(slug="tech", label="Tech", emoji="T", is_active=True)
        db.session.add_all([user, category])
        await db.session.flush()
        alert = Alert(user_id=user.id, category_id=category.id, name="Console",
                      include_keywords=["console"], status="ACTIVE")
        db.session.add(alert)
        await db.session.commit()
        bot = AsyncMock()
        bot.id = 123456789
        bot.send_message.return_value = telegram_message(601)
        gate = ListingIngestionGate()
        monkeypatch.setattr(settings, "AI_ENABLED", False)
        monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [601])
        monkeypatch.setattr(ingestion, "listing_ingestion_gate", gate)
        monkeypatch.setattr(recovery, "listing_ingestion_gate", gate)
        monkeypatch.setattr(recovery, "AsyncSessionLocal", db.maker)
        monkeypatch.setattr(recovery, "get_bot", lambda: bot)
        payload = ListingIngestPayload(source_name="vinted", external_id="one", title="Console",
                                       url="https://example.invalid/item", category_slug="tech",
                                       item_price_cents=100)

        context = SimpleNamespace(
            db=db, bot=bot, gate=gate, payload=payload, sender=send_listing_alert,
        )

        async def ingest():
            async with db.maker() as session:
                return await ingestion.ProcessListingUseCase(
                    session, bot, sender=context.sender
                ).execute(payload, {})

        async def row():
            async with db.maker() as reader:
                return (await reader.scalars(select(ListingMatch))).one()

        context.ingest, context.row = ingest, row
        yield context


async def test_ready_resumes_without_recollection(journey):
    # Refus certain récupérable ; le second passage ne reçoit aucune annonce extérieure.
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    j = journey
    j.bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=601, text="synthetic"), message="synthetic", retry_after=1,
    )
    await j.ingest()
    row = await j.row()
    assert row.notification_state == "ready"
    assert row.attempt_count == 1
    assert row.notified_at is None
    async with j.db.maker() as session:
        current = await session.get(ListingMatch, row.id)
        current.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    j.bot.send_message.side_effect = None
    await recovery.recover_notifications()
    result = await j.row()
    assert result.notified_at is not None
    assert result.notification_state == "notified"
    assert result.telegram_message_id > 0
    assert result.attempt_count == 2
    assert j.bot.send_message.await_count == 2
    await recovery.recover_notifications()
    await j.ingest()
    assert j.bot.send_message.await_count == 2


async def test_unknown_is_never_retried(journey):
    j = journey
    j.bot.send_message.side_effect = TimeoutError("synthetic response lost")
    await j.ingest()
    assert (await j.row()).notification_state == "unknown"
    j.bot.send_message.side_effect = None
    await recovery.recover_notifications()
    await j.ingest()
    assert j.bot.send_message.await_count == 1
    assert (await j.row()).notified_at is None


async def test_live_sending_owner_is_not_orphaned(journey, monkeypatch):
    j = journey
    started, release = asyncio.Event(), asyncio.Event()

    async def send(**_kwargs):
        started.set()
        await release.wait()
        return telegram_message(601)

    j.bot.send_message.side_effect = send
    owner = asyncio.create_task(j.ingest())
    await asyncio.wait_for(started.wait(), 3)
    assert (await j.row()).notification_state == "sending"
    # Observer l’acquisition tentée, après les lectures SQL et l’inscription au gate.
    # Le vrai verrou reste détenu par l’envoi : le worker doit réellement l’attendre.
    lock = j.gate._entries[(j.payload.source_name, j.payload.external_id)].lock
    acquire = lock.acquire
    attempted = asyncio.Event()

    async def observed_acquire():
        attempted.set()
        return await acquire()

    monkeypatch.setattr(lock, "acquire", observed_acquire)
    worker = asyncio.create_task(recovery.recover_notifications())
    try:
        # Échéance diagnostique, sans attente minimale ni budget de tours CPU.
        await asyncio.wait_for(attempted.wait(), 3)
        assert j.gate._participants == 2
        assert lock.locked()
        assert not worker.done()
        assert (await j.row()).notification_state == "sending"
    finally:
        release.set()
        await asyncio.gather(owner, worker)
    assert (await j.row()).notification_state == "notified"
    assert j.bot.send_message.await_count == 1
    assert not j.gate._entries
    assert j.gate._participants == 0


@pytest.mark.parametrize(("state", "case", "expected"), [
    ("unknown", "legacy", "unknown"),
    ("sending", "orphan", "unknown"),
    ("ready", "expired", "abandoned"),
    ("ready", "budget", "abandoned"),
    ("ready", "invalid_count", "unknown"),
    ("ready", "unauthorized", "deferred"),
    ("ready", "destination", "abandoned"),
    ("ready", "matching", "abandoned"),
    ("ready", "paused", "deferred"),
])
async def test_persisted_admission_refuses_unsafe_cases(journey, state, case, expected):
    from app.db.models.listing import Listing
    from app.db.repositories.listing_repo import ListingRepository

    j = journey
    async with j.db.maker() as session:
        user = (await session.scalars(select(User))).one()
        alert = (await session.scalars(select(Alert))).one()
        listing = await ListingRepository(session).create_from_payload(
            ListingIngestPayload(source_name="vinted", external_id="one", title="Console",
                                 url="https://example.invalid/item", category_slug="tech",
                                 item_price_cents=100), {},
        )
        row = await ListingRepository(session).create_match(alert.id, listing.id)
        row.notification_state = state
        if case == "legacy":
            row.attempt_count = None
        if case == "expired":
            listing.received_at = datetime.now(UTC) - timedelta(hours=1)
        if case == "budget":
            row.attempt_count = 3
        if case == "invalid_count":
            row.attempt_count = -1
        if case == "unauthorized":
            user.is_allowed = False
        if case == "destination":
            row.telegram_chat_id = 602
        if case == "matching":
            alert.include_keywords = ["different"]
        if case == "paused":
            alert.status = "PAUSED"
        initial_time = listing.received_at
        await session.commit()
    await recovery.recover_notifications()
    assert (await j.row()).notification_state == expected
    assert (await j.row()).notified_at is None
    j.bot.send_message.assert_not_awaited()
    async with j.db.maker() as reader:
        listing = (await reader.scalars(select(Listing))).one()
        assert listing.received_at.replace(tzinfo=UTC) == initial_time.replace(tzinfo=UTC)


@pytest.mark.parametrize("boundary", ["reserve", "mark", "cancel", "ambiguous_reserve"])
async def test_commit_boundaries_do_not_allow_resending(journey, monkeypatch, boundary):
    import sys

    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    j = journey
    original = AsyncSession.commit
    injected = 0

    async def commit(session):
        nonlocal injected
        caller = sys._getframe(1).f_code.co_name
        if ((caller == "reserve" and boundary != "mark")
                or (caller == "_persist_confirmed_delivery" and boundary == "mark")):
            injected += 1
            if boundary == "ambiguous_reserve":
                await original(session)
            if boundary == "cancel":
                raise asyncio.CancelledError()
            raise SQLAlchemyError("synthetic commit failure")
        await original(session)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    with pytest.raises(asyncio.CancelledError if boundary == "cancel" else SQLAlchemyError):
        await j.ingest()
    assert injected >= 1
    first = await j.row()
    assert first.notified_at is None
    if boundary == "mark":
        assert j.bot.send_message.await_count == 1
    else:
        j.bot.send_message.assert_not_awaited()
    monkeypatch.setattr(AsyncSession, "commit", original)
    await recovery.recover_notifications()
    result = await j.row()
    if boundary in {"ambiguous_reserve", "mark"}:
        assert result.notification_state == "unknown"
        assert j.bot.send_message.await_count == (1 if boundary == "mark" else 0)
    else:
        assert result.notification_state == "notified"
        assert j.bot.send_message.await_count == 1
    assert not j.gate._entries


async def test_job_limits_work_and_admission_time(journey, monkeypatch):
    from app.db.repositories.listing_repo import ListingRepository

    j = journey
    async with j.db.maker() as session:
        alert = (await session.scalars(select(Alert))).one()
        for index in range(15):
            payload = ListingIngestPayload(source_name="vinted", external_id=str(index),
                title="Console", url="https://example.invalid/item", category_slug="tech",
                item_price_cents=100)
            listing = await ListingRepository(session).create_from_payload(payload, {})
            await ListingRepository(session).create_match(alert.id, listing.id)
        await session.commit()
    await recovery.recover_notifications()
    assert j.bot.send_message.await_count == 10
    async with j.db.maker() as reader:
        rows = list((await reader.scalars(select(ListingMatch))).all())
        assert sum(row.notified_at is not None for row in rows) == 10
        assert sum(row.attempt_count == 0 for row in rows) == 5
    monkeypatch.setattr(recovery, "time", SimpleNamespace(monotonic=iter([0, 21]).__next__))
    await recovery.recover_notifications()
    assert j.bot.send_message.await_count == 10


async def test_additive_migration_preserves_old_evidence(journey):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from app.db.repositories.listing_repo import ListingRepository

    j = journey
    async with j.db.maker() as session:
        alert = (await session.scalars(select(Alert))).one()
        listing = await ListingRepository(session).create_from_payload(
            ListingIngestPayload(source_name="vinted", external_id="old", title="Console",
                url="https://example.invalid/item", category_slug="tech", item_price_cents=100), {})
        await ListingRepository(session).create_match(alert.id, listing.id)
        second = Alert(user_id=alert.user_id, category_id=alert.category_id, name="Second",
                       include_keywords=["console"], status="ACTIVE")
        session.add(second)
        await session.flush()
        await ListingRepository(session).create_match(second.id, listing.id, notified=True)
        await session.commit()
        before = (await session.execute(text(
            "SELECT id, alert_id, listing_id, notified_at, created_at "
            "FROM listing_matches ORDER BY id"
        ))).all()
        await session.execute(text("DROP INDEX ix_listing_matches_next_attempt_at"))
        await session.execute(text("DROP INDEX ix_listing_matches_recovery"))
        names = ("notification_state", "attempt_count", "attempt_id", "attempt_started_at",
                 "next_attempt_at", "telegram_chat_id", "telegram_bot_id", "telegram_message_id",
                 "notification_reason")
        for name in names:
            await session.execute(text("ALTER TABLE listing_matches DROP COLUMN " + name))
        await session.commit()
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "notification_migration", Path("alembic/versions/0011_notification_state.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        connection = await session.connection()

        def upgrade(sync_connection):
            with Operations.context(MigrationContext.configure(sync_connection)):
                module.upgrade()

        await connection.run_sync(upgrade)
        await session.commit()
    async with j.db.maker() as reader:
        after = (await reader.execute(text(
            "SELECT id, alert_id, listing_id, notified_at, created_at "
            "FROM listing_matches ORDER BY id"
        ))).all()
        assert before == after
        rows = list((await reader.scalars(select(ListingMatch))).all())
        assert {row.notification_state for row in rows} == {"unknown", "notified"}
        assert all(row.attempt_count is None for row in rows)
        assert all(row.telegram_message_id is None for row in rows)
        assert all(row.telegram_chat_id is None for row in rows)
        assert all(row.attempt_id is None for row in rows)
    await recovery.recover_notifications()
    j.bot.send_message.assert_not_awaited()


async def test_cycles_count_retries_and_preserve_retry_after(journey, monkeypatch):
    from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
    from aiogram.methods import SendMessage


    j = journey
    method = SendMessage(chat_id=601, text="synthetic")
    j.bot.send_message.side_effect = TelegramRetryAfter(
        method=method, message="synthetic refusal", retry_after=120,
    )
    j.payload.photos = ["https://example.invalid/photo.jpg"]
    j.bot.send_photo.side_effect = TelegramBadRequest(
        method=method, message="Bad Request: wrong type of the web page content",
    )
    monkeypatch.setattr("app.services.telegram_service.asyncio.sleep", AsyncMock())
    await j.ingest()
    first = await j.row()
    assert first.next_attempt_at.replace(tzinfo=UTC) >= (
        first.attempt_started_at.replace(tzinfo=UTC) + timedelta(seconds=120)
    )
    for expected in (2, 3):
        async with j.db.maker() as session:
            row = await session.get(ListingMatch, first.id)
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        await recovery.recover_notifications()
        result = await j.row()
        assert result.attempt_count == expected
        assert result.notified_at is None
        assert result.notification_state == ("ready" if expected == 2 else "abandoned")
        if expected == 2:
            assert result.next_attempt_at.replace(tzinfo=UTC) >= (
                result.attempt_started_at.replace(tzinfo=UTC) + timedelta(minutes=5)
            )
    await recovery.recover_notifications()
    assert j.bot.send_message.await_count == 3
    assert j.bot.send_photo.await_count == 6
    for call in j.bot.send_message.call_args_list + j.bot.send_photo.call_args_list:
        assert call.kwargs["request_timeout"] == 10


async def test_lost_match_evidence_is_not_reconstructed_as_unsent(journey):
    from sqlalchemy import delete

    j = journey
    await j.ingest()
    assert (await j.row()).notified_at is not None
    async with j.db.maker() as session:
        await session.execute(delete(ListingMatch))
        await session.commit()
    await j.ingest()
    row = await j.row()
    assert row.notification_state == "unknown"
    assert row.attempt_count is None
    assert row.notified_at is None
    assert row.telegram_message_id is None
    assert j.bot.send_message.await_count == 1


async def test_automatic_purge_keeps_unknown_but_explicit_deletion_remains(journey):
    from app.db.repositories.alert_repo import AlertRepository

    j = journey
    j.bot.send_message.side_effect = TimeoutError("synthetic loss")
    await j.ingest()
    async with j.db.maker() as session:
        repo = AlertRepository(session)
        alert = (await session.scalars(select(Alert))).one()
        await repo.soft_delete(alert)
        await session.commit()
        assert (await repo.purge_orphans_and_deleted())["deleted_alerts"] == 0
        await session.commit()
    assert (await j.row()).notification_state == "unknown"
    async with j.db.maker() as session:
        alert = (await session.scalars(select(Alert))).one()
        await AlertRepository(session).delete_completely(alert)
        await session.commit()
    async with j.db.maker() as reader:
        assert list((await reader.scalars(select(ListingMatch))).all()) == []


async def test_recovery_diagnostics_do_not_expose_exception_body(monkeypatch, caplog):
    async def fail():
        raise RuntimeError("synthetic-private-body sk-fake-secret")
    monkeypatch.setattr(recovery, "_recover_notifications", fail)
    await recovery.recover_notifications()
    assert "synthetic-private-body" not in caplog.text
    assert "sk-fake-secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("kind", ["wrong_chat", "invalid_outcome", "invalid_not_sent"])
async def test_invalid_sender_evidence_stays_unknown(journey, monkeypatch, kind):
    from app.services.telegram_service import DeliveryResult

    values = {
        "wrong_chat": DeliveryResult("acknowledged", "text", receipt=telegram_message(999)),
        "invalid_outcome": DeliveryResult("invalid", "text"),
        "invalid_not_sent": DeliveryResult("not_sent", "text"),
    }
    fake = AsyncMock(return_value=values[kind])
    monkeypatch.setattr(journey, "sender", fake)
    await journey.ingest()
    row = await journey.row()
    assert row.notification_state == "unknown"
    assert row.notified_at is None
    assert row.telegram_message_id is None
    await recovery.recover_notifications()
    await journey.ingest()
    assert fake.await_count == 1


async def test_annulation_during_recovery_keeps_gate_until_rollback(journey):
    from app.db.repositories.listing_repo import ListingRepository

    j = journey
    async with j.db.maker() as session:
        alert = (await session.scalars(select(Alert))).one()
        value = ListingIngestPayload(source_name="vinted", external_id="one", title="Console",
            url="https://example.invalid/item", category_slug="tech", item_price_cents=100)
        listing = await ListingRepository(session).create_from_payload(value, {})
        await ListingRepository(session).create_match(alert.id, listing.id)
        await session.commit()
    entered, never = asyncio.Event(), asyncio.Event()

    async def send(**_kwargs):
        entered.set()
        await never.wait()

    j.bot.send_message.side_effect = send
    worker = asyncio.create_task(recovery.recover_notifications())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert (await j.row()).notification_state == "sending"
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert not j.gate._entries
    await recovery.recover_notifications()
    assert (await j.row()).notification_state == "unknown"
    assert j.bot.send_message.await_count == 1


async def test_stored_receipt_only_completes_marking_without_sending(journey):
    from sqlalchemy import update
    j = journey
    await j.ingest()
    first = await j.row()
    old_count, receipt = first.attempt_count, first.telegram_message_id
    # Incohérence synthétique : preuve d'accusé conservée, seul le marqueur a disparu.
    async with j.db.maker() as session:
        await session.execute(update(ListingMatch).values(
            notified_at=None, notification_state="unknown",
        ))
        await session.commit()
    await recovery.recover_notifications()
    final = await j.row()
    assert final.notified_at is not None
    assert final.notification_state == "notified"
    assert final.telegram_message_id == receipt
    assert final.attempt_count == old_count
    assert j.bot.send_message.await_count == 1


async def test_ingestion_instances_keep_their_own_senders(journey):
    """Deux instances réservent et persistent leurs envois sans partager l'émetteur."""
    from tests.telegram_fakes import acknowledged_delivery

    first_sender = AsyncMock(side_effect=acknowledged_delivery)
    second_sender = AsyncMock(side_effect=acknowledged_delivery)
    async with journey.db.maker() as first_session, journey.db.maker() as second_session:
        first = ingestion.ProcessListingUseCase(first_session, journey.bot, sender=first_sender)
        second = ingestion.ProcessListingUseCase(second_session, journey.bot, sender=second_sender)
        first_result = await first.execute(
            journey.payload.model_copy(update={"external_id": "first-sender"}), {}
        )
        second_result = await second.execute(
            journey.payload.model_copy(update={"external_id": "second-sender"}), {}
        )

    assert first_result.matches[0].notification_sent
    assert second_result.matches[0].notification_sent
    first_sender.assert_awaited_once()
    second_sender.assert_awaited_once()
    assert first_sender.call_args.kwargs["listing"].id == first_result.listing_id
    assert second_sender.call_args.kwargs["listing"].id == second_result.listing_id
    assert first_sender.call_args.kwargs["chat_id"] == 601
    assert second_sender.call_args.kwargs["chat_id"] == 601
    async with journey.db.maker() as reader:
        rows = list((await reader.scalars(select(ListingMatch))).all())
        assert len(rows) == 2
        assert all(row.notified_at is not None for row in rows)
        assert all(row.notification_state == "notified" for row in rows)
    journey.bot.send_message.assert_not_awaited()

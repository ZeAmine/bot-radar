"""Frontières de la chaîne réelle ; PostgreSQL rejoue les écritures avec lecteur indépendant."""

import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import JSONFormatter
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.schemas.listing import ListingIngestPayload
from app.use_cases import ingest_listing as ingestion
from tests.database import postgres_replayed_database
from tests.telegram_fakes import telegram_message


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def events(caplog, phase):
    return [
        r
        for r in caplog.records
        if getattr(r, "event", "") == "alert_latency" and getattr(r, "phase", "") == phase
    ]


@pytest.fixture
async def journey(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [601, 602])
    clock = Clock()
    from app.core import alert_latency

    monkeypatch.setattr(alert_latency, "monotonic", clock)
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    async with postgres_replayed_database() as db:
        category = Category(slug="tech", label="Tech", emoji="T", is_active=True)
        db.session.add(category)
        await db.session.commit()

        async def add(chat):
            user = User(telegram_user_id=chat, is_allowed=True)
            db.session.add(user)
            await db.session.flush()
            db.session.add(
                Alert(
                    user_id=user.id,
                    category_id=category.id,
                    name="Console",
                    include_keywords=["console"],
                    status="ACTIVE",
                )
            )
            await db.session.commit()

        await add(601)
        bot = AsyncMock()
        bot.id = 123456789

        async def send(chat_id, **_kwargs):
            clock.advance(0.2)
            return telegram_message(chat_id)

        bot.send_message.side_effect = send
        assert db.session.bind is not None
        async with db.session.bind.connect() as connection:
            reader_pid = (
                await connection.scalar(text("SELECT pg_backend_pid()"))
                if connection.dialect.name == "postgresql"
                else None
            )
            if reader_pid is not None:
                assert await db.session.scalar(text("SELECT pg_backend_pid()")) != reader_pid

            async def states():
                async with AsyncSession(bind=connection) as reader:
                    return list((await reader.execute(select(ListingMatch.notified_at))).scalars())

            yield SimpleNamespace(
                db=db,
                bot=bot,
                clock=clock,
                add=add,
                states=states,
                payload=ListingIngestPayload(
                    source_name="synthetic",
                    external_id="timed",
                    title="Console SENSITIVE_BODY",
                    url="https://example.invalid/SENSITIVE_URL",
                    category_slug="tech",
                    item_price_cents=100,
                ),
            )


async def test_recipient_transport_ack_persistence_and_final_are_distinct(journey, caplog):
    await journey.add(602)
    original = journey.db.session.commit
    calls = 0

    async def commit():
        nonlocal calls
        calls += 1
        journey.clock.advance(0.1 if calls == 1 else 0.4 if calls == 4 else 0.3)
        await original()

    journey.db.session.commit = commit
    with caplog.at_level(logging.INFO):
        response = await ingestion.ProcessListingUseCase(journey.db.session, journey.bot).execute(
            journey.payload, {}
        )
    assert all(m.notification_sent for m in response.matches)
    assert all(await journey.states())
    assert [r.duration_ms for r in events(caplog, "transport_attempt")] == pytest.approx([200, 200])
    # Six commits : ingestion, réservation 1, reçu 1, réservation 2, reçu 2, final.
    assert [r.duration_ms for r in events(caplog, "entry_to_ack")] == pytest.approx([600, 1500])
    assert [r.duration_ms for r in events(caplog, "ack_to_persistence")] == pytest.approx(
        [300, 300]
    )
    assert [r.duration_ms for r in events(caplog, "recipient_wait")] == pytest.approx([300, 1200])
    assert events(caplog, "pre_first_telegram_call")[0].duration_ms == pytest.approx(400)
    assert events(caplog, "final_commit")[0].duration_ms == pytest.approx(300)
    assert events(caplog, "ingestion_terminal")[0].duration_ms == pytest.approx(2100)
    records = [r for r in caplog.records if getattr(r, "event", "") == "alert_latency"]
    assert records
    assert all(r.duration_unit == "ms" for r in records)
    assert len({r.ingestion_id for r in records}) == 1
    output = "".join(JSONFormatter().format(r) for r in records)
    assert "SENSITIVE_BODY" not in output
    assert "SENSITIVE_URL" not in output
    for record in events(caplog, "transport_attempt"):
        assert record.alert_id
        assert record.listing_id
        assert record.attempt_id


@pytest.mark.parametrize("outcome", ["rejected", "unknown", "unusable"])
async def test_no_ack_duration_for_unsuccessful_results(journey, caplog, outcome):
    method = SendMessage(chat_id=601, text="synthetic")

    async def send(**_kwargs):
        journey.clock.advance(0.7)
        if outcome == "unusable":
            return None
        error = TelegramBadRequest if outcome == "rejected" else TelegramNetworkError
        raise error(method=method, message="SENSITIVE_BODY SENSITIVE_URL")

    journey.bot.send_message.side_effect = send
    with caplog.at_level(logging.INFO):
        response = await ingestion.ProcessListingUseCase(journey.db.session, journey.bot).execute(
            journey.payload, {}
        )
    assert response.needs_retry is False
    assert await journey.states() == [None]
    from app.db.models.listing_match import ListingMatch
    async with journey.db.maker() as reader:
        row = (await reader.scalars(select(ListingMatch))).one()
        assert row.notification_state == ("abandoned" if outcome == "rejected" else "unknown")
    assert not events(caplog, "entry_to_ack")
    records = events(caplog, "delivery_terminal")
    assert len(records) == 1
    assert records[0].result == ("unknown" if outcome == "unusable" else outcome)
    assert records[0].duration_ms == pytest.approx(700)
    assert records[0].entry_to_ack_ms is None


async def test_cancellation_keeps_terminal_duration_and_propagates(journey, caplog):
    entered = asyncio.Event()
    never = asyncio.Event()

    async def send(**_kwargs):
        entered.set()
        await never.wait()

    journey.bot.send_message.side_effect = send
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(
            ingestion.ProcessListingUseCase(journey.db.session, journey.bot).execute(
                journey.payload, {}
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        journey.clock.advance(0.9)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await journey.states() == [None]
    assert not events(caplog, "entry_to_ack")
    assert events(caplog, "transport_attempt")[0].result == "cancelled"
    assert events(caplog, "delivery_terminal")[0].duration_ms == pytest.approx(900)
    assert events(caplog, "ingestion_terminal")[0].result == "cancelled"


async def test_sql_recovery_included_without_second_transport(journey, caplog):
    original = journey.db.session.commit
    calls = 0

    async def commit():
        nonlocal calls
        calls += 1
        if calls == 3:
            assert sys._getframe(1).f_code.co_name == "_persist_confirmed_delivery"
            journey.clock.advance(0.5)
            raise SQLAlchemyError("SENSITIVE_BODY")
        journey.clock.advance(0.1)
        await original()

    journey.db.session.commit = commit
    with caplog.at_level(logging.INFO):
        response = await ingestion.ProcessListingUseCase(journey.db.session, journey.bot).execute(
            journey.payload, {}
        )
    assert response.matches[0].notification_sent
    journey.bot.send_message.assert_awaited_once()
    assert all(await journey.states())
    assert events(caplog, "entry_to_ack")[0].duration_ms == pytest.approx(400)
    assert events(caplog, "ack_to_persistence")[0].duration_ms == pytest.approx(600)
    assert events(caplog, "ack_to_persistence")[0].result == "notified"
    assert events(caplog, "final_commit")[0].duration_ms == pytest.approx(100)


async def test_duplicate_without_send_and_no_match_are_separate(journey, caplog):
    use = ingestion.ProcessListingUseCase(journey.db.session, journey.bot)
    with caplog.at_level(logging.INFO):
        await use.execute(journey.payload, {})
        await use.execute(journey.payload, {})
        await use.execute(
            journey.payload.model_copy(update={"external_id": "unmatched", "title": "Other"}), {}
        )
    assert [r.result for r in events(caplog, "ingestion_terminal")] == [
        "attempted",
        "duplicate_no_send",
        "no_match",
    ]
    journey.bot.send_message.assert_awaited_once()


async def test_collection_discovery_and_semaphore_wait_are_distinct(journey, monkeypatch, caplog):
    from app.services.vinted_parsing import ScrapedVintedItem
    from app.tasks import vinted_poller as poller

    fetch_started, release_fetch, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Semaphore(asyncio.Semaphore):
        async def acquire(self):
            queued.set()
            return await super().acquire()

    semaphore = Semaphore(1)
    await semaphore.acquire()
    queued.clear()

    async def fetch(*_args):
        fetch_started.set()
        await release_fetch.wait()
        return [
            ScrapedVintedItem(
                external_id="collected",
                title="Console",
                condition="Très bon état",
                item_price_cents=100,
                url="https://example.invalid/item",
                photo_url=None,
            )
        ]

    monkeypatch.setattr(poller, "fetch_vinted_page", fetch)
    monkeypatch.setattr(poller, "AsyncSessionLocal", journey.db.maker)
    monkeypatch.setattr(poller, "_get_ingest_semaphore", lambda: semaphore)
    monkeypatch.setattr(ingestion, "default_bot", journey.bot)
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(
            poller.poll_vinted_once(set(), AsyncMock(), category_slug="tech")
        )
        try:
            await asyncio.wait_for(fetch_started.wait(), 3)
            journey.clock.advance(2)
            release_fetch.set()
            await asyncio.wait_for(queued.wait(), 3)
            journey.clock.advance(3)
            semaphore.release()
            assert await asyncio.wait_for(task, 3) == 1
        finally:
            release_fetch.set()
            if not task.done():
                semaphore.release()
            await asyncio.gather(task, return_exceptions=True)
    assert events(caplog, "collection")[0].duration_ms == pytest.approx(2000)
    assert events(caplog, "semaphore_wait")[0].duration_ms == pytest.approx(3000)
    assert events(caplog, "discovery_to_entry")[0].duration_ms == pytest.approx(3000)
    records = [r for r in caplog.records if getattr(r, "event", "") == "alert_latency"]
    assert len({r.collection_id for r in records}) == 1
    assert records[0].collection_id


async def test_frontier_fields_leave_with_the_terminal_after_the_ack(journey, monkeypatch, caplog):
    """La position du lot part avec la phase terminale, après l'accusé, pas avant l'envoi."""
    from app.services.vinted_parsing import ScrapedVintedItem
    from app.tasks import vinted_poller as poller

    catalogue_calls: list[str] = []

    async def fetch(url, _client):
        catalogue_calls.append(url)
        return [
            ScrapedVintedItem(
                external_id=identifier,
                title="Console",
                condition="Très bon état",
                item_price_cents=100,
                url=f"https://example.invalid/{identifier}",
                photo_url=None,
            )
            for identifier in ("999", "2700")
        ]

    monkeypatch.setattr(poller, "fetch_vinted_page", fetch)
    monkeypatch.setattr(poller, "AsyncSessionLocal", journey.db.maker)
    ingest_slot = asyncio.Semaphore(1)
    monkeypatch.setattr(poller, "_get_ingest_semaphore", lambda: ingest_slot)
    monkeypatch.setattr(ingestion, "default_bot", journey.bot)
    started_monotonic = 1234.5
    with caplog.at_level(logging.INFO):
        ingested = await poller.poll_vinted_once(
            seen_ids=set(),
            vinted_client=AsyncMock(),
            category_slug="tech",
            frontier=poller.CycleFrontier(started_monotonic=started_monotonic),
        )

    assert ingested == 2
    assert len(catalogue_calls) == 1
    terminals = events(caplog, "ingestion_terminal")
    assert {record.external_id for record in terminals} == {"999", "2700"}
    assert {record.frontier_id for record in terminals} == {"2700"}
    assert all(record.previous_frontier_id is None for record in terminals)
    assert {record.cycle_started_monotonic for record in terminals} == {started_monotonic}
    assert all(record.duration_unit == "ms" for record in terminals)
    acks = events(caplog, "entry_to_ack")
    assert [record.duration_ms for record in acks] == pytest.approx([200, 200])
    assert all(not hasattr(record, "frontier_id") for record in acks)
    phases = [
        getattr(record, "phase", "")
        for record in caplog.records
        if getattr(record, "event", "") == "alert_latency"
        and getattr(record, "phase", "") in {"entry_to_ack", "ingestion_terminal"}
    ]
    assert phases.count("entry_to_ack") == 2
    assert phases.index("ingestion_terminal") > phases.index("entry_to_ack")


async def test_guard_wait_is_separate_and_measured_before_reads(journey, monkeypatch, caplog):
    from app.use_cases.listing_ingestion_gate import ListingIngestionGate

    gate = ListingIngestionGate()
    monkeypatch.setattr(ingestion, "listing_ingestion_gate", gate)
    waiting = asyncio.Event()
    with caplog.at_level(logging.INFO):
        async with gate.protect("synthetic", "timed"):
            entry = gate._entries[("synthetic", "timed")]
            original = entry.lock.acquire

            async def acquire():
                waiting.set()
                return await original()

            monkeypatch.setattr(entry.lock, "acquire", acquire)
            task = asyncio.create_task(
                ingestion.ProcessListingUseCase(journey.db.session, journey.bot).execute(
                    journey.payload, {}
                )
            )
            await asyncio.wait_for(waiting.wait(), 3)
            journey.clock.advance(0.75)
            journey.bot.send_message.assert_not_awaited()
        await asyncio.wait_for(task, 3)
    assert events(caplog, "guard_wait")[0].duration_ms == pytest.approx(750)
    assert events(caplog, "pre_first_telegram_call")[0].duration_ms == pytest.approx(0)
    assert events(caplog, "entry_to_ack")[0].duration_ms == pytest.approx(950)


async def test_diagnostic_and_clock_failures_do_not_change_ack_or_sql_error(journey, monkeypatch):
    from app.core import alert_latency

    def broken(*_args, **_kwargs):
        raise RuntimeError("synthetic diagnostic failure")

    monkeypatch.setattr(alert_latency.logger, "log", broken)
    monkeypatch.setattr(ingestion.logger, "handle", broken)
    monkeypatch.setattr(alert_latency, "monotonic", broken)
    response = await ingestion.ProcessListingUseCase(journey.db.session, journey.bot).execute(
        journey.payload, {}
    )
    assert response.matches[0].notification_sent
    assert all(await journey.states())
    original = journey.db.session.commit
    calls = 0
    error = SQLAlchemyError("original synthetic SQL error")

    async def commit():
        nonlocal calls
        calls += 1
        if calls in (3, 4):
            raise error
        await original()

    journey.db.session.commit = commit
    with pytest.raises(SQLAlchemyError) as captured:
        await ingestion.ProcessListingUseCase(journey.db.session, journey.bot).execute(
            journey.payload.model_copy(update={"external_id": "sql-error"}), {}
        )
    assert captured.value is error
    assert calls == 4


async def test_final_commit_failure_is_separate_from_post_send_recalculation(
    journey, monkeypatch, caplog
):
    original = journey.db.session.commit
    calls = 0
    error = SQLAlchemyError("original final commit error")

    async def recalculate(_session):
        journey.clock.advance(0.25)

    async def commit():
        nonlocal calls
        calls += 1
        if sys._getframe(1).f_locals.get("final_called") is True:
            journey.clock.advance(0.7)
            raise error
        await original()

    journey.db.session.commit = commit
    monkeypatch.setattr(ingestion, "recalculate_invalidated_series", recalculate)
    with caplog.at_level(logging.INFO), pytest.raises(SQLAlchemyError) as captured:
        await ingestion.ProcessListingUseCase(journey.db.session, journey.bot).execute(
            journey.payload, {}
        )
    assert captured.value is error
    assert all(await journey.states())
    assert events(caplog, "post_send_recalculation")[0].duration_ms == pytest.approx(250)
    final = events(caplog, "final_commit")[0]
    assert final.duration_ms == pytest.approx(700)
    assert final.result == "error"
    assert events(caplog, "ack_to_persistence")[0].result == "notified"


async def test_real_bibliography_background_can_start_before_return(journey, monkeypatch, caplog):
    from app.core import tasks

    category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
    journey.db.session.add(category)
    await journey.db.session.flush()
    alert = await journey.db.session.scalar(select(Alert))
    alert.category_id = category.id
    await journey.db.session.commit()
    payload = journey.payload.model_copy(update={"category_slug": "bd_comics"})
    entered, release_work, final_entered, release_final = (asyncio.Event() for _ in range(4))
    original = journey.db.session.commit
    calls = 0

    async def resolve(_session, **_context):
        entered.set()
        await release_work.wait()
        return None

    async def commit():
        nonlocal calls
        calls += 1
        if sys._getframe(1).f_locals.get("final_called") is True:
            final_entered.set()
            await release_final.wait()
        await original()

    monkeypatch.setattr(ingestion, "resolve_bd_catalog_book", resolve)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", journey.db.maker)
    monkeypatch.setattr(ingestion, "record_sent_cote_state", AsyncMock())
    journey.db.session.commit = commit
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(
            ingestion.ProcessListingUseCase(journey.db.session, journey.bot).execute(payload, {})
        )
        try:
            await asyncio.wait_for(final_entered.wait(), 3)
            await asyncio.wait_for(entered.wait(), 3)
            assert not task.done()
            assert events(caplog, "background_start")
            assert not events(caplog, "background_run")
            release_final.set()
            await asyncio.wait_for(task, 3)
            journey.clock.advance(0.8)
            release_work.set()
            await tasks.wait_all_background_tasks(timeout=3)
        finally:
            release_final.set()
            release_work.set()
            await asyncio.gather(task, return_exceptions=True)
            await tasks.wait_all_background_tasks(timeout=0)
    background = events(caplog, "background_run")
    assert len(background) == 1
    assert background[0].work_kind == "bibliography"
    assert background[0].duration_ms == pytest.approx(800)
    assert background[0].attempt_id == events(caplog, "entry_to_ack")[0].attempt_id


async def test_cancelled_guard_wait_does_not_create_a_delivery(journey, monkeypatch, caplog):
    from app.use_cases.listing_ingestion_gate import ListingIngestionGate

    gate = ListingIngestionGate()
    monkeypatch.setattr(ingestion, "listing_ingestion_gate", gate)
    waiting = asyncio.Event()
    with caplog.at_level(logging.INFO):
        async with gate.protect("synthetic", "timed"):
            entry = gate._entries[("synthetic", "timed")]
            original = entry.lock.acquire

            async def acquire():
                waiting.set()
                return await original()

            monkeypatch.setattr(entry.lock, "acquire", acquire)
            task = asyncio.create_task(
                ingestion.ProcessListingUseCase(journey.db.session, journey.bot).execute(
                    journey.payload, {}
                )
            )
            await asyncio.wait_for(waiting.wait(), 3)
            journey.clock.advance(0.4)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    journey.bot.send_message.assert_not_awaited()
    assert await journey.states() == []
    assert events(caplog, "guard_wait")[0].result == "cancelled"
    assert events(caplog, "guard_wait")[0].duration_ms == pytest.approx(400)
    assert not events(caplog, "entry_to_ack")
    assert not gate._entries

"""Diagnostics bornés à la chaîne d'alerte : horloge, fond et transports contrôlés."""

import asyncio
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage

from app.core import alert_latency as timing
from app.core.config import settings
from app.db.models.listing import Listing
from app.services import telegram_service as telegram
from tests.telegram_fakes import telegram_message


def records(caplog, phase):
    return [
        r
        for r in caplog.records
        if getattr(r, "event", "") == "alert_latency" and getattr(r, "phase", "") == phase
    ]


async def test_photo_transport_retry_pause_and_text_ack_are_separate(monkeypatch, caplog):
    values = [0.0]
    monkeypatch.setattr(timing, "monotonic", lambda: values[0])
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    method = SendMessage(chat_id=601, text="synthetic")
    calls = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def photo(**_kwargs):
        nonlocal calls
        calls += 1
        values[0] += 0.2 if calls == 1 else 0.3
        if calls == 1:
            raise TelegramBadRequest(method=method, message="photo_invalid")
        raise TelegramBadRequest(method=method, message="photo_invalid")

    async def pause(seconds):
        assert seconds == 1
        entered.set()
        await release.wait()

    async def text(**_kwargs):
        values[0] += 0.4
        return telegram_message(601)

    bot = AsyncMock()
    bot.id = 123456789
    bot.send_photo.side_effect, bot.send_message.side_effect = photo, text
    monkeypatch.setattr(
        telegram, "asyncio", SimpleNamespace(sleep=pause, CancelledError=asyncio.CancelledError)
    )
    flow = timing.AlertTiming(ingestion_id=uuid.uuid4(), entered_at=0, acquired_at=0)
    listing = Listing(
        id=uuid.uuid4(),
        source_name="synthetic",
        external_id="photo",
        title="Console",
        url="https://example.invalid/item",
        category_slug="tech",
        item_price_cents=100,
        estimated_total_cents=500,
        photos=["https://example.invalid/photo"],
    )
    with caplog.at_level(logging.INFO), timing.timing_context(flow):
        task = asyncio.create_task(telegram.send_listing_alert(bot, 601, listing))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not task.done()
            values[0] += 1
            release.set()
            result = await asyncio.wait_for(task, 3)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    assert result.outcome == "acknowledged"
    assert [r.duration_ms for r in records(caplog, "transport_attempt")] == pytest.approx(
        [200, 300, 400]
    )
    assert records(caplog, "retry_pause")[0].duration_ms == pytest.approx(1000)
    assert records(caplog, "entry_to_ack")[0].duration_ms == pytest.approx(1900)
    assert records(caplog, "delivery_terminal")[0].duration_ms == pytest.approx(1900)


@pytest.mark.parametrize("outcome", ["returned", "raised", "cancelled"])
async def test_background_snapshot_and_program_to_start_to_end(monkeypatch, caplog, outcome):
    values = [0.0]
    monkeypatch.setattr(timing, "monotonic", lambda: values[0])
    entered, release = asyncio.Event(), asyncio.Event()
    flow = timing.AlertTiming(
        ingestion_id=uuid.uuid4(), alert_id=uuid.uuid4(), attempt_id=uuid.uuid4()
    )
    original_alert, original_attempt = flow.alert_id, flow.attempt_id
    error = RuntimeError("synthetic business failure")

    @timing.observe_background("bibliography")
    async def work():
        entered.set()
        await release.wait()
        if outcome == "raised":
            raise error
        return 42

    with caplog.at_level(logging.INFO):
        with timing.timing_context(flow):
            pending = work()
        flow.alert_id, flow.attempt_id = uuid.uuid4(), uuid.uuid4()
        values[0] = 0.5
        task = asyncio.create_task(pending)
        await asyncio.wait_for(entered.wait(), 3)
        values[0] = 1.5
        if outcome == "cancelled":
            task.cancel()
        release.set()
        if outcome == "returned":
            assert await task == 42
        elif outcome == "raised":
            with pytest.raises(RuntimeError) as captured:
                await task
            assert captured.value is error
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
    assert records(caplog, "background_start")[0].duration_ms == pytest.approx(500)
    end = records(caplog, "background_run")[0]
    assert end.duration_ms == pytest.approx(1000)
    assert end.scheduled_to_end_ms == pytest.approx(1500)
    assert end.result == outcome
    assert end.alert_id == str(original_alert)
    assert end.attempt_id == str(original_attempt)
    assert timing.current() is None


def test_closed_unstarted_background_has_no_orphan_coroutine():
    created = []

    async def inner():
        return None

    @timing.observe_background("bibliography")
    def work():
        created.append(True)
        return inner()

    with timing.timing_context(timing.AlertTiming()):
        pending = work()
    pending.close()
    assert created == []


async def test_outside_alert_chain_no_background_metrics(caplog):
    @timing.observe_background("book_pricing")
    async def work():
        return 42

    with caplog.at_level(logging.INFO):
        assert await work() == 42
    assert not records(caplog, "background_start")
    assert not records(caplog, "background_run")


async def test_formatter_failure_cannot_refuse_or_drop_valid_ack(monkeypatch, caplog):
    monkeypatch.setattr(settings, "AI_ENABLED", False)

    def broken(_record):
        raise RuntimeError("synthetic formatter failure")

    monkeypatch.setattr(telegram.logger, "handle", broken)
    bot = AsyncMock()
    bot.id = 123456789
    receipt = telegram_message(601)
    bot.send_message.return_value = receipt
    listing = Listing(
        id=uuid.uuid4(),
        source_name="synthetic",
        external_id="safe-log",
        title="Console",
        url="https://example.invalid/item",
        category_slug="tech",
        item_price_cents=100,
        estimated_total_cents=500,
        photos=[],
    )
    with caplog.at_level(logging.INFO):
        result = await telegram.send_listing_alert(bot, 601, listing)
    assert result.outcome == "acknowledged"
    assert result.receipt is receipt
    bot.send_message.assert_awaited_once()


async def test_photo_network_failure_has_no_retry_or_text_fallback(monkeypatch):
    """Un transport incertain ne prouve pas que Telegram n'a rien accepté."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    bot = AsyncMock()
    bot.id = 123456789
    bot.send_photo.side_effect = TelegramNetworkError(
        method=SendMessage(chat_id=601, text="synthetic"), message="synthetic uncertainty"
    )
    listing = Listing(id=uuid.uuid4(), source_name="synthetic", external_id="unknown",
        title="Console", url="https://example.invalid", category_slug="tech",
        item_price_cents=100, photos=["https://example.invalid/photo"])
    result = await telegram.send_listing_alert(bot, 601, listing)
    assert result.outcome == "unknown"
    assert result.receipt is None
    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()

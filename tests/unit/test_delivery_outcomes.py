"""Un accusé exploitable seul autorise un succès, sans fuite de contenu dans les diagnostics."""

import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage

from app.core.config import settings
from app.core.logging import JSONFormatter
from app.db.models.listing import Listing
from app.services.telegram_service import send_listing_alert
from tests.telegram_fakes import telegram_message


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", False)


def listing(photos=None):
    return Listing(
        id=uuid.uuid4(),
        source_name="synthetic",
        external_id="one",
        title="RAW_BODY_SYNTHETIC",
        normalized_title="console",
        category_slug="tech",
        url="https://example.invalid/sensitive_path",
        item_price_cents=100,
        estimated_total_cents=500,
        photos=photos or [],
    )


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        SimpleNamespace(message_id=42),
        telegram_message(message_id=0),
        telegram_message(chat_id=999),
    ],
)
async def test_no_success_without_exploitable_message(receipt):
    bot = AsyncMock()
    bot.send_message.return_value = receipt
    result = await send_listing_alert(bot, 601, listing())
    assert not result
    assert result.outcome == "unknown"
    assert result.receipt is None


async def test_valid_receipt_is_retained_in_memory():
    receipt = telegram_message()
    bot = AsyncMock()
    bot.send_message.return_value = receipt
    result = await send_listing_alert(bot, 601, listing())
    assert result.outcome == "acknowledged"
    assert result.receipt is receipt
    assert result.transport == "text"


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (
            TelegramBadRequest(
                method=SendMessage(chat_id=601, text="synthetic"),
                message="Bad Request: wrong type of the web page content"
            ),
            "rejected",
        ),
        (
            TelegramNetworkError(
                method=SendMessage(chat_id=601, text="synthetic"), message="synthetic timeout"
            ),
            "unknown",
        ),
        (RuntimeError("synthetic undocumented exception"), "unknown"),
    ],
)
async def test_classification_does_not_make_network_failure_certain(error, outcome):
    bot = AsyncMock()
    bot.send_message.side_effect = error
    result = await send_listing_alert(bot, 601, listing())
    assert not result
    assert result.outcome == outcome


async def test_local_failure_is_proven_before_call(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise ValueError("synthetic local validation")

    monkeypatch.setattr("app.services.telegram_service.get_listing_alert_keyboard", refuse)
    bot = AsyncMock()
    result = await send_listing_alert(bot, 601, listing())
    assert result.outcome == "not_sent"
    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_not_awaited()


async def test_final_json_diagnostics_are_correlated_and_exclude_content(caplog):
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramNetworkError(
        method=SendMessage(chat_id=601, text="synthetic"),
        message="RAW_BODY_SYNTHETIC https://example.invalid/sensitive_path",
    )
    value = listing()
    alert = SimpleNamespace(id=uuid.uuid4())
    attempt = uuid.uuid4()
    with caplog.at_level(logging.INFO):
        await send_listing_alert(bot, 601, value, alert=alert, attempt_id=attempt)
    output = [JSONFormatter().format(record) for record in caplog.records]
    assert "RAW_BODY_SYNTHETIC" not in "".join(output)
    assert "sensitive_path" not in "".join(output)
    records = [json.loads(line) for line in output if '"event": "notification_delivery"' in line]
    assert records
    for record in records:
        assert record["alert_id"] == str(alert.id)
        assert record["listing_id"] == str(value.id)
        assert record["attempt_id"] == str(attempt)
        assert record["transport"] == "text"
        assert record["delivery_result"] == "unknown"
        assert record["persistence_stage"] == "not_started"


@pytest.mark.parametrize("errors", [1, 2])
async def test_ambiguous_photo_stops_without_retry_or_fallback(monkeypatch, caplog, errors):

    accepted = []
    bot = AsyncMock()
    calls = 0

    async def photo(**_kwargs):
        nonlocal calls
        calls += 1
        if calls <= errors:
            accepted.append("photo")
            raise TelegramNetworkError(
                method=SendMessage(chat_id=601, text="synthetic"),
                message="RAW_BODY_SYNTHETIC response lost",
            )
        raise TelegramBadRequest(
            method=SendMessage(chat_id=601, text="synthetic"),
                message="Bad Request: wrong type of the web page content"
        )

    async def text(**_kwargs):
        accepted.append("text")
        return telegram_message()

    pause = AsyncMock()
    monkeypatch.setattr("app.services.telegram_service.asyncio.sleep", pause)
    bot.send_photo.side_effect, bot.send_message.side_effect = photo, text
    with caplog.at_level(logging.INFO):
        result = await send_listing_alert(bot, 601, listing(["https://example.invalid/photo.jpg"]))
    assert result.outcome == "unknown"
    assert accepted == ["photo"]
    assert bot.send_photo.await_count == 1
    bot.send_message.assert_not_awaited()
    pause.assert_not_awaited()
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "notification_delivery"
    ]
    assert [r.delivery_result for r in records] == ["unknown"]


async def test_none_photo_receipt_stops_without_text_fallback(monkeypatch):
    bot = AsyncMock()
    bot.send_photo.return_value = None
    bot.send_message.return_value = telegram_message()
    pause = AsyncMock()
    monkeypatch.setattr("app.services.telegram_service.asyncio.sleep", pause)
    result = await send_listing_alert(bot, 601, listing(["https://example.invalid/photo.jpg"]))
    assert not result
    assert result.outcome == "unknown"
    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    pause.assert_not_awaited()


async def test_post_ack_enrichment_failure_does_not_destroy_receipt(monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", True)

    def broken(**_kwargs):
        raise RuntimeError("synthetic scheduling failure")

    monkeypatch.setattr("app.services.telegram_service.ai_post_alert_pipeline.enrich_alert", broken)
    bot = AsyncMock()
    receipt = telegram_message()
    bot.send_message.return_value = receipt
    result = await send_listing_alert(bot, 601, listing())
    assert result
    assert result.receipt is receipt


async def test_invalid_photo_url_is_not_exposed_in_final_log(caplog):
    bot = AsyncMock()
    bot.send_message.return_value = telegram_message()
    with caplog.at_level(logging.INFO):
        await send_listing_alert(bot, 601, listing(["http://example.invalid/sensitive_path"]))
    assert "sensitive_path" not in "".join(JSONFormatter().format(r) for r in caplog.records)


@pytest.mark.parametrize("unknown_photo", [True, False])
async def test_rejected_fallback_does_not_erase_earlier_uncertainty(monkeypatch, unknown_photo):
    method = SendMessage(chat_id=601, text="synthetic")
    rejection = TelegramBadRequest(
        method=method, message="Bad Request: wrong type of the web page content",
    )
    bot = AsyncMock()
    bot.send_photo.side_effect = [
        TelegramNetworkError(method=method, message="synthetic response lost")
        if unknown_photo
        else rejection,
        rejection,
    ]
    bot.send_message.side_effect = rejection
    monkeypatch.setattr("app.services.telegram_service.asyncio.sleep", AsyncMock())
    result = await send_listing_alert(bot, 601, listing(["https://example.invalid/photo.jpg"]))
    assert not result
    assert result.outcome == ("unknown" if unknown_photo else "rejected")
    assert bot.send_photo.await_count == (1 if unknown_photo else 2)
    assert bot.send_message.await_count == (0 if unknown_photo else 1)

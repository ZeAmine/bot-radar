"""Un accusé exploitable seul autorise un succès, sans fuite de contenu dans les diagnostics."""

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
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

    monkeypatch.setattr("app.services.telegram_service.listing_alert_keyboard", refuse)
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


async def test_initial_receipt_does_not_wait_for_generation(monkeypatch):
    await _assert_initial_receipt(monkeypatch, "ok")


@pytest.mark.parametrize("scenario", [
    "timeout", "http_error", "invalid_json", "validation_error", "empty", "interrupted",
])
async def test_initial_receipt_survives_generation_failure(monkeypatch, scenario):
    await _assert_initial_receipt(monkeypatch, scenario)


@pytest.mark.parametrize("scenario", [
    "ok", "timeout", "http_error", "invalid_json", "validation_error", "empty", "interrupted",
])
async def test_alternate_adapter_preserves_initial_receipt(monkeypatch, scenario):
    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    await _assert_initial_receipt(monkeypatch, scenario)


async def _assert_initial_receipt(monkeypatch, scenario):
    from app.core.tasks import safe_create_task
    from app.providers.llm import base as llm_base
    from app.providers.llm.registry import build_llm_client
    from app.services.ai.pipeline import AIPostAlertPipeline

    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_FEATURE_PROFIT_SCORING", True)
    entered = asyncio.Event()
    release = asyncio.Event()
    requests = []
    tasks = []

    async def respond(request):
        requests.append(request)
        entered.set()
        await release.wait()
        if scenario == "timeout":
            raise httpx.ReadTimeout("synthetic")
        if scenario == "http_error":
            return httpx.Response(429)
        content = json.dumps({"profit_scoring": {
            "pepite_score": 8, "estimated_resale_cents": 1000, "net_profit_cents": 500,
        }})
        if scenario == "invalid_json":
            content = "not-json"
        elif scenario == "validation_error":
            content = '{"profit_scoring":{"pepite_score":99}}'
        elif scenario == "empty":
            content = ""
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "length" if scenario == "interrupted" else "stop",
            "message": {"content": content},
        }]})

    def schedule(coro, name):
        task = safe_create_task(coro, name=name)
        tasks.append(task)
        return task

    client = build_llm_client()
    client.api_key = "synthetic"
    client.base_url = "https://example.invalid"
    monkeypatch.setattr(
        "app.services.telegram_service.ai_post_alert_pipeline", AIPostAlertPipeline(client),
    )
    monkeypatch.setattr("app.services.telegram_service.safe_create_task", schedule)
    bot = AsyncMock()
    receipt = telegram_message(message_id=876)
    bot.send_message.return_value = receipt
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        try:
            result = await asyncio.wait_for(send_listing_alert(bot, 601, listing()), timeout=1)
            assert result.receipt is receipt
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not release.is_set()
            assert len(tasks) == 1
            assert not tasks[0].done()
            assert bot.send_message.await_count == 1
            release.set()
            await asyncio.wait_for(tasks[0], timeout=1)
            assert len(requests) == 1
            assert bot.send_message.await_count == (2 if scenario == "ok" else 1)
            if scenario == "ok":
                assert bot.send_message.call_args.kwargs["reply_to_message_id"] == 876
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

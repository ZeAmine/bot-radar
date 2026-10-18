import logging
import uuid
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.db.models.listing import Listing
from app.services.ai.modules.negotiation import NegotiationModule
from app.services.ai.modules.profit_scoring import ProfitScoringModule
from tests.fakes_llm import FakeLLM


@pytest.fixture
def listing():
    return Listing(
        id=uuid.uuid4(), title="Console portable", category_slug="gaming",
        item_price_cents=10000, estimated_total_cents=11000,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"suggested_offer_cents": -100, "message": "Bonjour"},
    {"suggested_offer_cents": 0, "message": "Bonjour"},
    {"suggested_offer_cents": 9000, "message": "  \n\t"},
    {"suggested_offer_cents": "incorrect", "message": "Bonjour"},
])
async def test_invalid_offer_uses_existing_fallback(listing, payload):
    with patch.object(settings, "AI_ENABLED", True), patch.object(
        settings, "AI_FEATURE_NEGOTIATION", True,
    ):
        expected = await NegotiationModule(client=FakeLLM(enabled=False)).generate_offer(listing)
        result = await NegotiationModule(client=FakeLLM(text=payload)).generate_offer(listing)
    assert result == expected


@pytest.mark.asyncio
async def test_valid_offer_is_trimmed_and_used(listing):
    client = FakeLLM(text={"suggested_offer_cents": 9000, "message": "  Bonjour !  "})
    with patch.object(settings, "AI_ENABLED", True), patch.object(
        settings, "AI_FEATURE_NEGOTIATION", True,
    ):
        result = await NegotiationModule(client=client).generate_offer(listing)
    assert result is not None
    assert result.suggested_offer_cents == 9000
    assert result.suggested_offer_euros == 90
    assert result.message == "Bonjour !"
    assert len(client.calls) == 1


@pytest.mark.parametrize("score", [0, 11, float("inf"), float("-inf"), float("nan")])
def test_invalid_profit_score_is_not_used(listing, score):
    module = ProfitScoringModule()
    result = module.parse_chunk_result({"pepite_score": score}, listing, None)
    assert result is None
    assert module.format_telegram_html(result) is None


def test_negative_resale_is_not_used(listing):
    result = ProfitScoringModule().parse_chunk_result(
        {"estimated_resale_cents": -1, "pepite_score": 8}, listing, None,
    )
    assert result is None


@pytest.mark.parametrize("score", [1, 10, None])
def test_profit_optional_fields_and_negative_margin_remain_valid(listing, score):
    result = ProfitScoringModule().parse_chunk_result(
        {"estimated_resale_cents": 0, "net_profit_cents": -11000, "pepite_score": score},
        listing, None,
    )
    assert result is not None
    assert result.pepite_score == score
    assert result.estimated_resale_cents == 0
    assert result.net_profit_cents == -11000
    assert ProfitScoringModule().parse_chunk_result({}, listing, None) is not None


def test_profit_validation_log_omits_provider_content(listing, caplog):
    sentinel = "PRIVATE_PROVIDER_CONTENT"
    with caplog.at_level(logging.WARNING):
        result = ProfitScoringModule().parse_chunk_result(
            {"pepite_score": sentinel}, listing, None,
        )
    assert result is None
    assert sentinel not in caplog.text
    assert any(
        getattr(record, "error_category", None) == "business_validation"
        for record in caplog.records
    )

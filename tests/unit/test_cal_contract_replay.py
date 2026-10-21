import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import extract_best_historical_low
from app.providers.chasse_aux_livres import LOOKUP_POLL_MAX, ChasseAuxLivresProvider
from app.providers.registry import ProviderRegistry
from app.services.book_market_pricing import BookPricingResult, _stamp_collection
from app.services.market_refresh_policy import component_due

FIXTURES = Path(__file__).parents[1] / "fixtures/chasse"
SEARCH_PAGE = (
    '<div id="hash-cont" data-ajax="true" '
    'data-hash="mockhash1234567890abcdef12345678"></div>'
)
SEARCH_RESULTS = {"d": '<a href="/prix/2756079332/9782756079332">Jean Doux</a>'}
PRODUCT_PAGE = (
    '<div data-asin="2756079332" data-lvs="synthetic" '
    'data-nbeng="25" data-didh="abc123"></div>'
)
OLD = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


async def _collect(monkeypatch, history_body: dict) -> tuple:
    partial = json.loads(
        (FIXTURES / "partial_offers_2026_10_19.json").read_text()
    )["payload"]
    monkeypatch.setattr(
        "app.providers.chasse_aux_livres.resolve_session_token",
        AsyncMock(return_value="synthetic-session"),
    )
    history_call = AsyncMock(return_value=httpx.Response(200, json=history_body))
    monkeypatch.setattr(
        "app.providers.chasse_aux_livres.chasse_session.get_history", history_call,
    )
    requests: list[str] = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/search":
            return httpx.Response(200, text=SEARCH_PAGE)
        if request.url.path == "/rest/search-results":
            return httpx.Response(200, json=SEARCH_RESULTS)
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=PRODUCT_PAGE)
        return httpx.Response(200, json=partial)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ChasseAuxLivresProvider(
            http_client=client, sleep=AsyncMock(),
        ).get_offers(
            isbn13="9782756079332", title="Jean Doux",
        )
    return result, requests, history_call


async def _pricing(result):
    return BookPricingResult.from_offers(
        result,
        ProviderRegistry.select_best_offer(result),
        extract_best_historical_low(result),
    )


async def test_raw_cal_partial_and_empty_history_replay_through_pricing(monkeypatch) -> None:
    result, requests, history_call = await _collect(
        monkeypatch, {"price_includes_shipping": True, "stats": []},
    )

    assert requests == [
        "/search",
        "/rest/search-results",
        "/prix/2756079332/9782756079332",
    ] + ["/rest/lookup/results"] * (1 + LOOKUP_POLL_MAX)
    assert [(attempt.operation, attempt.status) for attempt in result.attempts] == [
        ("current", "partial"),
        ("history", "success"),
    ]
    assert result.historical_checked is True
    assert not result
    history_call.assert_awaited_once()

    pricing = await _pricing(result)
    cache = BookMarketCache(last_successful_collection_at=OLD)

    _stamp_collection(cache, pricing)

    assert pricing.lowest_delivered_price_cents is None
    assert pricing.current_collection_usable is False
    assert cache.last_successful_collection_at == OLD
    assert cache.historical_checked_at == pricing.collected_at
    assert cache.source_history is None
    assert cache.historical_low_1y_cents is None
    assert component_due(cache, "history", pricing.collected_at + timedelta(days=6)) == (
        pricing.collected_at + timedelta(days=7)
    )


async def test_raw_cal_history_with_points_replays_to_source_history(monkeypatch) -> None:
    history = json.loads(
        (FIXTURES / "jean_doux_history_2026_10_18.json").read_text()
    )
    result, _, _ = await _collect(monkeypatch, history)

    assert result.historical_checked is True
    assert any(offer.is_historical for offer in result)
    pricing = await _pricing(result)
    cache = BookMarketCache(last_successful_collection_at=OLD)

    _stamp_collection(cache, pricing)

    assert pricing.current_collection_usable is False
    assert cache.last_successful_collection_at == OLD
    assert cache.historical_checked_at == pricing.collected_at
    assert cache.source_history is not None
    assert cache.historical_low_1y_cents is not None
    assert cache.source_history["condition"] == "Très bon état +"


async def test_raw_cal_malformed_history_series_does_not_stamp_a_date(monkeypatch) -> None:
    result, _, _ = await _collect(
        monkeypatch,
        {"price_includes_shipping": True, "stats": [{"label": "Très bon état +",
                                                     "statValues": "not-a-list"}]},
    )

    assert result.historical_checked is False
    assert [(attempt.operation, attempt.status) for attempt in result.attempts] == [
        ("current", "partial"),
        ("history", "failed"),
    ]
    pricing = await _pricing(result)
    cache = BookMarketCache(
        last_successful_collection_at=OLD,
        historical_checked_at=OLD,
        source_history={"price_cents": 1200},
    )

    _stamp_collection(cache, pricing)

    assert cache.historical_checked_at == OLD
    assert cache.source_history == {"price_cents": 1200}


async def test_raw_cal_invalid_history_does_not_stamp_a_date(monkeypatch) -> None:
    result, _, _ = await _collect(
        monkeypatch, {"price_includes_shipping": True, "stats": "not-a-list"},
    )

    assert result.historical_checked is False
    assert [(attempt.operation, attempt.status) for attempt in result.attempts] == [
        ("current", "partial"),
        ("history", "failed"),
    ]
    pricing = await _pricing(result)
    cache = BookMarketCache(
        last_successful_collection_at=OLD,
        historical_checked_at=OLD,
        source_history={"price_cents": 1200},
    )

    _stamp_collection(cache, pricing)

    assert cache.last_successful_collection_at == OLD
    assert cache.historical_checked_at == OLD
    assert cache.source_history == {"price_cents": 1200}

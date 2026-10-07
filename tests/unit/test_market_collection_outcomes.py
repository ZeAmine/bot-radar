"""Une erreur fournisseur ne doit pas devenir une absence normale d'offres."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.providers.momox import MomoxProvider
from app.providers.registry import ProviderRegistry
from app.services import market_health


async def test_failed_provider_is_named_in_the_collection():
    failed = MagicMock(name="failed")
    failed.name = "test_source"
    failed.get_offers = AsyncMock(side_effect=TimeoutError())
    collection = await ProviderRegistry(providers=[failed]).fetch_all_offers(
        isbn13=None, title="Livre"
    )
    assert collection.failed_providers == ("test_source",)


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_http_error_is_not_a_successful_empty_catalogue(status):
    client = AsyncMock()
    client.get.return_value = httpx.Response(status, request=httpx.Request("GET", "https://test"))
    offers = await MomoxProvider(http_client=client).get_offers(
        isbn13="9782205078054", title="Livre"
    )
    assert offers.failed_providers == ("momox",)


async def test_successful_empty_catalogue_has_no_failed_provider():
    provider = MagicMock()
    provider.name = "empty"
    provider.get_offers = AsyncMock(return_value=[])
    collection = await ProviderRegistry(providers=[provider]).fetch_all_offers(
        isbn13=None, title="Livre"
    )
    assert collection == []
    assert collection.failed_providers == ()


async def test_empty_catalogue_does_not_emit_an_incident(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(market_health, "notify_admins", sent)
    client = MagicMock()
    client.set = AsyncMock(return_value=True)
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    await market_health.notify_no_prices_found(3, 3, 0, incremental=False, mode_label="complet")
    sent.assert_not_awaited()


async def test_redis_failure_does_not_repeat_the_same_incident(monkeypatch):
    client = MagicMock()
    client.set = AsyncMock(side_effect=ConnectionError())
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    assert await market_health._claim_no_prices_notification_slot()
    assert not await market_health._claim_no_prices_notification_slot()


async def test_scheduler_and_panel_share_one_collection(monkeypatch):
    import asyncio
    import uuid

    from app.core.constants import ComparisonStatus
    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.services import book_market_pricing as pricing
    from app.tasks import refresh_book_prices as refresh

    book = Book(id=uuid.uuid4(), title="Livre", normalized_title="livre")
    cache = BookMarketCache(comparison_status=ComparisonStatus.AVAILABLE)
    entered, release = asyncio.Event(), asyncio.Event()
    network_calls = []

    async def one(_book_id, **_kwargs):
        network_calls.append("panel")
        entered.set()
        await release.wait()
        return cache

    async def another(_book, **_kwargs):
        network_calls.append("scheduler")
        return []

    monkeypatch.setattr(pricing, "_price_book_once", one)
    monkeypatch.setattr(refresh, "fetch_offers_for_book", another)
    monkeypatch.setattr(refresh, "persist_market_result", AsyncMock(return_value=cache))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=MagicMock(commit=AsyncMock()))
    context.__aexit__ = AsyncMock()
    monkeypatch.setattr(refresh, "AsyncSessionLocal", lambda: context)
    panel_task = asyncio.create_task(pricing.price_book_now(book.id, reason="panel"))
    await entered.wait()
    scheduled = asyncio.create_task(
        refresh._price_one_book(book, MagicMock(), None, asyncio.Semaphore(1))
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(panel_task, scheduled)
    assert network_calls == ["panel"]


def test_market_panel_keeps_the_cheapest_offer_for_each_merchant():
    from app.db.models.book_market_cache import BookMarketCache
    from app.services.market_price_service import _extract_partners

    cache = BookMarketCache(
        current_offers=[
            {
                "provider_name": "shop",
                "delivered_price_cents": 1000,
                "item_condition": "Très bon état",
            },
            {
                "provider_name": "shop",
                "delivered_price_cents": 2000,
                "item_condition": "Très bon état",
            },
        ]
    )
    assert _extract_partners(cache, None, None)["shop"]["total_cents"] == 1000

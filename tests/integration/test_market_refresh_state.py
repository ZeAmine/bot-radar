"""Le prix ancien et le résultat de la nouvelle collecte restent distincts."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from app.core.constants import ComparisonStatus
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import MarketOffer, MarketOffers
from app.services import book_market_pricing
from app.services.book_market_pricing import price_book
from tests.database import postgres_replayed_database


async def test_failed_refresh_preserves_real_observation_date_and_names_source():
    async with postgres_replayed_database() as database:
        s = database.session
        book = Book(title="Livre", normalized_title="livre")
        s.add(book)
        await s.flush()
        old = datetime(2026, 9, 1, tzinfo=UTC)
        cache = BookMarketCache(
            book_id=book.id,
            lowest_delivered_price_cents=1500,
            checked_at=old,
            comparison_status=ComparisonStatus.AVAILABLE,
            current_offers=[{"provider_name": "old_source", "delivered_price_cents": 1500}],
        )
        s.add(cache)
        await s.commit()
        registry = MagicMock()
        registry.fetch_all_offers = AsyncMock(
            return_value=MarketOffers(failed_providers=("test_source",))
        )
        await price_book(s, book, registry=registry)
        await s.commit()
        assert cache.checked_at == old
        assert cache.failed_provider_names == ["test_source"]
        assert cache.collection_status == "failed"
        assert cache.last_attempt_at > old
        assert cache.lowest_delivered_price_cents == 1500
        assert cache.current_offers[0]["provider_name"] == "old_source"


async def test_fresh_current_price_does_not_redate_an_old_historical_low():
    async with postgres_replayed_database() as database:
        s = database.session
        book = Book(title="Livre", normalized_title="livre")
        s.add(book)
        await s.flush()
        old = datetime(2026, 9, 1, tzinfo=UTC)
        cache = BookMarketCache(
            book_id=book.id,
            historical_low_1y_cents=500,
            historical_low_at=old,
            checked_at=old,
            comparison_status=ComparisonStatus.AVAILABLE,
        )
        s.add(cache)
        await s.commit()
        registry = MagicMock()
        registry.fetch_all_offers = AsyncMock(
            return_value=[
                MarketOffer(
                    provider_name="source",
                    item_price_cents=1000,
                    shipping_price_cents=100,
                    delivered_price_cents=1100,
                    source_url="https://example.com/book",
                    item_condition="Très bon état",
                )
            ]
        )
        await price_book(s, book, registry=registry)
        assert cache.checked_at > old
        assert cache.historical_low_at == old


async def test_whole_collection_timeout_is_persisted(monkeypatch):
    async with postgres_replayed_database() as database:
        s = database.session
        book = Book(title="Livre", normalized_title="livre")
        s.add(book)
        await s.commit()
        provider = MagicMock()
        provider.name = "slow_source"
        registry = MagicMock(providers=[provider])
        registry.fetch_all_offers = AsyncMock(side_effect=TimeoutError())
        monkeypatch.setattr(book_market_pricing, "AsyncSessionLocal", database.maker)
        cache = await book_market_pricing.price_book_now(book.id, reason="test", registry=registry)
        assert cache is not None
        assert cache.collection_status == "failed"
        assert cache.failed_provider_names == ["slow_source"]

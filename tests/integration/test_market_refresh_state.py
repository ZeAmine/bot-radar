"""Le prix ancien et le résultat de la nouvelle collecte restent distincts."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.constants import ComparisonStatus
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import MarketOffer, MarketOffers
from app.services import book_market_pricing
from app.services.book_market_pricing import price_book
from app.services.market_price_service import admissible_reference_price
from tests.database import postgres_replayed_database


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("validity", ["expired", "fresh", "unknown"])
async def test_empty_collection_never_renews_price_validity(failed, validity):
    """Un relevé informatif reste conservé sans devenir une nouvelle observation."""
    async with postgres_replayed_database() as db:
        now = datetime.now(UTC)
        expires = {
            "expired": now - timedelta(days=2),
            "fresh": now + timedelta(hours=2), "unknown": None,
        }[validity]
        book = Book(title="Livre", normalized_title="livre")
        db.session.add(book)
        await db.session.flush()
        cache = BookMarketCache(
            book_id=book.id, lowest_delivered_price_cents=1500,
            checked_at=now - timedelta(days=3), expires_at=expires,
            comparison_status=ComparisonStatus.AVAILABLE,
        )
        db.session.add(cache)
        await db.session.commit()
        registry = MagicMock()
        registry.fetch_all_offers = AsyncMock(return_value=MarketOffers(
            failed_providers=("source",) if failed else (),
        ))
        for _ in range(2):
            await price_book(db.session, book, registry=registry)
            await db.session.commit()
        async with db.maker() as reader:
            persisted = await reader.get(BookMarketCache, cache.id)
            stored_expiry = persisted.expires_at
            assert (stored_expiry.replace(tzinfo=UTC) if stored_expiry else None) == expires
            assert persisted.lowest_delivered_price_cents == 1500
            assert persisted.checked_at.replace(tzinfo=UTC) == now - timedelta(days=3)
            assert persisted.collection_status == ("failed" if failed else "empty")
            assert admissible_reference_price(persisted) == (
                (1500, False) if validity == "fresh" else (None, False)
            )


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


@pytest.mark.parametrize("failed", [False, True])
async def test_linked_book_keeps_refresh_metadata_with_production_autoflush_disabled(failed):
    from app.db.models.alert import Alert
    from app.db.models.alert_book import AlertBook
    from app.db.models.category import Category
    from app.db.models.user import User

    async with postgres_replayed_database() as database:
        s = database.session
        s.autoflush = False
        user = User(telegram_user_id=1234, is_allowed=True)
        category = Category(slug="bd_comics", label="BD", emoji="📚", is_active=True)
        s.add_all([user, category])
        await s.flush()
        alert = Alert(
            user_id=user.id, category_id=category.id, name="Livre", include_keywords=["livre"]
        )
        book = Book(title="Livre", normalized_title="livre")
        s.add_all([alert, book])
        await s.flush()
        cache = BookMarketCache(
            book_id=book.id,
            lowest_delivered_price_cents=1000,
            comparison_status=ComparisonStatus.AVAILABLE,
        )
        s.add_all([cache, AlertBook(alert_id=alert.id, book_id=book.id)])
        await s.commit()
        registry = MagicMock()
        registry.fetch_all_offers = AsyncMock(
            return_value=(
                MarketOffers(failed_providers=("test_source",))
                if failed
                else MarketOffers(
                    [
                        MarketOffer(
                            provider_name="source",
                            item_price_cents=1500,
                            shipping_price_cents=0,
                            delivered_price_cents=1500,
                            source_url="https://example.com",
                            item_condition="Très bon",
                        )
                    ]
                )
            )
        )
        await price_book(s, book, registry=registry)
        await s.commit()
        await s.refresh(cache)
        assert cache.collection_status == ("failed" if failed else "complete")
        assert cache.failed_provider_names == (["test_source"] if failed else [])
        assert cache.last_attempt_at is not None

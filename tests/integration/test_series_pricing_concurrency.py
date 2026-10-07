"""Les transactions concurrentes de deux tomes doivent laisser un total cohérent."""

import asyncio
import os

import pytest

from app.core.constants import AlertStatus, ComparisonStatus
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.providers.base import MarketOffer
from app.services.book_market_pricing import BookPricingResult, persist_market_result
from tests.database import postgres_replayed_database


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="Verrou PostgreSQL réel requis")
async def test_two_committing_tomes_leave_the_complete_series_total(monkeypatch):
    async with postgres_replayed_database() as database:
        s = database.session
        user = User(telegram_user_id=123, is_allowed=True)
        category = Category(slug="bd_comics", label="BD", emoji="📚", is_active=True)
        s.add_all([user, category])
        await s.flush()
        alert = Alert(
            user_id=user.id,
            category_id=category.id,
            name="Série",
            include_keywords=["série"],
            status=AlertStatus.ACTIVE,
            series_total_cents=200,
        )
        books = [Book(title=f"Série Tome {n}", normalized_title=f"serie {n}") for n in (1, 2)]
        s.add_all([alert, *books])
        await s.flush()
        for book in books:
            s.add(AlertBook(alert_id=alert.id, book_id=book.id))
            s.add(
                BookMarketCache(
                    book_id=book.id,
                    lowest_delivered_price_cents=100,
                    comparison_status=ComparisonStatus.AVAILABLE,
                )
            )
        await s.commit()
        ready = asyncio.Event()
        arrivals = 0
        read = MarketPriceRepository.get_alert_ids_linked_to_book
        write = MarketPriceRepository.set_series_total

        async def synchronize(repo, book_id):
            nonlocal arrivals
            ids = await read(repo, book_id)
            arrivals += 1
            if arrivals == 2:
                ready.set()
            await ready.wait()
            return ids

        async def delayed_write(repo, alert_id, total):
            await asyncio.sleep(0.05)
            await write(repo, alert_id, total)

        monkeypatch.setattr(MarketPriceRepository, "get_alert_ids_linked_to_book", synchronize)
        monkeypatch.setattr(MarketPriceRepository, "set_series_total", delayed_write)

        async def update(book_id, price):
            async with database.maker() as tx:
                offer = MarketOffer(
                    provider_name="source",
                    item_price_cents=price,
                    shipping_price_cents=0,
                    delivered_price_cents=price,
                    source_url="https://example.com",
                    item_condition="Très bon",
                )
                result = BookPricingResult.from_offers([offer], offer, None)
                await persist_market_result(tx, book_id, result)
                await tx.commit()

        await asyncio.wait_for(
            asyncio.gather(update(books[0].id, 200), update(books[1].id, 300)), 5
        )
        await s.refresh(alert)
        assert alert.series_total_cents == 500

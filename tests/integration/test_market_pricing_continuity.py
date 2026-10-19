from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
from app.providers.registry import ProviderRegistry
from app.services.book_market_pricing import price_book_now
from tests.database import postgres_replayed_database

CODES = ['9782756079332', '9782205049657', '9782205051995', '9782205059250',
         '9782808500487', '9782413091424', '9791026856405']


class Source:
    name = 'chasse_aux_livres'

    def __init__(self, *, failing=False):
        self.calls = []
        self.failing = failing

    async def get_offers(self, *, isbn13, **_kwargs):
        self.calls.append(isbn13)
        if self.failing:
            return MarketOffers(failed_providers=(self.name,), attempts=(
                CollectionAttempt(self.name, 'current', 'failed', 'timeout'),
                CollectionAttempt(self.name, 'history', 'failed', 'timeout'),
            ))
        return MarketOffers([
            MarketOffer(self.name, 1000, 200, 1200, 'https://example.com/current',
                        item_condition='Très bon', price_includes_shipping=True),
            MarketOffer(self.name, 921, None, 921, 'https://example.com/history',
                        item_condition='Très bon état +', price_includes_shipping=True,
                        is_historical=True, is_available=False,
                        observed_at=datetime.now(UTC)-timedelta(days=2)),
        ], historical_checked=True, attempts=(CollectionAttempt(self.name, 'current'),
                                             CollectionAttempt(self.name, 'history')))


async def seed(session):
    user = User(telegram_user_id=424242, is_allowed=True)
    category = Category(slug='bd_comics', label='BD', emoji='B', is_active=True)
    session.add_all([user, category])
    await session.flush()
    alert = Alert(user_id=user.id, category_id=category.id, name='Éditions',
                  include_keywords=['editions'])
    session.add(alert)
    await session.flush()
    books = [Book(title=f'Édition {i}', normalized_title=f'edition {i}', isbn13=code)
             for i, code in enumerate(CODES)]
    session.add_all(books)
    await session.flush()
    session.add_all([AlertBook(alert_id=alert.id, book_id=book.id) for book in books])
    await session.commit()
    return books


async def test_pricing_deduplicates_and_reuses_recent_editions(monkeypatch):
    import asyncio

    source = Source()
    registry = ProviderRegistry(providers=[source])
    async with postgres_replayed_database() as database:
        books = await seed(database.session)
        monkeypatch.setattr('app.services.book_market_pricing.AsyncSessionLocal', database.maker)
        await asyncio.gather(*(price_book_now(books[0].id, reason='backfill', registry=registry)
                               for _ in range(3)))
        for book in books[1:]:
            await price_book_now(book.id, reason='backfill', registry=registry)
        assert source.calls == CODES
        for book in books:
            await price_book_now(book.id, reason='backfill', registry=registry)
        assert source.calls == CODES
        async with database.maker() as reader:
            caches = list((await reader.scalars(select(BookMarketCache))).all())
            assert len(caches) == 7
            assert all(cache.source_history['price_cents'] == 921 for cache in caches)
            assert all(cache.next_refresh_at is not None for cache in caches)


async def test_pricing_failure_preserves_prices_without_global_stop(monkeypatch):
    source = Source(failing=True)
    registry = ProviderRegistry(providers=[source])
    old = datetime.now(UTC)-timedelta(days=8)
    async with postgres_replayed_database() as database:
        books = await seed(database.session)
        for book in books:
            database.session.add(BookMarketCache(book_id=book.id,
                lowest_delivered_price_cents=1200, last_successful_collection_at=old,
                historical_checked_at=old, source_history={'price_cents': 921,
                'condition': 'Très bon état +', 'days': 365, 'shipping_included': True,
                'collected_at': old.isoformat()}))
        await database.session.commit()
        monkeypatch.setattr('app.services.book_market_pricing.AsyncSessionLocal', database.maker)
        for book in books[:3]:
            await price_book_now(book.id, reason='backfill', registry=registry)
        assert source.calls == CODES[:3]
        async with database.maker() as reader:
            caches = list((await reader.scalars(select(BookMarketCache))).all())
            for cache in caches:
                assert cache.lowest_delivered_price_cents == 1200
                assert cache.source_history['price_cents'] == 921
                assert cache.last_successful_collection_at.replace(tzinfo=UTC) == old
                assert cache.historical_checked_at.replace(tzinfo=UTC) == old

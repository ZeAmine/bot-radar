import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core import pilot_mode
from app.core.config import settings
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
from app.providers.registry import ProviderRegistry
from app.services.book_market_pricing import price_book_now
from app.services.market_backfill import manifest_digest
from tests.database import postgres_replayed_database

CODES = ['9782756079332', '9782205049657', '9782205051995', '9782205059250',
         '9782808500487', '9782413091424', '9791026856405']


def enable(tmp_path, monkeypatch):
    state = tmp_path/'pilot.json'
    monkeypatch.setattr(settings, 'CHASSE_PILOT_STATE_PATH', str(state))
    manifest = {'version': 1, 'scope': 'pilot_only', 'collection_isbn13': CODES,
                'maximum_unique_collections': 7, 'books': [], 'alerts': [],
                'catalogue_repairs': [], 'isbn_aliases': []}
    manifest['digest'] = manifest_digest(manifest)
    pilot_mode.enable(state, manifest, manifest['digest'])
    return state


class Source:
    name = 'chasse_aux_livres'

    def __init__(self, *, failing=False):
        self.calls = []
        self.failing = failing

    async def get_offers(self, *, isbn13, **_kwargs):
        pilot_mode.require_source(isbn13)
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
    alert = Alert(user_id=user.id, category_id=category.id, name='Pilote',
                  include_keywords=['pilote'])
    session.add(alert)
    await session.flush()
    books = [Book(title=f'Pilote {i}', normalized_title=f'pilote {i}', isbn13=code)
             for i, code in enumerate(CODES)]
    session.add_all(books)
    await session.flush()
    session.add_all([AlertBook(alert_id=alert.id, book_id=book.id) for book in books])
    await session.commit()
    return books


async def test_pilot_collects_seven_editions_once_and_preserves_scope_after_restart(
    tmp_path, monkeypatch,
):
    import asyncio

    state = enable(tmp_path, monkeypatch)
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
        assert len(pilot_mode.read_state()['attempts']) == 7
        assert pilot_mode.active()
        assert not pilot_mode.read_state()['halted']
        assert state.stat().st_mode & 0o077 == 0
        for book in books:
            await price_book_now(book.id, reason='backfill', registry=registry)
        assert source.calls == CODES
        async with database.maker() as reader:
            caches = list((await reader.scalars(select(BookMarketCache))).all())
            assert len(caches) == 7
            assert all(cache.source_history['price_cents'] == 921 for cache in caches)
            assert all(cache.next_refresh_at is not None for cache in caches)


async def test_pilot_repeated_failure_preserves_prices_and_stops_before_third_book(
    tmp_path, monkeypatch,
):
    enable(tmp_path, monkeypatch)
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
        assert source.calls == CODES[:2]
        assert pilot_mode.read_state()['halted']
        async with database.maker() as reader:
            caches = list((await reader.scalars(select(BookMarketCache))).all())
            for cache in caches:
                assert cache.lowest_delivered_price_cents == 1200
                assert cache.source_history['price_cents'] == 921
                assert cache.last_successful_collection_at.replace(tzinfo=UTC) == old
                assert cache.historical_checked_at.replace(tzinfo=UTC) == old


async def test_pilot_outside_edition_has_no_database_mutation_or_source_call(tmp_path, monkeypatch):
    enable(tmp_path, monkeypatch)
    source = Source()
    async with postgres_replayed_database() as database:
        book = Book(id=uuid.uuid4(), title='Standard hors pilote', normalized_title='standard',
                    isbn13='9782205055641')
        database.session.add(book)
        await database.session.commit()
        monkeypatch.setattr('app.services.book_market_pricing.AsyncSessionLocal', database.maker)
        result = await price_book_now(book.id, reason='backfill',
                                      registry=ProviderRegistry(providers=[source]))
        assert result is None
        assert source.calls == []
        async with database.maker() as reader:
            assert list((await reader.scalars(select(BookMarketCache))).all()) == []


async def test_manual_recovery_keeps_prices_and_resumes_only_the_failed_edition(
    tmp_path, monkeypatch,
):
    from argparse import Namespace

    from scripts.chasse_pilot import resume_pilot

    state = enable(tmp_path, monkeypatch)
    old = datetime.now(UTC) - timedelta(days=8)
    class RestrictedSource(Source):
        async def get_offers(self, *, isbn13, **kwargs):
            if self.failing:
                pilot_mode.require_source(isbn13)
                self.calls.append(isbn13)
                return MarketOffers(failed_providers=(self.name,), attempts=(
                    CollectionAttempt(self.name, "current", "failed", "bot_challenge",
                                      200, stage="search_results"),
                ))
            return await super().get_offers(isbn13=isbn13, **kwargs)

    source = RestrictedSource(failing=True)
    registry = ProviderRegistry(providers=[source])
    async with postgres_replayed_database() as database:
        books = await seed(database.session)
        database.session.add(BookMarketCache(book_id=books[0].id,
            lowest_delivered_price_cents=1200, last_successful_collection_at=old,
            historical_checked_at=old, source_history={"price_cents": 921,
            "condition": "Très bon état +", "days": 365, "shipping_included": True,
            "collected_at": old.isoformat()}))
        await database.session.commit()
        monkeypatch.setattr("app.services.book_market_pricing.AsyncSessionLocal", database.maker)
        await price_book_now(books[0].id, reason="backfill", registry=registry)
        before = pilot_mode.read_state()["attempts"][CODES[0]]
        assert pilot_mode.read_state()["halted"]
        assert before["diagnostics"][0]["stage"] == "search_results"
        async with database.maker() as reader:
            cache = await reader.scalar(select(BookMarketCache))
            assert cache.lowest_delivered_price_cents == 1200
            assert cache.source_history["price_cents"] == 921
            assert cache.last_successful_collection_at.replace(tzinfo=UTC) == old
            deadline = cache.next_refresh_at.replace(tzinfo=UTC)
        future = deadline + timedelta(seconds=1)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return future.astimezone(tz) if tz else future.replace(tzinfo=None)
        monkeypatch.setattr(pilot_mode, "datetime", Clock)
        monkeypatch.setattr("app.services.book_market_pricing.datetime", Clock)
        monkeypatch.setattr("app.services.market_refresh_policy.datetime", Clock)
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", database.maker)
        await resume_pilot(Namespace(state=state, approved_digest=pilot_mode.read_state()["digest"],
                                    isbn=CODES[0], authorize_pilot_retry=True))
        source.failing = False
        await price_book_now(books[0].id, reason="backfill", registry=registry)
        await price_book_now(books[0].id, reason="backfill", registry=registry)
        await price_book_now(books[1].id, reason="backfill", registry=registry)
        assert source.calls == [CODES[0], CODES[0], CODES[1]]
        assert pilot_mode.read_state()["attempts"][CODES[0]]["previous_attempts"] == [before]
        assert not pilot_mode.read_state()["halted"]
        assert pilot_mode.active()

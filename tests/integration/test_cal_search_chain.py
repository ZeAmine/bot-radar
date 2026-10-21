import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import select

from app.core.constants import ComparisonStatus
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.repositories.book_repo import BookRepository
from app.providers.chasse_aux_livres import (
    LOOKUP_POLL_DELAY_SECONDS,
    LOOKUP_POLL_MAX,
    ChasseAuxLivresProvider,
)
from app.providers.registry import ProviderRegistry
from app.services.book_market_pricing import price_book_now
from app.services.market_price_service import MarketPriceService
from app.services.market_refresh_policy import aware
from tests.database import postgres_replayed_database
from tests.integration.test_market_pricing_continuity import seed

URL = ('https://www.chasse-aux-livres.fr/prix/2756079332/'
       'jean-doux-et-le-mystere-de-la-disquette-molle-philippe-valette-9782756079332')


async def test_partial_source_stores_observations_and_history_without_refreshing_retained_offers(
    monkeypatch,
):
    from app.providers import chasse_aux_livres
    from app.services import book_market_pricing

    history = json.loads((Path(__file__).parents[1] / 'fixtures/chasse/'
                          'jean_doux_history_2026_10_18.json').read_text())
    monkeypatch.setattr(chasse_aux_livres, 'resolve_session_token', AsyncMock(return_value='test'))
    monkeypatch.setattr(chasse_aux_livres.chasse_session, 'get_history',
                        AsyncMock(return_value=httpx.Response(200, json=history)))
    requests = []
    failed = False

    def response(request):
        requests.append(request.url.path)
        assert request.url.path != '/search'
        assert request.url.path != '/rest/search-results'
        if failed:
            return httpx.Response(429, headers={'Retry-After': '86400'})
        if request.url.path.startswith('/prix/'):
            return httpx.Response(200, text=(
                '<div data-asin="2756079332" data-lvs="fixture" '
                'data-nbeng="22" data-didh="0123456789abcdef"></div>'
            ))
        return httpx.Response(200, json={'offers': {'22': [{
            'logo': {'name': 'MOMOX_SHOP'}, 'condition': {'_name': 'USED'},
            'subCondition': {'name': 'Très bon'}, 'price': {'amount': 1200},
            'shippingCost': {'amount': 250}, 'totalPrice': {'amount': 1450},
            'affiliateUrl': 'https://merchant.example/offer',
        }]}})

    async with postgres_replayed_database() as database:
        books = await seed(database.session)
        book = books[0]
        old = datetime.now(UTC) - timedelta(days=8)
        database.session.add(BookMarketCache(
            book_id=book.id, historical_checked_at=old,
            lowest_delivered_price_cents=1700, comparison_status=ComparisonStatus.AVAILABLE,
            checked_at=old, last_successful_collection_at=old,
            current_offers=[{'provider_name': 'chasse_aux_livres', 'item_price_cents': 1450,
                             'shipping_price_cents': 250, 'delivered_price_cents': 1700,
                             'source_url': URL, 'item_condition': 'Très bon',
                             'is_available': True, 'price_includes_shipping': True}],
            source_history={'price_cents': 1000, 'source_url': URL,
                            'condition': 'Très bon état +', 'days': 365,
                            'shipping_included': True, 'collected_at': old.isoformat()},
        ))
        await database.session.commit()
        monkeypatch.setattr(book_market_pricing, 'AsyncSessionLocal', database.maker)
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            registry = ProviderRegistry(providers=[ChasseAuxLivresProvider(
                http_client=client, max_retries=0, sleep=AsyncMock(),
            )])
            await price_book_now(book.id, reason='alert_created', registry=registry)
            async with database.maker() as reader:
                stored = await BookRepository(reader).get_by_id(book.id)
                cache = stored.market_cache
                assert cache.lowest_delivered_price_cents == 1700
                assert cache.source_history['price_cents'] == 936
                assert cache.source_history['source_url'] == URL
                assert cache.source_history['days'] == 365
                assert cache.source_history['shipping_included'] is True
                assert cache.current_offers[0]['shipping_price_cents'] == 250
                assert cache.current_offers[0]['item_condition'] == 'Très bon'
                assert aware(cache.last_successful_collection_at) == old
                assert aware(cache.checked_at) == old
                assert cache.collection_status == 'partial'
                snapshots = (await reader.scalars(select(MarketPriceSnapshot).where(
                    MarketPriceSnapshot.book_id == book.id,
                    MarketPriceSnapshot.is_historical.is_(False),
                ))).all()
                assert len(snapshots) == 1
                assert snapshots[0].delivered_price_cents == 1450
                assert snapshots[0].raw_payload['collection_status'] == 'partial'
                assert snapshots[0].raw_payload['partners']['Momox']['total_cents'] == 1450
                assert cache.historical_checked_at is not None
                assert cache.next_refresh_at is not None
                creation = MarketPriceService.get_comparison_summary(None, stored,
                                                                    for_creation=True)
                panel = MarketPriceService.format_market_details_card(None, stored)
                assert '9,36 €' in creation
                assert '9,36 €' in panel
                assert '17,00 €' in creation
                assert '17,00 €' in panel
                before = (cache.lowest_delivered_price_cents, cache.source_history,
                          cache.current_offers, cache.last_successful_collection_at,
                          cache.historical_checked_at)
            later = datetime.now(UTC) + timedelta(days=8)

            class Later(datetime):
                @classmethod
                def now(cls, tz=None):
                    return later if tz else later.replace(tzinfo=None)

            monkeypatch.setattr(book_market_pricing, 'datetime', Later)
            failed = True
            await price_book_now(book.id, reason='scheduled', registry=registry)
            async with database.maker() as reader:
                cache = (await reader.scalars(select(BookMarketCache).where(
                    BookMarketCache.book_id == book.id,
                ))).one()
                assert (cache.lowest_delivered_price_cents, cache.source_history,
                        cache.current_offers, cache.last_successful_collection_at,
                        cache.historical_checked_at) == before
                assert cache.collection_status == 'failed'
                assert aware(cache.last_attempt_at) >= later
        assert requests == [URL.removeprefix('https://www.chasse-aux-livres.fr')] + [
            '/rest/lookup/results'] * (1 + LOOKUP_POLL_MAX) + [
            URL.removeprefix('https://www.chasse-aux-livres.fr')]


async def test_initial_zero_eligible_is_followed_by_the_reserved_batch_without_date_invention():
    async with postgres_replayed_database() as database:
        books = await seed(database.session)
        repo = BookRepository(database.session)
        first = datetime(2026, 10, 19, 16, 9, tzinfo=UTC)
        await repo.plan_price_refreshes(now=first, capacity=3)
        assert await repo.get_books_needing_price_refresh(now=first) == []
        scheduled = await repo.get_books_for_active_bd_alerts()
        assert all(book.market_cache.next_refresh_at is not None for book in scheduled)
        next_run = datetime(2026, 10, 19, 16, 15, tzinfo=UTC) + timedelta(microseconds=1)
        await repo.plan_price_refreshes(now=next_run, capacity=3)
        selected = await repo.get_books_needing_price_refresh(limit=3, now=next_run)
        assert len(selected) == 3
        assert {book.id for book in selected} <= {book.id for book in books}
        assert all(book.market_cache.last_successful_collection_at is None for book in scheduled)
        assert all(book.market_cache.historical_checked_at is None for book in scheduled)
        assert len({book.market_cache.next_refresh_at for book in scheduled}) >= 3


async def test_empty_search_recovers_once_and_stores_history_independently_of_partial_offers(
    monkeypatch,
):
    from app.providers import chasse_aux_livres
    from app.providers.base import extract_best_historical_low
    from app.services.book_market_pricing import BookPricingResult, persist_market_result

    fixture = Path(__file__).parents[1] / 'fixtures/chasse'
    history = json.loads((fixture / 'jean_doux_history_2026_10_18.json').read_text())
    partial = json.loads((fixture / 'partial_offers_2026_10_19.json').read_text())['payload']
    monkeypatch.setattr(chasse_aux_livres, 'resolve_session_token',
                        AsyncMock(return_value='synthetic-session'))
    monkeypatch.setattr(chasse_aux_livres.chasse_session, 'get_history',
                        AsyncMock(return_value=httpx.Response(200, json=history)))
    requests = []
    searches = 0

    def respond(request):
        nonlocal searches
        requests.append(request.url.path)
        if request.url.path == '/search':
            return httpx.Response(200, text='<div data-hash="synthetic0123456789"></div>')
        if request.url.path == '/rest/search-results':
            searches += 1
            if searches == 1:
                return httpx.Response(200, content=b'', headers={'content-type': 'text/plain'})
            return httpx.Response(200, json={'d': '<a href="' + URL.split('.fr')[1]
                                            + '">Jean Doux</a>'})
        if request.url.path.startswith('/prix/'):
            return httpx.Response(200, text=(
                '<div data-asin="2756079332" data-lvs="synthetic" '
                'data-nbeng="25" data-didh="abc123"></div>'
            ))
        return httpx.Response(200, json=partial)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = ChasseAuxLivresProvider(http_client=client, max_retries=2)
        provider._sleep = AsyncMock()
        offers = await ProviderRegistry(providers=[provider]).fetch_all_offers(
            isbn13='9782756079332', title='Jean Doux',
        )
        provider._sleep.assert_awaited()
        delays = [call.args[0] for call in provider._sleep.await_args_list]
        assert delays[0] == 1.5
        assert delays[1:] == [LOOKUP_POLL_DELAY_SECONDS] * LOOKUP_POLL_MAX
    assert requests == ['/search', '/rest/search-results', '/search', '/rest/search-results',
                        URL.split('.fr')[1]] + ['/rest/lookup/results'] * (1 + LOOKUP_POLL_MAX)
    assert offers.attempts[0].status == 'partial'
    assert offers.attempts[1].status == 'success'
    result = BookPricingResult.from_offers(
        offers, ProviderRegistry.select_best_offer(offers), extract_best_historical_low(offers),
    )
    async with postgres_replayed_database() as database:
        books = await seed(database.session)
        book = books[0]
        await persist_market_result(database.session, book.id, result)
        await database.session.commit()
        async with database.maker() as reader:
            stored = await BookRepository(reader).get_by_id(book.id)
            cache = stored.market_cache
            assert cache.lowest_delivered_price_cents is None
            assert cache.last_successful_collection_at is None
            assert cache.checked_at is None
            assert cache.source_history['price_cents'] == 936
            assert cache.historical_checked_at is not None
            assert cache.refresh_state['current']['failures'] == 1
            assert cache.refresh_state['history']['failures'] == 0
            panel = MarketPriceService.format_market_details_card(None, stored)
            creation = MarketPriceService.get_comparison_summary(None, stored, for_creation=True)
            assert '9,36 €' in panel
            assert '9,36 €' in creation
            assert 'Dernière collecte échouée' not in panel
            assert 'Actualisation impossible' not in panel

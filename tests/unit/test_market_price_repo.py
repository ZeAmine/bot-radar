"""Tests unitaires pour MarketPriceRepository :
Vérification de l'intégrité de l'historique bas et non-contamination par les prix en direct.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book_market_cache import BookMarketCache
from app.db.repositories.market_price_repo import MarketPriceRepository


@pytest.mark.asyncio
async def test_update_or_create_cache_never_uses_live_price_as_historical_low():
    """Vérifie qu'un prix en direct (ex: 21,89 €) n'est JAMAIS utilisé comme historique bas."""
    session = AsyncMock()
    session.add = MagicMock()
    repo = MarketPriceRepository(session)

    # Aucun cache existant, aucun relevé pour l'attester.
    repo.get_cache_by_book_id = AsyncMock(return_value=None)

    book_id = uuid.uuid4()
    cache = await repo.update_or_create_cache(
        book_id=book_id,
        lowest_delivered_price_cents=2189,
        best_provider_name="Momox",
        best_source_url="https://example.com",
        confidence=MatchConfidence.HIGH,
        comparison_status=ComparisonStatus.AVAILABLE,
        historical_low_1y_cents=None,  # Le fournisseur n'a pas fourni d'historique
    )

    assert cache.lowest_delivered_price_cents == 2189
    assert cache.historical_low_1y_cents is None
    assert cache.historical_low_at is None


@pytest.mark.asyncio
async def test_update_or_create_cache_preserves_existing_certified_historical_low():
    """Vérifie qu'un historique bas certifié préexistant (6,70 €)
    n'est pas écrasé par un prix en direct plus élevé."""
    session = AsyncMock()
    repo = MarketPriceRepository(session)

    book_id = uuid.uuid4()
    existing_cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book_id,
        lowest_delivered_price_cents=2000,
        historical_low_1y_cents=670,  # 6,70 € certifié
        historical_low_at=datetime.now(UTC),
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    repo.get_cache_by_book_id = AsyncMock(return_value=existing_cache)

    cache = await repo.update_or_create_cache(
        book_id=book_id,
        lowest_delivered_price_cents=2189,
        best_provider_name="Momox",
        best_source_url="https://example.com",
        confidence=MatchConfidence.HIGH,
        comparison_status=ComparisonStatus.AVAILABLE,
        historical_low_1y_cents=None,
    )

    assert cache.lowest_delivered_price_cents == 2189
    assert cache.historical_low_1y_cents == 670


@pytest.mark.asyncio
async def test_new_undated_aggregate_preserves_legacy_value():
    """Un nouvel agrégat non daté ne réécrit pas une ancienne valeur conservée."""
    session = AsyncMock()
    repo = MarketPriceRepository(session)

    book_id = uuid.uuid4()
    existing_cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book_id,
        lowest_delivered_price_cents=2000,
        historical_low_1y_cents=670,
        historical_low_at=datetime.now(UTC),
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    repo.get_cache_by_book_id = AsyncMock(return_value=existing_cache)

    cache = await repo.update_or_create_cache(
        book_id=book_id,
        lowest_delivered_price_cents=1500,
        best_provider_name="Rakuten",
        best_source_url="https://example.com",
        confidence=MatchConfidence.HIGH,
        comparison_status=ComparisonStatus.AVAILABLE,
        historical_low_1y_cents=450,  # Nouveau plus bas certifié
    )

    assert cache.historical_low_1y_cents == 670
    assert cache.annual_low_cents is None


def test_undated_aggregate_is_not_an_annual_reference():
    from app.services.market_price_service import reference_price
    cache = BookMarketCache(historical_low_1y_cents=670)
    assert reference_price(cache) == (None, False)


@pytest.mark.asyncio
async def test_legacy_value_is_not_rewritten_by_a_recomputation():
    """La projection nouvelle ne réécrit ni montant ni date de l’ancien minimum."""
    session = AsyncMock()
    repo = MarketPriceRepository(session)

    book_id = uuid.uuid4()
    existing_cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book_id,
        lowest_delivered_price_cents=2063,
        historical_low_1y_cents=2290,  # 22,90 € certifié par l'agrégateur
        historical_low_at=datetime.now(UTC),
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    repo.get_cache_by_book_id = AsyncMock(return_value=existing_cache)

    cache = await repo.update_or_create_cache(
        book_id=book_id,
        lowest_delivered_price_cents=2063,
        best_provider_name="Momox",
        best_source_url="https://example.com",
        confidence=MatchConfidence.HIGH,
        comparison_status=ComparisonStatus.AVAILABLE,
        historical_low_1y_cents=2290,
    )

    assert cache.historical_low_1y_cents == 2290
    assert cache.annual_low_cents is None


@pytest.mark.asyncio
async def test_update_or_create_cache_never_raises_the_low_above_observations():
    """Un releve plus cher que la cote ne peut pas l'éclever : le plancher est un minimum."""
    session = AsyncMock()
    repo = MarketPriceRepository(session)

    book_id = uuid.uuid4()
    existing_cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book_id,
        lowest_delivered_price_cents=900,
        historical_low_1y_cents=670,
        historical_low_at=datetime.now(UTC),
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    repo.get_cache_by_book_id = AsyncMock(return_value=existing_cache)

    cache = await repo.update_or_create_cache(
        book_id=book_id,
        lowest_delivered_price_cents=2189,
        best_provider_name="Momox",
        best_source_url="https://example.com",
        confidence=MatchConfidence.HIGH,
        comparison_status=ComparisonStatus.AVAILABLE,
        historical_low_1y_cents=None,
    )

    assert cache.historical_low_1y_cents == 670


@pytest.mark.asyncio
async def test_update_or_create_cache_does_not_invent_a_low_from_observations_only():
    """Sans cote preexistante, nos releves ne creent pas de reference.

    Le plancher corrige une cote fausse, il n'en fabrique pas : sinon chaque livre
    deja releve bascule en « prix bas constate » pour son seul prix du jour.
    """
    session = AsyncMock()
    session.add = MagicMock()
    repo = MarketPriceRepository(session)

    book_id = uuid.uuid4()
    repo.get_cache_by_book_id = AsyncMock(return_value=None)

    cache = await repo.update_or_create_cache(
        book_id=book_id,
        lowest_delivered_price_cents=1311,
        best_provider_name="Momox",
        best_source_url="https://example.com",
        confidence=MatchConfidence.HIGH,
        comparison_status=ComparisonStatus.AVAILABLE,
        historical_low_1y_cents=None,
    )

    assert cache.historical_low_1y_cents is None


@pytest.mark.asyncio
async def test_normalized_observations_replace_the_unbounded_minimum():
    from datetime import timedelta

    from app.db.models.book import Book
    from app.providers.base import MarketOffer
    from tests.database import sqlite_only_database
    async with sqlite_only_database() as database:
        book = Book(title="Preuve", normalized_title="preuve")
        database.session.add(book)
        await database.session.flush()
        cache = BookMarketCache(book_id=book.id, historical_low_1y_cents=1)
        database.session.add(cache)
        await database.session.flush()
        repo = MarketPriceRepository(database.session)
        now = datetime.now(UTC)
        for days, price in [(400, 100), (80, 1357), (0, 2000)]:
            await repo.save_snapshot(book.id, MarketOffer(
                provider_name="synthetic", item_price_cents=price, shipping_price_cents=0,
                delivered_price_cents=price, source_url="https://example.invalid",
                item_condition="Très bon", observed_at=now-timedelta(days=days),
            ))
        await repo.refresh_annual_references([book.id], now=now, force=True)
        assert cache.annual_low_cents == 1357
        assert cache.annual_count == 2
        assert cache.historical_low_1y_cents == 1

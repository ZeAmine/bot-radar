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

    # Simuler qu'aucun cache n'existe encore
    repo.get_cache_by_book_id = AsyncMock(return_value=None)
    repo.get_historical_low_1y = AsyncMock(return_value=None)

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

    # L'historique bas DOIT être None, et non pas 2189
    assert cache.lowest_delivered_price_cents == 2189
    assert cache.historical_low_1y_cents is None
    assert cache.historical_low_at is None


@pytest.mark.asyncio
async def test_update_or_create_cache_preserves_existing_certified_historical_low():
    """Vérifie qu'un historique bas certifié préexistant (6,70 €) n'est pas écrasé par un prix en direct plus élevé."""
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

    # Nouvelle offre en direct à 21,89 € sans historique fourni
    cache = await repo.update_or_create_cache(
        book_id=book_id,
        lowest_delivered_price_cents=2189,
        best_provider_name="Momox",
        best_source_url="https://example.com",
        confidence=MatchConfidence.HIGH,
        comparison_status=ComparisonStatus.AVAILABLE,
        historical_low_1y_cents=None,
    )

    # Le prix en direct est mis à jour à 21,89 €, mais l'historique bas reste strictement à 6,70 €
    assert cache.lowest_delivered_price_cents == 2189
    assert cache.historical_low_1y_cents == 670


@pytest.mark.asyncio
async def test_update_or_create_cache_updates_to_lower_certified_historical_low():
    """Vérifie que si le fournisseur fournit un historique encore plus bas, il est mis à jour."""
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

    assert cache.historical_low_1y_cents == 450


@pytest.mark.asyncio
async def test_get_historical_low_1y_reads_from_raw_payload():
    """Vérifie que get_historical_low_1y extrait les cotes certifiées depuis raw_payload."""
    session = AsyncMock()
    repo = MarketPriceRepository(session)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [
        {"historical_low_1y_cents": 850, "other": "data"},
        {"historical_low_1y_cents": 670, "other": "data"},
        {"delivered_price_cents": 2189},  # Sans historical_low_1y_cents
        None,
    ]
    session.execute = AsyncMock(return_value=mock_result)

    hist_low = await repo.get_historical_low_1y(uuid.uuid4())
    assert hist_low == 670

"""Tests unitaires pour la tâche planifiée de rafraîchissement des prix BD."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.providers.base import MarketOffer
from app.tasks.refresh_book_prices import refresh_book_prices_task


@pytest.mark.asyncio
async def test_refresh_book_prices_task_success():
    """Vérifie l'exécution de la tâche de rafraîchissement avec mise à jour du cache."""
    mock_book = Book(
        id=uuid.uuid4(),
        title="Blacksad — Intégrale",
        normalized_title="blacksad integrale",
        isbn13="9782205078054",
    )

    mock_offer = MarketOffer(
        provider_name="ebay",
        item_price_cents=1500,
        shipping_price_cents=450,
        delivered_price_cents=1950,
        source_url="https://ebay.fr/123",
        confidence=MatchConfidence.HIGH,
    )

    mock_registry = MagicMock()
    mock_registry.fetch_all_offers = AsyncMock(return_value=[mock_offer])
    mock_registry.select_best_offer = MagicMock(return_value=mock_offer)

    mock_session = AsyncMock()

    with pytest.MonkeyPatch.context() as mp:
        mock_book_repo = MagicMock()
        mock_book_repo.get_books_for_active_bd_alerts = AsyncMock(return_value=[mock_book])

        mock_market_repo = MagicMock()
        mock_market_repo.save_snapshot = AsyncMock()
        mock_market_repo.update_or_create_cache = AsyncMock()

        mp.setattr("app.tasks.refresh_book_prices.BookRepository", lambda _s: mock_book_repo)
        mp.setattr(
            "app.tasks.refresh_book_prices.MarketPriceRepository", lambda _s: mock_market_repo
        )

        stats = await refresh_book_prices_task(registry=mock_registry, session=mock_session)

        assert stats["processed"] == 1
        assert stats["available"] == 1
        assert stats["errors"] == 0

        mock_market_repo.save_snapshot.assert_called_once_with(mock_book.id, mock_offer)
        mock_market_repo.update_or_create_cache.assert_called_once_with(
            book_id=mock_book.id,
            lowest_delivered_price_cents=1950,
            best_provider_name="ebay",
            best_source_url="https://ebay.fr/123",
            confidence=MatchConfidence.HIGH,
            comparison_status=ComparisonStatus.AVAILABLE,
            cache_duration_hours=24,
            historical_low_1y_cents=None,
        )


@pytest.mark.asyncio
async def test_refresh_book_prices_task_incremental():
    """Vérifie que le mode incrémental interroge uniquement le micro-lot prioritaire."""
    mock_book = Book(
        id=uuid.uuid4(),
        title="1629",
        normalized_title="1629",
        isbn13="9782344045084",
    )
    mock_registry = MagicMock()
    mock_registry.fetch_all_offers = AsyncMock(return_value=[])
    mock_registry.select_best_offer = MagicMock(return_value=None)
    mock_session = AsyncMock()

    with pytest.MonkeyPatch.context() as mp:
        mock_book_repo = MagicMock()
        mock_book_repo.get_books_needing_price_refresh = AsyncMock(return_value=[mock_book])
        mock_book_repo.get_books_for_active_bd_alerts = AsyncMock(return_value=[])

        mock_market_repo = MagicMock()
        mock_market_repo.save_snapshot = AsyncMock()
        mock_market_repo.update_or_create_cache = AsyncMock()

        mp.setattr("app.tasks.refresh_book_prices.BookRepository", lambda _s: mock_book_repo)
        mp.setattr(
            "app.tasks.refresh_book_prices.MarketPriceRepository", lambda _s: mock_market_repo
        )

        stats = await refresh_book_prices_task(
            registry=mock_registry, session=mock_session, incremental=True, batch_size=5
        )

        assert stats["processed"] == 1
        mock_book_repo.get_books_needing_price_refresh.assert_called_once_with(limit=5)
        mock_book_repo.get_books_for_active_bd_alerts.assert_not_called()


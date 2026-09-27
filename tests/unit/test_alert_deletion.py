"""Tests unitaires pour la suppression définitive d'une alerte et le nettoyage des orphelins."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.repositories.alert_repo import AlertRepository


@pytest.mark.asyncio
async def test_delete_completely_cleans_alert_and_orphans():
    """Vérifie que delete_completely supprime l'alerte et nettoie les livres orphelins."""
    session = AsyncMock()
    repo = AlertRepository(session)

    alert_id = uuid.uuid4()
    book_id = uuid.uuid4()

    mock_alert = Alert(
        id=alert_id,
        user_id=uuid.uuid4(),
        name="Spider-Man — Kraven",
        isbn="9791039147125",
        status=AlertStatus.ACTIVE,
    )
    mock_book = Book(
        id=book_id,
        title="Spider-Man — Kraven",
        normalized_title="spider man kraven",
    )

    # Mock de la requête des livres liés
    mock_books_res = MagicMock()
    mock_books_res.scalars.return_value.all.return_value = [book_id]

    # Mock du count d'autres alertes (0 autre alerte -> orphelin)
    mock_count_res = MagicMock()
    mock_count_res.scalar_one.return_value = 0

    session.execute.side_effect = [mock_books_res, mock_count_res]
    session.get.return_value = mock_book

    result = await repo.delete_completely(mock_alert)

    assert result["alert_id"] == alert_id
    assert result["deleted_books_count"] == 1
    session.delete.assert_any_call(mock_alert)
    session.delete.assert_any_call(mock_book)


@pytest.mark.asyncio
async def test_delete_completely_preserves_shared_book():
    """Vérifie que delete_completely conserve le livre
    s'il est encore référencé par une autre alerte."""
    session = AsyncMock()
    repo = AlertRepository(session)

    alert_id = uuid.uuid4()
    book_id = uuid.uuid4()

    mock_alert = Alert(
        id=alert_id,
        user_id=uuid.uuid4(),
        name="Spider-Man",
        status=AlertStatus.ACTIVE,
    )

    mock_books_res = MagicMock()
    mock_books_res.scalars.return_value.all.return_value = [book_id]

    # 1 autre alerte référence encore ce livre
    mock_count_res = MagicMock()
    mock_count_res.scalar_one.return_value = 1

    session.execute.side_effect = [mock_books_res, mock_count_res]

    result = await repo.delete_completely(mock_alert)

    assert result["deleted_books_count"] == 0
    session.delete.assert_called_once_with(mock_alert)

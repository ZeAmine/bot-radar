"""Tests unitaires pour le garde anti-doublon des alertes."""

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.repositories.alert_repo import AlertRepository


@pytest.mark.asyncio
async def test_find_existing_alert():
    """Vérifie que find_existing détecte une alerte existante par ISBN ou par titre."""
    session = AsyncMock()
    repo = AlertRepository(session)

    user_id = uuid.uuid4()
    mock_alert = Alert(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Deathbringer",
        isbn="9782413078128",
        status=AlertStatus.ACTIVE,
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_alert
    session.execute.return_value = mock_result

    # Recherche par ISBN
    found_by_isbn = await repo.find_existing(user_id, isbn="9782413078128")
    assert found_by_isbn is not None
    assert found_by_isbn.name == "Deathbringer"

    # Recherche par titre
    found_by_title = await repo.find_existing(user_id, title="Deathbringer")
    assert found_by_title is not None
    assert found_by_title.isbn == "9782413078128"


@pytest.mark.asyncio
async def test_find_existing_alert_not_found():
    """Vérifie que find_existing retourne None si aucune alerte ne correspond."""
    session = AsyncMock()
    repo = AlertRepository(session)

    user_id = uuid.uuid4()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    res = await repo.find_existing(user_id, isbn="9780000000000", title="Inconnue")
    assert res is None

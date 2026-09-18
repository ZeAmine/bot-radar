"""Tests d'intégration pour le Webhook Telegram."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.core.config import settings


@pytest.mark.asyncio
async def test_webhook_invalid_secret(async_client: AsyncClient):
    """Vérifie qu'un secret incorrect retourne 403 Forbidden."""
    response = await async_client.post(
        "/api/v1/telegram/webhook/invalid_secret_token",
        json={"update_id": 12345},
    )
    assert response.status_code == 403
    assert "invalide" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_webhook_valid_secret(async_client: AsyncClient):
    """Vérifie qu'un secret valide transmet l'update au dispatcher."""
    valid_secret = settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()

    update_payload = {
        "update_id": 10001,
        "message": {
            "message_id": 1,
            "date": 1725900000,
            "chat": {"id": 12345678, "type": "private"},
            "from": {"id": 12345678, "is_bot": False, "first_name": "TestUser"},
            "text": "/help",
        },
    }

    with patch("app.api.routes.telegram.dp.feed_update", new_callable=AsyncMock) as mock_feed:
        mock_feed.return_value = True
        response = await async_client.post(
            f"/api/v1/telegram/webhook/{valid_secret}",
            json=update_payload,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        mock_feed.assert_called_once()

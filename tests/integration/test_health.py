"""Tests d'intégration pour le endpoint de santé /health."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_endpoint(async_client: AsyncClient):
    """Vérifie que /health répond 200 avec status ok et un header X-Request-ID."""
    response = await async_client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert "environment" in data
    assert "ai" in data
    assert data["ai"]["status"] in ("active", "unconfigured", "disabled", "error")
    assert "X-Request-ID" in response.headers

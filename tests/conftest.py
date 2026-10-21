"""Configuration et fixtures pytest."""

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture(autouse=True)
def forbid_real_telegram(monkeypatch):
    """Tout envoi Telegram doit être simulé, même avec un token local configuré."""
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Telegram réel interdit pendant les tests")

    monkeypatch.setattr(AiohttpSession, "make_request", forbidden)


@pytest.fixture
async def async_client():
    """Client HTTP asynchrone pour tester l'API FastAPI."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.fixture(autouse=True)
def isolate_notification_windows(monkeypatch):
    """La fenêtre de repli locale ne doit jamais survivre au test qui l'a ouverte."""
    monkeypatch.setattr("app.services.market_health._notification_deadlines", {})
    monkeypatch.setattr("app.services.market_health._incident_states", {})
    monkeypatch.setattr("app.services.market_health._incident_observations", {})
    monkeypatch.setattr("app.services.market_health._incident_pending", {})
    monkeypatch.setattr("app.services.market_health._incident_loaded", False)


@pytest.fixture(autouse=True)
def isolate_cal_governor(monkeypatch):
    """Un coupe-circuit CAL ouvert ne doit jamais survivre au test qui l'a ouvert."""
    from app.providers.cal_governor import CalGovernor

    monkeypatch.setattr(
        "app.providers.chasse_aux_livres.cal_governor",
        CalGovernor(store=False, min_interval_seconds=0.0),
    )

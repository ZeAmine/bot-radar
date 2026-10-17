"""Tests d'intégration pour les routes internes GET /api/v1/internal/*."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.core.config import settings
from app.core.constants import AlertStatus, ComparisonStatus, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.main import app
from tests.database import postgres_replayed_database


@pytest.fixture
async def internal_test_db() -> AsyncGenerator[AsyncSession, None]:
    """Base de test peuplée (PostgreSQL si la CI la rejoue, sinon SQLite)."""
    async with postgres_replayed_database() as database:
        session = database.session
        user = User(telegram_user_id=12345, is_allowed=True)
        cat = Category(slug="bd_comics", label="BD", emoji="📚", is_active=True)
        session.add_all([user, cat])
        await session.flush()

        alert = Alert(
            user_id=user.id,
            category_id=cat.id,
            name="Alerte Tintin",
            include_keywords=["tintin"],
            status=AlertStatus.ACTIVE,
        )
        book = Book(
            title="Tintin au Tibet",
            normalized_title="tintin au tibet",
            isbn13="9782203001190",
        )
        session.add_all([alert, book])
        await session.flush()

        cache = BookMarketCache(
            book_id=book.id,
            lowest_delivered_price_cents=1200,
            best_provider_name="ebay",
            comparison_status=ComparisonStatus.AVAILABLE,
            confidence=MatchConfidence.HIGH,
        )
        session.add(cache)
        await session.commit()
        yield session


@pytest.fixture
async def internal_client(internal_test_db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield internal_test_db

    app.dependency_overrides[get_db] = override_get_db
    token = settings.INTERNAL_API_TOKEN.get_secret_value()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_internal_routes_unauthorized(async_client: AsyncClient):
    """Vérifie le rejet 401 si le token interne est manquant ou invalide."""
    res_no_auth = await async_client.get("/api/v1/internal/alerts")
    assert res_no_auth.status_code == 401 or res_no_auth.status_code == 403

    res_bad_auth = await async_client.get(
        "/api/v1/internal/alerts",
        headers={"Authorization": "Bearer bad_token"},
    )
    assert res_bad_auth.status_code == 401


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Un Redis en mémoire : la session injectée doit survivre au round-trip."""
    store: dict[str, str] = {}
    client = AsyncMock()
    client.set = AsyncMock(side_effect=lambda key, value, **_: store.update({key: value}) or True)
    client.get = AsyncMock(side_effect=lambda key: store.get(key))
    monkeypatch.setattr("app.api.routes.internal.get_redis_client", lambda: client)
    monkeypatch.setattr("app.providers.chasse_aux_livres.get_redis_client", lambda: client)
    return store


@pytest.mark.asyncio
async def test_chasse_session_never_echoes_the_token(
    internal_client: AsyncClient,
    fake_redis: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    """Le jeton va et vient : seule sa longueur sort, sinon il finit dans un log."""
    # Le test porte sur la voie Redis : l'environnement local ne doit pas décider du
    # résultat — `source` vaut « env » dès que le réglage porte un jeton.
    monkeypatch.setattr(settings, "CHASSE_AUX_LIVRES_DUIH", "")
    res = await internal_client.post(
        "/api/v1/internal/chasse/session",
        json={"duih": "jeton-ultra-secret-0123456789"},
    )
    assert res.status_code == 200
    assert "jeton-ultra-secret" not in res.text

    status = await internal_client.get("/api/v1/internal/chasse/session")
    assert status.status_code == 200
    assert "jeton-ultra-secret" not in status.text
    assert status.json()["has_token"] is True
    assert status.json()["token_length"] > 0
    assert status.json()["source"] == "redis"
    # Le round-trip est réel : c'est ce que relira le provider à la passe suivante.
    assert list(fake_redis.values()) == ["jeton-ultra-secret-0123456789"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_redis")
async def test_chasse_session_without_token_names_no_source(
    internal_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Aucun jeton se lit « aucune source », jamais « redis ».

    Dire d'où viendrait un jeton inexistant enverrait chercher une clé absente au
    lieu de dire qu'il faut capturer le jeton dans un navigateur.
    """
    # L'environnement local ne doit pas décider du résultat de ce test.
    monkeypatch.setattr(settings, "CHASSE_AUX_LIVRES_DUIH", "")

    res = await internal_client.get("/api/v1/internal/chasse/session")

    assert res.status_code == 200
    assert res.json() == {"has_token": False, "token_length": 0, "source": None}


@pytest.mark.asyncio
async def test_chasse_session_rejects_an_empty_token(internal_client: AsyncClient):
    """Un jeton vide s'écrirait, puis se lirait comme une session installée."""
    res = await internal_client.post("/api/v1/internal/chasse/session", json={"duih": "   "})

    assert res.status_code == 400


@pytest.mark.asyncio
async def test_chasse_session_requires_the_internal_token(async_client: AsyncClient):
    """L'injection d'une session ne peut pas être une porte ouverte."""
    res = await async_client.post(
        "/api/v1/internal/chasse/session", json={"duih": "quelconque"}
    )

    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_internal_alerts(internal_client: AsyncClient):
    """Vérifie que la route interne retourne la liste des alertes."""
    res = await internal_client.get("/api/v1/internal/alerts")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 1
    assert data[0]["name"] == "Alerte Tintin"
    assert data[0]["category"] == "bd_comics"


@pytest.mark.asyncio
async def test_get_book_market_price(internal_client: AsyncClient):
    """Vérifie la consultation du cache de prix pour un ISBN catalogué."""
    res = await internal_client.get("/api/v1/internal/books/9782203001190/market-price")
    assert res.status_code == 200
    data = res.json()
    assert data["isbn13"] == "9782203001190"
    assert data["lowest_delivered_price_cents"] == 1200
    assert data["best_provider_name"] == "ebay"
    assert data["comparison_status"] == "available"

    res_not_found = await internal_client.get("/api/v1/internal/books/0000000000000/market-price")
    assert res_not_found.status_code == 404

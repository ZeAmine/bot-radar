"""Tests d'intégration pour les routes internes GET /api/v1/internal/*."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.dependencies import get_db
from app.core.config import settings
from app.core.constants import AlertStatus, ComparisonStatus, MatchConfidence
from app.db.base import Base
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.main import app


@pytest.fixture
async def internal_test_db() -> AsyncGenerator[AsyncSession, None]:
    """Base SQLite en mémoire peuplée pour les tests des routes internes."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
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

    await engine.dispose()


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

    # Test avec un ISBN inexistant
    res_not_found = await internal_client.get("/api/v1/internal/books/0000000000000/market-price")
    assert res_not_found.status_code == 404

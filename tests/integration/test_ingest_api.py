"""Tests d'intégration pour l'endpoint d'ingestion POST /api/v1/ingest/listings."""

import os
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.dependencies import get_db
from app.core.config import settings
from app.db.base import Base
from app.db.models.category import Category
from app.main import app


@pytest.fixture
async def test_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Crée une base de test asynchrone (PostgreSQL si TEST_DATABASE_URL, sinon SQLite)."""
    test_engine = create_async_engine(
        os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:"),
        echo=False,
    )

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_maker() as session:
        # Insérer les catégories initiales de test
        cat_bd = Category(
            id=uuid.uuid4(),
            slug="bd_comics",
            label="BD & Comics",
            emoji="📚",
            is_active=True,
        )
        session.add(cat_bd)
        await session.commit()
        yield session

    await test_engine.dispose()


@pytest.fixture
async def authenticated_client(test_db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Client de test configuré avec injection de la DB de test et token valide."""

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield test_db_session

    app.dependency_overrides[get_db] = override_get_db

    token = settings.INGEST_API_TOKEN.get_secret_value()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_ingest_unauthorized(async_client: AsyncClient):
    """Vérifie le rejet 401 si le token est manquant ou invalide."""
    payload = {
        "source_name": "test-source",
        "external_id": "item-1",
        "url": "https://www.example.com/item-1",
        "title": "Blacksad",
        "category_slug": "bd_comics",
        "item_price_cents": 1000,
    }

    # Sans header Authorization
    res_no_auth = await async_client.post("/api/v1/ingest/listings", json=payload)
    assert res_no_auth.status_code == 403 or res_no_auth.status_code == 401

    # Avec un token invalide
    res_bad_auth = await async_client.post(
        "/api/v1/ingest/listings",
        json=payload,
        headers={"Authorization": "Bearer wrong_token"},
    )
    assert res_bad_auth.status_code == 401


@pytest.mark.asyncio
async def test_ingest_valid_listing_and_duplicate(authenticated_client: AsyncClient):
    """Vérifie l'ingestion d'une annonce, le calcul du total et la détection des doublons."""
    payload = {
        "source_name": "authorized-source",
        "external_id": "listing-abc-123",
        "url": "https://www.example.com/listing-123",
        "title": "Blacksad — Intégrale",
        "description": "Très bon état",
        "category_slug": "bd_comics",
        "item_price_cents": 900,
        "estimated_protection_cents": 70,
        "estimated_shipping_cents": 399,
        "condition": "Très bon état",
        "seller_name": "vendeur-test",
        "seller_rating": 4.9,
        "photos": ["https://cdn.example.com/cover.jpg"],
        "isbn13": "9782205078054",
    }

    # 1. Première ingestion
    response1 = await authenticated_client.post("/api/v1/ingest/listings", json=payload)
    assert response1.status_code == 200

    data1 = response1.json()
    assert data1["is_duplicate"] is False
    assert data1["estimated_total_cents"] == 1369  # 900 + 70 + 399
    assert "listing_id" in data1

    # 2. Deuxième ingestion (même source + external_id) -> Détection de doublon
    response2 = await authenticated_client.post("/api/v1/ingest/listings", json=payload)
    assert response2.status_code == 200

    data2 = response2.json()
    assert data2["is_duplicate"] is True
    assert data2["listing_id"] == data1["listing_id"]


@pytest.mark.asyncio
async def test_ingest_bd_listing_with_market_comparison(
    authenticated_client: AsyncClient,
    test_db_session: AsyncSession,
):
    """Vérifie que la comparaison de prix marché est bien calculée et envoyée pour les BDs."""
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from app.core.constants import AlertStatus, ComparisonStatus, MatchConfidence
    from app.db.models.alert import Alert
    from app.db.models.alert_book import AlertBook
    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.db.models.category import Category
    from app.db.models.user import User

    # Récupérer la catégorie BD
    stmt_cat = select(Category).where(Category.slug == "bd_comics")
    cat_bd = (await test_db_session.execute(stmt_cat)).scalar_one()

    # Créer un utilisateur avec Telegram ID
    user = User(
        id=uuid.uuid4(),
        telegram_user_id=987654321,
        username="bd_fan",
    )
    test_db_session.add(user)

    # Créer une alerte BD active
    alert = Alert(
        id=uuid.uuid4(),
        user_id=user.id,
        category_id=cat_bd.id,
        name="Alerte Blacksad",
        include_keywords=["blacksad"],
        max_item_price_cents=1500,
        accepted_conditions=["Très bon état"],
        status=AlertStatus.ACTIVE,
    )
    test_db_session.add(alert)

    # Créer le livre au catalogue avec son cache marché
    book = Book(
        id=uuid.uuid4(),
        isbn13="9782205078054",
        title="Blacksad — Intégrale",
        normalized_title="blacksad integrale",
    )
    now = datetime.now(UTC)
    cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book.id,
        lowest_delivered_price_cents=2200,  # 22,00 €
        best_provider_name="momox",
        best_source_url="https://momox-shop.fr/blacksad",
        confidence=MatchConfidence.HIGH,
        checked_at=now,
        expires_at=now + timedelta(days=1),
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    book.market_cache = cache
    test_db_session.add(book)
    await test_db_session.commit()

    payload = {
        "source_name": "vinted",
        "external_id": "item-blacksad-deal",
        "url": "https://www.vinted.fr/items/item-blacksad-deal",
        "title": "Blacksad — Intégrale",
        "category_slug": "bd_comics",
        "item_price_cents": 900,
        "estimated_protection_cents": 70,
        "estimated_shipping_cents": 399,
        "condition": "Très bon état",
        "isbn13": "9782205078054",
    }

    with patch(
        "app.api.routes.ingest.send_listing_alert", new_callable=AsyncMock
    ) as mock_send_alert:
        mock_send_alert.return_value = True

        response = await authenticated_client.post("/api/v1/ingest/listings", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert len(data["matches"]) == 1
        assert data["matches"][0]["notification_sent"] is True

        mock_send_alert.assert_called_once()
        _, kwargs = mock_send_alert.call_args
        comparison_line = kwargs.get("market_comparison_line")
        assert comparison_line is not None
        assert "22,00 €" in comparison_line
        assert "-8,31 €" in comparison_line

    # Vérifier que le livre est bien relié à l'alerte
    stmt_link = select(AlertBook).where(
        AlertBook.alert_id == alert.id,
        AlertBook.book_id == book.id,
    )
    link = (await test_db_session.execute(stmt_link)).scalar_one_or_none()
    assert link is not None

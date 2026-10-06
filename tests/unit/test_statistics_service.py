"""Tests unitaires pour le service et le dépôt de statistiques."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AlertStatus, ComparisonStatus, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.db.repositories.stats_repo import StatsRepository
from app.services.statistics_service import StatisticsService
from tests.database import fresh_database


@pytest.fixture
async def stats_session():
    """Crée une session SQLite en mémoire avec données de test pour les statistiques."""
    async with fresh_database(replayed_on_postgres=False) as (session, _maker):
        user = User(telegram_user_id=999, is_allowed=True)
        cat_bd = Category(slug="bd-comics", label="BD & Comics", emoji="📚", is_active=True)
        cat_tech = Category(slug="tech", label="Tech", emoji="💻", is_active=True)
        session.add_all([user, cat_bd, cat_tech])
        await session.flush()

        # 2 alertes actives, 1 en pause
        alert1 = Alert(
            user_id=user.id,
            category_id=cat_bd.id,
            name="Alerte Batman",
            include_keywords=["batman"],
            status=AlertStatus.ACTIVE,
        )
        alert2 = Alert(
            user_id=user.id,
            category_id=cat_tech.id,
            name="Alerte GPU",
            include_keywords=["rtx"],
            status=AlertStatus.ACTIVE,
        )
        alert3 = Alert(
            user_id=user.id,
            category_id=cat_bd.id,
            name="Alerte Spiderman",
            include_keywords=["spiderman"],
            status=AlertStatus.PAUSED,
        )
        session.add_all([alert1, alert2, alert3])
        await session.flush()

        # Annonces reçues
        now = datetime.now(UTC)
        listing1 = Listing(
            source_name="vinted",
            external_id="ext_1",
            url="https://vinted.fr/items/1",
            title="Batman Tome 1",
            normalized_title="batman tome 1",
            category_slug="bd-comics",
            item_price_cents=1000,
            received_at=now,
            raw_payload={},
        )
        listing2 = Listing(
            source_name="vinted",
            external_id="ext_2",
            url="https://vinted.fr/items/2",
            title="RTX 4080",
            normalized_title="rtx 4080",
            category_slug="tech",
            item_price_cents=80000,
            received_at=now,
            raw_payload={},
        )
        listing3 = Listing(
            source_name="vinted",
            external_id="ext_3",
            url="https://vinted.fr/items/3",
            title="Batman Tome 2",
            normalized_title="batman tome 2",
            category_slug="bd-comics",
            item_price_cents=1200,
            received_at=now,
            raw_payload={},
        )
        session.add_all([listing1, listing2, listing3])
        await session.flush()

        # Matches
        match1 = ListingMatch(
            alert_id=alert1.id,
            listing_id=listing1.id,
            notified_at=now,
            created_at=now,
        )
        match2 = ListingMatch(
            alert_id=alert1.id,
            listing_id=listing3.id,
            notified_at=now,
            created_at=now,
        )
        match3 = ListingMatch(
            alert_id=alert2.id,
            listing_id=listing2.id,
            notified_at=None,  # non notifié
            created_at=now,
        )
        session.add_all([match1, match2, match3])
        await session.flush()

        # Livres & cache marché
        book = Book(
            title="Batman Tome 1",
            normalized_title="batman tome 1",
            isbn13="9781234567890",
        )
        session.add(book)
        await session.flush()

        cache1 = BookMarketCache(
            book_id=book.id,
            lowest_delivered_price_cents=1500,
            best_provider_name="ebay",
            comparison_status=ComparisonStatus.AVAILABLE,
            confidence=MatchConfidence.HIGH,
        )
        session.add(cache1)
        await session.commit()

        yield session


@pytest.mark.asyncio
async def test_stats_repository_counts(stats_session: AsyncSession):
    """Vérifie l'exactitude des requêtes d'agrégation de StatsRepository."""
    repo = StatsRepository(stats_session)
    data = await repo.get_period_stats(days=7)

    assert data["days"] == 7
    assert data["active_alerts"] == 2  # 2 actives (sur 3)
    assert len(data["category_alerts"]) == 2
    assert data["received_listings"] == 3
    assert data["matched_listings"] == 3
    assert data["sent_notifications"] == 2  # 2 avec notified_at
    assert data["top_category"] == "bd-comics"  # 2 annonces BD vs 1 Tech
    assert len(data["top_alerts"]) >= 1
    assert data["top_alerts"][0] == ("Alerte Batman", 2, "📚")
    assert data["bd_comparisons_available"] == 1
    assert data["bd_comparisons_unavailable"] == 0


@pytest.mark.asyncio
async def test_statistics_service_formatting(stats_session: AsyncSession):
    """Vérifie le formatage HTML du message de statistiques."""
    service = StatisticsService(stats_session)
    msg = await service.get_formatted_stats(days=7)

    assert "📊 Statistiques" in msg
    assert "<b>Période :</b> 7 derniers jours" in msg
    assert "<b>Alertes actives :</b> 2 (📚 1 • 💻 1)" in msg
    assert "<b>Annonces reçues :</b> 3" in msg
    assert "<b>Annonces correspondant à une alerte :</b> 3 (100,0 %)" in msg
    assert "<b>Alertes envoyées :</b> 2" in msg
    assert "<b>Catégorie la plus active :</b> 📚 Livres" in msg
    assert "• 📚 <b>Alerte Batman</b> : 2 alertes" in msg
    assert "Comparaisons de prix (livres) :" in msg
    assert "• <b>Disponibles :</b> 1" in msg
    assert "• <b>Indisponibles :</b> 0" in msg


@pytest.mark.asyncio
async def test_statistics_formatting_empty_period():
    """Vérifie le formatage sans annonces ni alertes déclenchées."""
    empty_data = {
        "days": 30,
        "active_alerts": 0,
        "received_listings": 0,
        "matched_listings": 0,
        "sent_notifications": 0,
        "top_category": "Aucune",
        "top_alerts": [],
        "bd_comparisons_available": 0,
        "bd_comparisons_unavailable": 0,
    }
    msg = StatisticsService.format_stats_message(empty_data)

    assert "📊 Statistiques" in msg
    assert "<b>Période :</b> 30 derniers jours" in msg
    assert "Aucune alerte déclenchée sur la période" in msg
    assert "Aucune" in msg

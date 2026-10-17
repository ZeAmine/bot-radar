"""Dépôt de données pour le calcul des métriques et statistiques d'activité."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AlertStatus, ComparisonStatus
from app.db.models.alert import Alert
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.utils.percentile import interpolated_percentile

# Échantillon des percentiles de latence, borné pour garder la requête peu coûteuse
# quel que soit le volume de notifications conservé.
LATENCY_SAMPLE_SIZE = 500


class StatsRepository:
    """Gestion des requêtes statistiques d'audit et d'activité du bot."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count_active_alerts_by_category(self) -> list[tuple[Any, ...]]:
        """Compte les alertes actives par catégorie."""
        stmt = (
            select(Category.slug, Category.label, Category.emoji, func.count(Alert.id))
            .join(Alert, Alert.category_id == Category.id)
            .where(Alert.status == AlertStatus.ACTIVE, Alert.deleted_at.is_(None))
            .group_by(Category.slug, Category.label, Category.emoji)
            .order_by(func.count(Alert.id).desc())
        )
        return list((await self.session.execute(stmt)).all())

    async def get_period_stats(self, days: int) -> dict[str, Any]:
        """Calcule les métriques d'activité sur les `days` derniers jours."""
        now = datetime.now(UTC)
        threshold = now - timedelta(days=days)

        stmt_active_alerts = select(func.count(Alert.id)).where(
            Alert.status == AlertStatus.ACTIVE,
            Alert.deleted_at.is_(None),
        )
        active_alerts_count = (await self.session.execute(stmt_active_alerts)).scalar_one() or 0

        cat_alerts_rows = await self.count_active_alerts_by_category()
        category_alerts = [
            {"slug": r[0], "label": r[1], "emoji": r[2], "count": r[3]}
            for r in cat_alerts_rows
        ]

        stmt_received_listings = select(func.count(Listing.id)).where(
            Listing.received_at >= threshold
        )
        received_listings_count = (
            await self.session.execute(stmt_received_listings)
        ).scalar_one() or 0

        stmt_matches = select(func.count(ListingMatch.id)).where(
            ListingMatch.created_at >= threshold
        )
        matches_count = (await self.session.execute(stmt_matches)).scalar_one() or 0

        stmt_notified = select(func.count(ListingMatch.id)).where(
            ListingMatch.created_at >= threshold,
            ListingMatch.notified_at.is_not(None),
        )
        notified_count = (await self.session.execute(stmt_notified)).scalar_one() or 0

        stmt_top_category = (
            select(Listing.category_slug, func.count(Listing.id).label("cnt"))
            .where(Listing.received_at >= threshold)
            .group_by(Listing.category_slug)
            .order_by(func.count(Listing.id).desc())
            .limit(1)
        )
        top_cat_row = (await self.session.execute(stmt_top_category)).first()
        top_category = top_cat_row[0] if top_cat_row else "Aucune"

        stmt_top_alerts = (
            select(Alert.name, func.count(ListingMatch.id).label("cnt"), Category.emoji)
            .join(ListingMatch, Alert.id == ListingMatch.alert_id)
            .outerjoin(Category, Alert.category_id == Category.id)
            .where(ListingMatch.created_at >= threshold)
            .group_by(Alert.name, Category.emoji)
            .order_by(func.count(ListingMatch.id).desc())
            .limit(5)
        )
        top_alerts_rows = (await self.session.execute(stmt_top_alerts)).all()
        top_alerts = [
            (row[0], row[1], row[2] if len(row) > 2 and row[2] else "")
            for row in top_alerts_rows
        ]

        stmt_bd_available = select(func.count(BookMarketCache.id)).where(
            BookMarketCache.comparison_status == ComparisonStatus.AVAILABLE
        )
        bd_available_count = (await self.session.execute(stmt_bd_available)).scalar_one() or 0

        stmt_bd_unavailable = select(func.count(BookMarketCache.id)).where(
            BookMarketCache.comparison_status != ComparisonStatus.AVAILABLE
        )
        bd_unavailable_count = (await self.session.execute(stmt_bd_unavailable)).scalar_one() or 0

        return {
            "days": days,
            "active_alerts": active_alerts_count,
            "category_alerts": category_alerts,
            "received_listings": received_listings_count,
            "matched_listings": matches_count,
            "sent_notifications": notified_count,
            "top_category": top_category,
            "top_alerts": top_alerts,
            "bd_comparisons_available": bd_available_count,
            "bd_comparisons_unavailable": bd_unavailable_count,
        }

    async def get_notification_latency_ms(self, days: int = 7) -> dict[str, Any]:
        """Écart des timestamps réception persistée → marquage, pas durée jusqu'au commit.

        Seuls les matches notifiés dont la réception est dans la fenêtre `days`
        participent, au plus LATENCY_SAMPLE_SIZE lignes triées par réception
        décroissante (pas par date de notification). Les écarts négatifs sont
        exclus. Les pending et tentatives sans accusé ne sont pas ce dénominateur.
        F01 conserve la réception initiale ; une reprise SQL F03-B peut déplacer
        le timestamp du marquage, posé avant l'UPDATE et son commit. Aucun
        horodatage de publication Vinted n'est transmis par le poller actuel.
        Percentiles interpolés en Python, même sémantique sur SQLite/PostgreSQL.
        """
        threshold = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(ListingMatch.notified_at, Listing.received_at)
            .join(Listing, Listing.id == ListingMatch.listing_id)
            .where(
                ListingMatch.notified_at.is_not(None),
                Listing.received_at >= threshold,
            )
            .order_by(Listing.received_at.desc())
            .limit(LATENCY_SAMPLE_SIZE)
        )
        rows = (await self.session.execute(stmt)).all()

        def to_seconds(delta: Any) -> float:
            # SQLite renvoie l'écart en secondes (float), PostgreSQL un intervalle :
            # les deux sont acceptés, aucun n'est supposé.
            return float(delta.total_seconds()) if hasattr(delta, "total_seconds") else float(delta)

        # Un écart négatif (horloge incohérente, ligne importée) fausserait le p50 :
        # il est exclu.
        seconds = sorted(
            to_seconds(notified_at - received_at)
            for notified_at, received_at in rows
            if notified_at is not None and received_at is not None
            and notified_at >= received_at
        )

        def percentile_ms(pct: float) -> int | None:
            value = interpolated_percentile(seconds, pct)
            return None if value is None else int(round(value * 1000))

        return {
            "days": days,
            "count": len(seconds),
            "p50_ms": percentile_ms(50),
            "p95_ms": percentile_ms(95),
        }

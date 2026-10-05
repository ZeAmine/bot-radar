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

# Taille de l'échantillon servant au calcul des percentiles de latence. Borné pour que
# la requête reste peu coûteuse quel que soit le volume de notifications conservé.
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

        # 1. Nombre d'alertes actives au total et par catégorie
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

        # 2. Annonces reçues
        stmt_received_listings = select(func.count(Listing.id)).where(
            Listing.received_at >= threshold
        )
        received_listings_count = (
            await self.session.execute(stmt_received_listings)
        ).scalar_one() or 0

        # 3. Annonces correspondant à une alerte (matches)
        stmt_matches = select(func.count(ListingMatch.id)).where(
            ListingMatch.created_at >= threshold
        )
        matches_count = (await self.session.execute(stmt_matches)).scalar_one() or 0

        # 4. Alertes Telegram envoyées
        stmt_notified = select(func.count(ListingMatch.id)).where(
            ListingMatch.created_at >= threshold,
            ListingMatch.notified_at.is_not(None),
        )
        notified_count = (await self.session.execute(stmt_notified)).scalar_one() or 0

        # 5. Catégorie la plus active
        stmt_top_category = (
            select(Listing.category_slug, func.count(Listing.id).label("cnt"))
            .where(Listing.received_at >= threshold)
            .group_by(Listing.category_slug)
            .order_by(func.count(Listing.id).desc())
            .limit(1)
        )
        top_cat_row = (await self.session.execute(stmt_top_category)).first()
        top_category = top_cat_row[0] if top_cat_row else "Aucune"

        # 6. Alertes les plus déclenchées
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

        # 7 & 8. Comparaisons BD disponibles et indisponibles
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
        """Délai entre l'ingestion d'une annonce et l'envoi de son alerte, en ms.

        C'est le seul délai mesurable de bout en bout : `Listings.published_at` est
        vide, car la charge utile de Vinted ne porte aucun horodatage de publication.
        Le délai face à la publication — le seul qui dirait si l'on est parmi les
        premiers — reste donc hors de portée sans cette donnée amont.

        Ce que la mesure couvre, c'est ce que l'application contrôle : matcher,
        comparer, construire le message et l'envoyer. Mesuré à 0,98 s en p50 en
        production, dont ~0,85 s pour la photo que Telegram doit télécharger.

        Le percentile est calculé en Python sur les `LATENCY_SAMPLE_SIZE` notifications
        les plus récentes, et non par `percentile_cont` : cette fonction n'existe que
        sur PostgreSQL, alors que les tests tournent par défaut sur SQLite. L'échantillon
        est borné pour que la requête reste peu coûteuse quel que soit le volume.
        L'interpolation elle-même est celle de `percentile_cont`, partagée avec la
        cadence de scrutation (`interpolated_percentile`) pour que les deux nombres
        affichés par `/status` se lisent pareil.
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
            # SQLite peut renvoyer l'écart déjà en secondes (float) là où PostgreSQL
            # renvoie un intervalle : les deux sont acceptés, aucun n'est supposé.
            return float(delta.total_seconds()) if hasattr(delta, "total_seconds") else float(delta)

        # Un écart négatif n'est pas une latence : il signale une horloge incohérente
        # ou une ligne importée. Le conserver ferait lire un p50 qui n'a aucun sens.
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

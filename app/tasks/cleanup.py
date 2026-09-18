"""Tâche de nettoyage des anciens événements et snapshots."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from app.db.models.bot_event import BotEvent
from app.db.models.listing import Listing
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def cleanup_old_data_task(days_retention: int = 90, listings_retention_days: int = 14) -> None:
    """Supprime les événements d'audit, snapshots et annonces sans match obsolètes."""
    threshold = datetime.now(UTC) - timedelta(days=days_retention)
    listings_threshold = datetime.now(UTC) - timedelta(days=listings_retention_days)

    async with AsyncSessionLocal() as session:
        try:
            stmt_events = delete(BotEvent).where(BotEvent.created_at < threshold)
            res_events = await session.execute(stmt_events)

            stmt_snapshots = delete(MarketPriceSnapshot).where(
                MarketPriceSnapshot.checked_at < threshold
            )
            res_snapshots = await session.execute(stmt_snapshots)

            # Purge des annonces sans alerte liée (évite l'explosion du stockage sur VPS)
            stmt_unmatched_listings = delete(Listing).where(
                Listing.received_at < listings_threshold,
                ~Listing.matches.any(),
            )
            res_listings = await session.execute(stmt_unmatched_listings)

            # Purge des alertes supprimées et des livres orphelins (avec leurs caches et snapshots)
            from app.db.repositories.alert_repo import AlertRepository
            alert_repo = AlertRepository(session)
            orphan_stats = await alert_repo.purge_orphans_and_deleted()

            await session.commit()
            events_count = getattr(res_events, "rowcount", 0)
            snapshots_count = getattr(res_snapshots, "rowcount", 0)
            listings_count = getattr(res_listings, "rowcount", 0)
            logger.info(
                "Nettoyage effectué : %s événements, %s snapshots, %s annonces, %s alertes et %s livres orphelins supprimés.",
                events_count,
                snapshots_count,
                listings_count,
                orphan_stats.get("deleted_alerts", 0),
                orphan_stats.get("deleted_orphans", 0),
            )
        except Exception as exc:
            await session.rollback()
            logger.error("Erreur lors du nettoyage des données anciennes: %s", exc)

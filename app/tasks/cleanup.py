"""Tâche de nettoyage des anciens événements et snapshots."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, update

from app.db.models.alert_book import AlertBook
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.bot_event import BotEvent
from app.db.models.listing import Listing
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def cleanup_old_data_task(
    days_retention: int = 90, listings_retention_days: int = 14
) -> None:
    """Supprime les événements d'audit, snapshots et annonces sans match obsolètes."""
    threshold = datetime.now(UTC) - timedelta(days=days_retention)
    listings_threshold = datetime.now(UTC) - timedelta(days=listings_retention_days)

    async with AsyncSessionLocal() as session:
        try:
            stmt_events = delete(BotEvent).where(BotEvent.created_at < threshold)
            res_events = await session.execute(stmt_events)

            repo = MarketPriceRepository(session)
            await repo.qualify_legacy_observations()
            now = datetime.now(UTC)
            at = func.coalesce(MarketPriceSnapshot.observed_at, MarketPriceSnapshot.checked_at)
            observation_threshold = now - timedelta(days=366)
            payload_threshold = now - timedelta(days=90)
            # Les payloads extérieurs ne participent pas à la preuve normalisée.
            await session.execute(
                update(MarketPriceSnapshot)
                .where(
                    MarketPriceSnapshot.checked_at < payload_threshold,
                )
                .values(raw_payload=None)
            )
            old_caches = (await session.scalars(select(BookMarketCache).where(or_(
                BookMarketCache.checked_at < payload_threshold,
                BookMarketCache.checked_at.is_(None),
            )))).all()
            for cache in old_caches:
                if cache.current_offers:
                    cache.current_offers = [
                        {key: value for key, value in item.items() if key != "raw_payload"}
                        for item in cache.current_offers
                    ]
            stmt_snapshots = delete(MarketPriceSnapshot).where(
                or_(
                    (MarketPriceSnapshot.annual_eligible.is_(True)) & (at < observation_threshold),
                    (MarketPriceSnapshot.annual_eligible.is_(False))
                    & (MarketPriceSnapshot.checked_at < threshold),
                )
            )
            res_snapshots = await session.execute(stmt_snapshots)
            due_ids = list(
                (
                    await session.scalars(
                        select(BookMarketCache.book_id).where(
                            or_(
                                BookMarketCache.annual_expires_at.is_(None),
                                BookMarketCache.annual_expires_at <= now,
                            )
                        )
                    )
                ).all()
            )
            await repo.refresh_annual_references(due_ids, now=now)
            from app.services.book_market_pricing import refresh_series_total

            alert_ids = (
                await session.scalars(
                    select(AlertBook.alert_id).where(AlertBook.book_id.in_(due_ids)).distinct()
                )
            ).all()
            for alert_id in sorted(alert_ids):
                await refresh_series_total(session, alert_id)

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

            from app.db.repositories.pause_action_repo import PauseActionRepository

            await PauseActionRepository(session).purge()
            await session.commit()
            events_count = getattr(res_events, "rowcount", 0)
            snapshots_count = getattr(res_snapshots, "rowcount", 0)
            listings_count = getattr(res_listings, "rowcount", 0)
            logger.info(
                "Nettoyage effectué : %s événements, %s snapshots, %s annonces, "
                "%s alertes et %s livres orphelins supprimés.",
                events_count,
                snapshots_count,
                listings_count,
                orphan_stats.get("deleted_alerts", 0),
                orphan_stats.get("deleted_orphans", 0),
            )
        except Exception as exc:
            await session.rollback()
            logger.error("Erreur lors du nettoyage des données anciennes: %s", exc)

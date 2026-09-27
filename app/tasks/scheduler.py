"""Configuration et démarrage du planificateur de tâches APScheduler."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.tasks.cleanup import cleanup_old_data_task
from app.tasks.refresh_book_prices import refresh_book_prices_task

logger = logging.getLogger(__name__)


def create_scheduler() -> AsyncIOScheduler:
    """Crée et configure le scheduler avec les horaires configurés."""
    scheduler = AsyncIOScheduler(timezone="Europe/Paris")

    # Parsing des horaires configurés (ex: '07:00,19:00')
    times = [t.strip() for t in settings.PRICE_REFRESH_TIMES.split(",") if ":" in t]
    for time_str in times:
        try:
            hour_str, minute_str = time_str.split(":")
            hour = int(hour_str)
            minute = int(minute_str)

            scheduler.add_job(
                refresh_book_prices_task,
                trigger=CronTrigger(hour=hour, minute=minute, timezone="Europe/Paris"),
                id=f"refresh_prices_{hour:02d}_{minute:02d}",
                name=f"Rafraîchissement des prix BD à {hour:02d}:{minute:02d}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info(
                "Job planifié : rafraîchissement BD à %02d:%02d Europe/Paris.", hour, minute
            )
        except Exception as exc:
            logger.error(
                "Format d'horaire invalide pour PRICE_REFRESH_TIMES '%s': %s", time_str, exc
            )

    # Job incrémental glissant toutes les 30 minutes : rafraîchit les 8 livres les plus
    # anciens/expirés
    # Évite tout pic de charge CPU/réseau et garantit une fraîcheur 24h/24 des cotes
    scheduler.add_job(
        refresh_book_prices_task,
        trigger=CronTrigger(minute="15,45", timezone="Europe/Paris"),
        id="refresh_prices_incremental",
        name="Rafraîchissement glissant des prix BD (lots de 8)",
        kwargs={"incremental": True, "batch_size": 8},
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Job planifié : rafraîchissement glissant BD toutes les 30 min (lots de 8).")

    # Job quotidien de nettoyage et purge des données obsolètes à 03:30 Europe/Paris
    scheduler.add_job(
        cleanup_old_data_task,
        trigger=CronTrigger(hour=3, minute=30, timezone="Europe/Paris"),
        id="cleanup_old_data_03_30",
        name="Nettoyage nocturne des annonces et snapshots obsolètes",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Job planifié : nettoyage nocturne des données à 03:30 Europe/Paris.")

    return scheduler


scheduler = create_scheduler()

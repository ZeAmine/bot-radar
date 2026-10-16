"""Configuration et démarrage du planificateur de tâches APScheduler."""

import logging
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.tasks.cleanup import cleanup_old_data_task
from app.tasks.proactive_autotune import run_proactive_autotune_scan
from app.tasks.recover_notifications import RECOVERY_SCAN_SECONDS, recover_notifications
from app.tasks.refresh_book_prices import refresh_book_prices_task
from app.use_cases.prepare_bd_alert import recover_bd_preparations

logger = logging.getLogger(__name__)


def create_scheduler() -> AsyncIOScheduler:
    """Crée et configure le scheduler avec les horaires configurés."""
    scheduler = AsyncIOScheduler(timezone="Europe/Paris")

    def refresh_job(job_id: str, **kwargs: Any) -> Any:
        """Rend le rafraîchissement en lui transmettant l'instant **prévu**.

        APScheduler connaît l'instant prévu de chaque exécution — il le calcule et
        le fait circuler jusqu'à l'exécuteur —, mais il n'appelle la fonction qu'avec
        `job.args` et `job.kwargs` : l'horodatage est perdu au dernier moment. Sans
        lui, la tâche ne daterait son occurrence qu'avec l'heure à laquelle elle
        s'exécute vraiment, ce qui confondrait une exécution **retardée** (la passe de
        19:00 lancée à 19:31) avec l'exécution prévue de 19:30.

        `next_run_time` est lu au moment de l'appel, avant que la tâche ne rende la
        main : APScheduler ne l'avance qu'**après** avoir soumis le job, donc la
        valeur lue est bien celle de l'exécution en cours.
        """

        async def job_avec_occurrence() -> dict[str, int]:
            job = scheduler.get_job(job_id)
            planifie = getattr(job, "next_run_time", None) if job is not None else None
            return await refresh_book_prices_task(
                job_id=job_id, scheduled_for=planifie, **kwargs,
            )

        return job_avec_occurrence

    scheduler.add_job(
        recover_bd_preparations, "interval", minutes=5,
        next_run_time=datetime.now(UTC), id="recover_bd_preparations",
        name="Reprise des préparations BD", replace_existing=True,
        max_instances=1, coalesce=True,
    )

    scheduler.add_job(
        recover_notifications, "interval", seconds=RECOVERY_SCAN_SECONDS,
        id="recover_notifications", max_instances=1, coalesce=True, replace_existing=True,
    )

    # Parsing des horaires configurés (ex: '07:00,19:00')
    times = [t.strip() for t in settings.PRICE_REFRESH_TIMES.split(",") if ":" in t]
    for time_str in times:
        try:
            hour_str, minute_str = time_str.split(":")
            hour = int(hour_str)
            minute = int(minute_str)

            scheduler.add_job(
                refresh_job(f"refresh_prices_{hour:02d}_{minute:02d}"),
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

    # Lot glissant : minimum de 8, ajusté à la population par le repository.
    # Cible de réussite à 24 h ; une source indisponible reste signalée après 48 h.
    scheduler.add_job(
        refresh_job("refresh_prices_incremental", incremental=True, batch_size=8),
        trigger=CronTrigger(minute="15,45", timezone="Europe/Paris"),
        id="refresh_prices_incremental",
        name="Rafraîchissement glissant des prix BD",
        next_run_time=datetime.now(UTC),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Job planifié : rafraîchissement glissant BD toutes les 30 min (lots adaptés).")

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

    # Job d'Auto-Tuning proactif des alertes toutes les 4 heures en journée
    scheduler.add_job(
        run_proactive_autotune_scan,
        trigger=CronTrigger(hour="10,14,18,22", minute=0, timezone="Europe/Paris"),
        id="proactive_autotune_scan",
        name="Auto-Tuning proactif des alertes (exclusions IA)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Job planifié : Auto-Tuning proactif à 10h, 14h, 18h et 22h Europe/Paris.")

    return scheduler


scheduler = create_scheduler()

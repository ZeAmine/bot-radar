"""Configuration et démarrage du planificateur de tâches APScheduler."""

import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

from apscheduler.events import EVENT_JOB_SUBMITTED, JobSubmissionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.services.market_health import check_cal_storage_health
from app.tasks.cleanup import cleanup_old_data_task
from app.tasks.night_book_maintenance import (
    NIGHT_MAINTENANCE_JOB_ID,
    night_timezone,
    run_night_book_maintenance,
)
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

        APScheduler connaît l'instant prévu de chaque exécution et le fait circuler
        jusqu'à l'exécuteur, mais il n'appelle la fonction qu'avec `job.args` et
        `job.kwargs` : l'horodatage est perdu au dernier moment. Sans lui, la tâche
        ne daterait son occurrence qu'avec l'heure à laquelle elle s'exécute vraiment,
        ce qui confondrait une exécution **retardée** (la passe de 19:00 lancée à
        19:31) avec l'exécution prévue de 19:30.

        L'instant est pris de `EVENT_JOB_SUBMITTED`, dont le champ
        `scheduled_run_times` porte, par contrat, « les instants auxquels le job
        devait s'exécuter ». L'événement est émis **avant** la soumission et avant
        que `next_run_time` ne soit avancé, ce qui le rend fiable à cet endroit.

        Lire `job.next_run_time` au moment de l'exécution ne convenait pas : la
        valeur est avancée à la planification de l'exécution suivante, et une
        exécution retardée lirait alors une heure **future** — l'exécution de 19:00
        lancée à 19:31 se verrait attribuer une occurrence ultérieure. Le champ de
        l'événement, lui, reste celui de l'exécution déclenchée.

        L'instant est rangé dans une file par job : si deux exécutions se chevauchaient,
        chacune consommerait la sienne, dans l'ordre de soumission.
        """
        en_attente: deque[datetime] = deque()

        def retenir_instant(event: JobSubmissionEvent) -> None:
            if event.job_id == job_id and event.scheduled_run_times:
                en_attente.append(min(event.scheduled_run_times))

        scheduler.add_listener(retenir_instant, EVENT_JOB_SUBMITTED)

        async def job_avec_occurrence() -> dict[str, int]:
            planifie = en_attente.popleft() if en_attente else None
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

    scheduler.add_job(
        check_cal_storage_health, "interval", minutes=1,
        id="check_cal_storage_health", max_instances=1, coalesce=True, replace_existing=True,
    )

    scheduler.add_job(
        refresh_job("refresh_prices"),
        trigger=IntervalTrigger(minutes=max(1, settings.PRICE_PASS_INTERVAL_MINUTES)),
        id="refresh_prices",
        name="Rafraîchissement des prix BD",
        next_run_time=datetime.now(UTC),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "Job planifié : rafraîchissement BD toutes les %d min, à la capacité du régulateur.",
        max(1, settings.PRICE_PASS_INTERVAL_MINUTES),
    )

    scheduler.add_job(
        run_night_book_maintenance,
        trigger=CronTrigger(minute="20,50", timezone=night_timezone()),
        id=NIGHT_MAINTENANCE_JOB_ID,
        name="Maintenance nocturne des livres",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "Job planifié : maintenance nocturne des livres à :20 et :50, "
        "pendant la plage du mode nuit."
    )

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

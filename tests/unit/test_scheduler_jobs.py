"""Cadence des jobs du scheduler : la reprise des notifications suit ses backoffs."""

from datetime import timedelta

from app.tasks.recover_notifications import RECOVERY_SCAN_SECONDS
from app.tasks.scheduler import create_scheduler


def test_notification_recovery_scans_twice_a_minute() -> None:
    """Le balayage de reprise tient la moitié du plus court backoff d'admission.

    Les backoffs d'admission valent au moins une minute ; balayer toutes les trente
    secondes divise par deux l'attente entre l'échéance d'une reprise et sa tentative,
    sans jamais devancer un `retry_after` de Telegram — la date d'échéance reste la
    borne. Un balayage plus lent que le plus court backoff laisserait une reprise due
    attendre un tour complet pour rien.
    """
    scheduler = create_scheduler()  # Jamais démarré dans ce test.
    job = scheduler.get_job("recover_notifications")

    assert job is not None
    assert job.trigger.interval == timedelta(seconds=RECOVERY_SCAN_SECONDS)
    assert RECOVERY_SCAN_SECONDS <= 60
    assert job.max_instances == 1
    assert job.coalesce is True

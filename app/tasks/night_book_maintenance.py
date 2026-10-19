"""Maintenance des livres pendant la plage nocturne de la surveillance Vinted."""

import logging
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.tasks.refresh_book_prices import refresh_book_prices_task
from app.tasks.vinted_poller import get_night_mode_remaining_seconds, is_night_time

logger = logging.getLogger(__name__)

NIGHT_MAINTENANCE_JOB_ID = "night_book_maintenance"
NIGHT_MAINTENANCE_END_MARGIN_SECONDS = 120.0
NIGHT_MAINTENANCE_PAUSE_RANGE = (2.5, 3.8)

_lots_done = 0
_editions_done = 0


def empty_stats() -> dict[str, int]:
    """Compteurs d'une passe qui n'a rien lancé."""
    return dict.fromkeys(
        ("processed", "refreshed", "empty", "reused", "available",
         "unavailable", "degraded", "errors"),
        0,
    )


def night_timezone() -> str:
    """Fuseau du mode nuit, avec le repli déjà appliqué par le poller."""
    try:
        ZoneInfo(settings.NIGHT_MODE_TIMEZONE)
    except Exception:
        return "Europe/Paris"
    return settings.NIGHT_MODE_TIMEZONE


def _log_window_close() -> None:
    global _lots_done, _editions_done
    if not _lots_done:
        return
    logger.info(
        "Maintenance nocturne close : %d lot(s), %d édition(s) traitée(s) ; "
        "le travail restant est reporté à la prochaine plage.",
        _lots_done,
        _editions_done,
        extra={
            "event": "night_maintenance",
            "phase": "end",
            "lots": _lots_done,
            "editions": _editions_done,
        },
    )
    _lots_done = 0
    _editions_done = 0


async def run_night_book_maintenance() -> dict[str, int]:
    """Traite un lot de maintenance si la plage nocturne est ouverte.

    Une seule passe par déclenchement : le lot reste borné, la progression est
    persistée par les réservations de prix, et la fin de plage ne peut pas être
    franchie puisqu'elle borne aussi la durée de la passe.
    """
    global _lots_done, _editions_done

    if not settings.NIGHT_MODE_ENABLED:
        return empty_stats()
    if not is_night_time():
        _log_window_close()
        return empty_stats()

    budget = get_night_mode_remaining_seconds() - NIGHT_MAINTENANCE_END_MARGIN_SECONDS
    if budget <= 0:
        _log_window_close()
        return empty_stats()

    batch_size = max(1, settings.NIGHT_MAINTENANCE_BATCH_SIZE)
    logger.info(
        "Maintenance nocturne : lot de %d édition(s) au plus, fin de plage dans %.0f min.",
        batch_size,
        budget / 60.0,
        extra={
            "event": "night_maintenance",
            "phase": "lot",
            "batch_size": batch_size,
            "remaining_s": round(budget, 1),
        },
    )
    stats = await refresh_book_prices_task(
        incremental=True,
        batch_size=batch_size,
        max_duration_seconds=budget,
        job_id=NIGHT_MAINTENANCE_JOB_ID,
        pause_range=NIGHT_MAINTENANCE_PAUSE_RANGE,
    )
    _lots_done += 1
    _editions_done += stats["processed"]
    return stats

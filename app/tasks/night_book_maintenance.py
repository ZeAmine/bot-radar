"""Maintenance des livres pendant la plage nocturne de la surveillance Vinted."""

import logging
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.tasks.refresh_book_prices import refresh_book_prices_task
from app.tasks.vinted_poller import get_night_mode_remaining_seconds, is_night_time

logger = logging.getLogger(__name__)

NIGHT_MAINTENANCE_JOB_ID = "night_book_maintenance"
# Le balayage nocturne ne marche qu'une zone : la froide. C'est la seule que la
# file de jour ne sert qu'une fois chauds et tièdes écoulés — autrement dit jamais
# quand le marché bouge. Le laisser reprendre « toutes zones » en ferait une copie
# de la passe ordinaire, ce qu'il était devenu.
NIGHT_SWEEP_ZONES: tuple[str, ...] = ("cold",)
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
    """Balaye une vague de livres froids si la plage nocturne est ouverte.

    Ce n'est pas « la passe de jour, la nuit » : c'est un tour de rôle réservé à
    la zone froide. La file ordinaire sert les zones par urgence, si bien qu'un
    livre à quatre fois le plafond de son alerte peut attendre indéfiniment que
    les livres chauds se calment. La nuit, quand rien ne presse, la place libre
    lui revient.

    Une seule vague par déclenchement : le lot reste borné, la progression est
    persistée par les échéances, et la fin de plage ne peut pas être franchie
    puisqu'elle borne aussi la durée de la passe.
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
    zones_label = ", ".join(NIGHT_SWEEP_ZONES)
    logger.info(
        "Balayage nocturne : %d livre(s) %s au plus, fin de plage dans %.0f min.",
        batch_size,
        zones_label,
        budget / 60.0,
        extra={
            "event": "night_maintenance",
            "phase": "lot",
            "batch_size": batch_size,
            "zones": zones_label,
            "remaining_s": round(budget, 1),
        },
    )
    stats = await refresh_book_prices_task(
        batch_size=batch_size,
        zones=NIGHT_SWEEP_ZONES,
        max_duration_seconds=budget,
        job_id=NIGHT_MAINTENANCE_JOB_ID,
        pause_range=NIGHT_MAINTENANCE_PAUSE_RANGE,
    )
    _lots_done += 1
    _editions_done += stats["processed"]
    logger.info(
        "Balayage nocturne : %d livre(s) servi(s) hors de leur rythme rattrapé(s).",
        stats["processed"],
        extra={"event": "night_maintenance", "phase": "progress",
               "processed": stats["processed"]},
    )
    if stats["processed"] == 0:
        logger.info("Balayage nocturne : aucun livre froid en retard à servir.")
    return stats

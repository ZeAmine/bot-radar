"""Prévient qu'une cote de marché n'est plus nourrie par aucune observation.

Une cote affichée ne se périme pas d'elle-même : le cache la conserve, et son
`historical_low_at` est réécrit à chaque passe, si bien qu'un chiffre mort depuis
des semaines se lit comme vérifié hier. C'est exactement le défaut qui a laissé
« 22,90 € » dans une alerte trois semaines après que la source eut cessé de
répondre — rien ne l'avait signalé.

Ce module ne corrige rien : il regarde ce qui a réellement été constaté et prévient
une fois par jour. La correction, elle, est ailleurs (ADR-019).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.redis import get_redis_client
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.services.market_price_service import format_cents
from app.services.telegram_service import notify_admins

logger = logging.getLogger(__name__)

# Au-delà de ce délai sans observation certifiée, la cote affichée est une vestige.
STALE_AFTER_DAYS = 7

# Un message par jour suffit : le job passe deux fois par jour.
NOTIFY_KEY = "radar:market:stale_low:notified"
NOTIFY_TTL_SECONDS = 20 * 3600

# Au-delà, la liste devient un mur : on annonce le reste en une ligne.
MAX_LISTED = 8


@dataclass(frozen=True)
class StaleCertifiedLow:
    """Une cote toujours affichée, que plus aucun relevé ne vient confirmer."""

    title: str
    isbn13: str | None
    low_cents: int
    last_seen: datetime | None

    @property
    def days_without_observation(self) -> int | None:
        """Jours depuis la dernière observation ; ``None`` si elle n'a jamais eu lieu."""
        if self.last_seen is None:
            return None
        aware = self.last_seen if self.last_seen.tzinfo else self.last_seen.replace(tzinfo=UTC)
        return max((datetime.now(UTC) - aware).days, 0)


def _age_line(item: StaleCertifiedLow) -> str:
    days = item.days_without_observation
    if days is None:
        return "jamais observée dans les relevés conservés"
    return f"rien depuis {days} j"


def format_stale_certified_lows(items: list[StaleCertifiedLow]) -> str:
    """Message d'état : ce qui est figé, et pourquoi cela ne s'auto-corrige pas."""
    if not items:
        return ""

    listed = items[:MAX_LISTED]
    lines = "\n".join(
        f"• <i>{item.title}</i> — {format_cents(item.low_cents)} ({_age_line(item)})"
        for item in listed
    )
    remainder = len(items) - len(listed)
    tail = f"\n• … et {remainder} autre(s)" if remainder > 0 else ""

    return (
        "⚠️ <b>Cotes de marché figées</b>\n\n"
        f"{len(items)} livre(s) affichent encore un prix bas constaté que plus aucun "
        "relevé ne nourrit :\n\n"
        f"{lines}{tail}\n\n"
        "Ces valeurs restent affichées tant qu'une source ne les corrige pas : elles ne "
        "se périment pas toutes seules.\n"
        "<code>scripts/sync_chasse_session.py</code> relance la série 1 an."
    )


async def _claim_notification_slot() -> bool:
    """Réserve le droit d'envoyer, une fois par fenêtre. Un Redis muet n'empêche rien."""
    try:
        claimed = await get_redis_client().set(
            NOTIFY_KEY, "1", ex=NOTIFY_TTL_SECONDS, nx=True
        )
        return bool(claimed)
    except Exception as exc:  # pragma: no cover - Redis indisponible
        # Mieux vaut deux messages qu'un silence : le job passe deux fois par jour.
        logger.warning("Dédoublonnage de l'alerte de cote indisponible : %s", exc)
        return True


async def notify_stale_certified_lows(
    session: Any,
    *,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> int:
    """Prévient l'utilisateur des cotes figées. Renvoie le nombre signalé.

    Ne lève jamais : un contrôle de santé qui casse le rafraîchissement qu'il
    surveille serait le meilleur moyen de ne jamais savoir ce qu'il voyait.
    """
    try:
        stale_after = datetime.now(UTC) - timedelta(days=stale_after_days)
        rows = await MarketPriceRepository(session).find_stale_certified_lows(
            stale_after=stale_after, limit=MAX_LISTED
        )
        if not rows:
            return 0

        items = [
            StaleCertifiedLow(
                title=title,
                isbn13=isbn,
                low_cents=low,
                last_seen=last_seen,
            )
            for title, isbn, low, last_seen in rows
        ]
        if not await _claim_notification_slot():
            logger.debug(
                "Cotes figées détectées (%s), déjà signalées aujourd'hui.", len(items)
            )
            return 0

        await notify_admins(format_stale_certified_lows(items))
        logger.warning(
            "Cotes de marché figées signalées : %s livre(s) sans observation récente.",
            len(items),
        )
        return len(items)
    except Exception as exc:
        logger.warning("Contrôle de santé des cotes impossible : %s", exc)
        return 0

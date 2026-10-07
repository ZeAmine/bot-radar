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
from time import monotonic
from typing import Any

from app.core.redis import get_redis_client
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.services.market_price_service import (
    COTE_UNAVAILABLE_LINE,
    NO_CATALOGUE_LINE,
    PENDING_COTE_LINE,
    SEARCHING_BOOK_LINE,
    is_no_offer_line,
)
from app.services.telegram_service import notify_admins
from app.utils.price import format_cents

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


def format_stale_certified_lows(
    items: list[StaleCertifiedLow],
    total: int | None = None,
) -> str:
    """Message d'état : ce qui est figé, et pourquoi cela ne s'auto-corrige pas.

    `total` est le nombre réel de livres figés, quand il dépasse la liste affichée.
    Sans lui, un message tronqué à huit lignes annoncerait « 8 livres » pour cent onze :
    le contrôle qui sous-estime son propre incident vaut moins que pas de contrôle.
    """
    if not items:
        return ""

    listed = items[:MAX_LISTED]
    affected = max(total or 0, len(items))
    lines = "\n".join(
        f"• <i>{item.title}</i> — {format_cents(item.low_cents)} ({_age_line(item)})"
        for item in listed
    )
    remainder = affected - len(listed)
    tail = f"\n• … et {remainder} autre(s)" if remainder > 0 else ""

    return (
        "⚠️ <b>Cotes de marché figées</b>\n\n"
        f"{affected} livre(s) affichent encore un prix bas constaté que plus aucun "
        "relevé ne nourrit :\n\n"
        f"{lines}{tail}\n\n"
        "Ces valeurs restent affichées tant qu'une source ne les corrige pas : elles ne "
        "se périment pas toutes seules.\n"
        "<code>scripts/sync_chasse_session.py</code> relance la série 1 an."
    )


_notification_deadlines: dict[str, float] = {}


async def _claim_slot(key: str, ttl: int) -> bool:
    """Dédoublonne aussi pendant une panne Redis, dans l'unique processus."""
    now = monotonic()
    if _notification_deadlines.get(key, 0) > now:
        return False
    try:
        claimed = bool(await get_redis_client().set(key, "1", ex=ttl, nx=True))
    except Exception:
        logger.warning("Dédoublonnage Redis indisponible ; repli local pour %s", key)
        claimed = True
    if claimed:
        _notification_deadlines[key] = now + ttl
    return claimed


async def _claim_notification_slot() -> bool:
    return await _claim_slot(NOTIFY_KEY, NOTIFY_TTL_SECONDS)


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
        rows, total = await MarketPriceRepository(session).find_stale_certified_lows(
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
                "Cotes figées détectées (%s), déjà signalées aujourd'hui.", total
            )
            return 0

        await notify_admins(format_stale_certified_lows(items, total))
        logger.warning(
            "Cotes de marché figées signalées : %s livre(s) sans observation récente.",
            total,
        )
        return total
    except Exception as exc:
        logger.warning("Contrôle de santé des cotes impossible : %s", exc)
        return 0


# --- Alertes de fin de passe ---

# Seuil d'erreurs au-delà duquel la passe est considérée comme défaillante.
ERROR_RATIO_THRESHOLD = 0.5
# En dessous de ce nombre d'erreurs, le ratio n'a pas de sens statistique.
MIN_ERRORS_FOR_ALERT = 3

# Un fournisseur dont les relevés se taisent depuis ce délai est signalé.
SILENT_PROVIDER_AFTER_HOURS = 48
# Dédoublonnage propre à « aucune cote » : comme les deux autres, cette alerte
# ne part qu'une fois par fenêtre. Sans elle, elle suivait le rythme du job
# glissant, soit jusqu'à quarante-huit messages par jour.
NO_PRICES_NOTIFY_KEY = "radar:market:no_prices:notified"
NO_PRICES_NOTIFY_TTL_SECONDS = 20 * 3600
# Fenêtre de sollicitation : un fournisseur doit avoir été réellement interrogé
# pendant cette période avant que son silence ne devienne un incident.
PROVIDER_RECENT_WINDOW_DAYS = 7
SILENT_PROVIDER_MIN_OBSERVATIONS = 3

# Dédoublonnage propre aux sources silencieuses : un figement de cote et une
# source muette sont deux incidents distincts qui ne doivent pas s'étouffer.
SILENT_NOTIFY_KEY = "radar:market:silent_provider:notified"
SILENT_NOTIFY_TTL_SECONDS = 20 * 3600


async def _claim_no_prices_notification_slot() -> bool:
    return await _claim_slot(NO_PRICES_NOTIFY_KEY, NO_PRICES_NOTIFY_TTL_SECONDS)


async def _claim_silent_notification_slot() -> bool:
    return await _claim_slot(SILENT_NOTIFY_KEY, SILENT_NOTIFY_TTL_SECONDS)


def _provider_age_line(last_seen: datetime | None, now: datetime) -> str:
    """Heures depuis le dernier relevé d'un fournisseur, ou « jamais »."""
    if last_seen is None:
        return "jamais"
    aware = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=UTC)
    hours = max((now - aware).total_seconds() / 3600, 0)
    return f"dernier relevé il y a {hours:.0f} h"


async def notify_silent_providers(session: Any) -> int:
    """Signale les fournisseurs de cotation sans aucun relevé depuis 48 h.

    Un fournisseur en panne ne casse rien : les autres répondent, le cache
    continue de servir son dernier prix, et le silence passe inaperçu. Ce
    contrôle ne lève jamais et n'envoie qu'une fois par fenêtre — un contrôle
    de santé qui casse ce qu'il surveille serait le meilleur moyen de ne jamais
    savoir ce qu'il voyait.
    """
    try:
        now = datetime.now(UTC)
        silent_after = now - timedelta(hours=SILENT_PROVIDER_AFTER_HOURS)
        recent_after = now - timedelta(days=PROVIDER_RECENT_WINDOW_DAYS)
        rows = await MarketPriceRepository(session).find_silent_providers(
            silent_after=silent_after,
            recent_after=recent_after,
            min_recent_observations=SILENT_PROVIDER_MIN_OBSERVATIONS,
        )
        if not rows:
            return 0
        if not await _claim_silent_notification_slot():
            logger.debug("%d source(s) muette(s), déjà signalée(s) aujourd'hui.", len(rows))
            return 0

        lines = "\n".join(
            f"• <b>{name}</b> — {_provider_age_line(last_seen, now)}"
            for name, last_seen in rows
        )
        message = (
            "⚠️ <b>Sources de cotation muettes</b>\n\n"
            f"{len(rows)} fournisseur(s) interrogés régulièrement n'ont renvoyé "
            f"aucun relevé depuis {SILENT_PROVIDER_AFTER_HOURS} h :\n\n"
            f"{lines}\n\n"
            "Leur dernier prix reste affiché dans les cotes : sans relevé nouveau, "
            "rien ne le corrige."
        )
        await notify_admins(message)
        logger.warning(
            "%d fournisseur(s) de cotation silencieux depuis %d h.",
            len(rows),
            SILENT_PROVIDER_AFTER_HOURS,
        )
        return len(rows)
    except Exception as exc:
        logger.warning("Contrôle des sources de cotation impossible : %s", exc)
        return 0


async def notify_incomplete_refresh(
    processed: int,
    total: int,
    *,
    mode_label: str,
) -> None:
    """Prévient quand la passe n'a pas traité tous les livres (plafond de durée)."""
    if processed >= total:
        return
    message = (
        f"⚠️ <b>Rafraîchissement incomplet</b>\n\n"
        f"Mode {mode_label} : {processed}/{total} livres traités.\n"
        "Le reste attendra la prochaine passe."
    )
    await notify_admins(message)
    logger.warning("Rafraîchissement incomplet : %d/%d livres traités.", processed, total)


async def notify_high_error_ratio(
    processed: int,
    errors: int,
    *,
    mode_label: str,
) -> None:
    """Prévient quand une part des livres a échoué au lieu de simplement manquer.

    Un livre sans offre est normal ; une passe où la moitié des livres terminent
    en exception signale un fournisseur ou un réseau défaillant, et rien ne le
    dira autrement.
    """
    if processed == 0 or errors < MIN_ERRORS_FOR_ALERT:
        return
    if errors / processed < ERROR_RATIO_THRESHOLD:
        return
    message = (
        f"⚠️ <b>Rafraîchissement dégradé</b>\n\n"
        f"Mode {mode_label} : {errors}/{processed} livres en erreur.\n"
        "Les cotes de ces livres n'ont pas été mises à jour."
    )
    await notify_admins(message)
    logger.warning(
        "Rafraîchissement dégradé : %d/%d livres en erreur.", errors, processed
    )


async def notify_no_prices_found(
    processed: int,
    total: int,
    available: int,
    *,
    incremental: bool,
    mode_label: str,
    errors: int = 0,
    failed_providers: tuple[str, ...] = (),
) -> None:
    """Signale une passe complète sans prix seulement si un échec est attesté.

    Une absence normale d'offres reste dans le panneau du livre. Les lots
    incrémentaux et les passes partielles ne prouvent jamais un incident global.
    """
    if not errors and not failed_providers:
        logger.debug("Collecte terminée sans offre : aucun incident technique.")
        return
    if processed == 0 or available > 0:
        return
    # `total` est la sélection, pas le catalogue : en mode incrémental la
    # sélection EST le lot, donc `processed == total` toujours, et compter ne
    # prouve rien. C'est le mode qui dit ce qu'une passe a couvert.
    if incremental:
        logger.debug(
            "Lot incrémental de %d livres sans cote : aucun verdict, "
            "la sélection est le lot lui-même.",
            processed,
        )
        return
    # `processed > 0` implique une sélection non vide, donc `total > 0`.
    if processed < total:
        logger.debug(
            "Passe partielle (%d/%d) sans cote : aucun verdict.", processed, total
        )
        return
    if not await _claim_no_prices_notification_slot():
        logger.debug("Passe sans cote déjà signalée dans la fenêtre.")
        return

    message = (
        f"⚠️ <b>Collecte de prix en échec</b>\n\n"
        f"Passe {mode_label} : {processed} livres traités, 0 cote disponible.\n\n"
        f"{errors} livre(s) en erreur. Les derniers prix connus sont conservés.\n"
        + (f"Sources en échec : {', '.join(failed_providers)}.\n" if failed_providers else "")
        + "La prochaine collecte réessaiera automatiquement."

    )
    await notify_admins(message)
    logger.warning("Collecte de prix en échec sur une passe complète (%d livres).", processed)


# --- Diagnostic : avec quelle cote une alerte BD part-elle ? ---
#
# Le diagnostic au repos ne peut pas trancher : le job glissant finit par coter un
# livre lié, si bien que la base affiche « cote utilisable » longtemps après une
# alerte partie muette. Ces compteurs-ci mesurent l'instant de l'envoi, et rien
# d'autre : un compteur par état, incrémenté après que l'alerte est partie.

COTE_STATE_KEY_PREFIX = "radar:cote_sent_state:"
COTE_STATES = ("real", "pending", "no_offer", "searching", "uncatalogued", "unavailable")


def classify_sent_cote_state(market_line: str | None) -> str:
    """État de la cote réellement transmise à une alerte BD.

    ``real`` : une cote chiffrée. ``pending`` : un livre identifié, sa cote en
    cours. ``no_offer`` : un livre identifié, une passe a eu lieu et n'a trouvé
    aucune offre. ``searching`` : aucun livre résolu, la recherche est annoncée.
    ``uncatalogued`` : aucun livre résolu, et la recherche a **conclu** qu'aucune
    source ne connaît l'ouvrage. ``unavailable`` : la collecte a échoué sans
    cote utilisable. Une ligne absente est comptée ``pending`` : c'est
    un livre dont la ligne manque, pas une recherche qui n'a pas lieu.
    """
    if market_line == NO_CATALOGUE_LINE:
        return "uncatalogued"
    if market_line == SEARCHING_BOOK_LINE:
        return "searching"
    if is_no_offer_line(market_line):
        return "no_offer"
    if market_line and market_line.startswith(COTE_UNAVAILABLE_LINE):
        return "unavailable"
    if market_line is None or market_line == PENDING_COTE_LINE:
        return "pending"
    return "real"


async def record_sent_cote_state(state: str) -> None:
    """Incrémente le compteur d'un état. Ne lève jamais : une mesure n'est pas une alerte."""
    try:
        await get_redis_client().incr(f"{COTE_STATE_KEY_PREFIX}{state}")
    except Exception as exc:  # pragma: no cover - Redis indisponible
        logger.debug("Compteur d'état de cote non incrémenté (%s) : %s", state, exc)


async def read_sent_cote_states() -> dict[str, int]:
    """Lecture brute des compteurs d'état, pour le rapport d'audit."""
    values = await get_redis_client().mget(
        [f"{COTE_STATE_KEY_PREFIX}{state}" for state in COTE_STATES]
    )
    return {state: int(value or 0) for state, value in zip(COTE_STATES, values, strict=True)}

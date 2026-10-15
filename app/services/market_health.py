"""Signale les collectes d'offres ou d'historique réellement absentes sous 48 h.

La date du prix minimum ne mesure pas la fraîcheur de la collecte. Les preuves
persistées distinguent les deux, sans renouveler un relevé lors d'un échec.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from app.core.alert_latency import observe_background
from app.core.redis import get_redis_client
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.services.market_price_service import (
    COTE_UNAVAILABLE_LINE,
    LOT_UNRESOLVED_LINE,
    NO_CATALOGUE_LINE,
    PENDING_COTE_LINE,
    SEARCHING_BOOK_LINE,
    is_no_offer_line,
)
from app.services.telegram_service import notify_admins

logger = logging.getLogger(__name__)

# Au-delà de ce délai sans collecte réussie, le retard doit être visible. C'est la
# **seule** référence de péremption du module : les deux contrôles — historique et
# cote courante — la partagent. Deux valeurs distinctes rendaient le même incident
# visible ou invisible selon le contrôle qui le regardait.
STALE_AFTER_DAYS = 7

# Un message par jour suffit : le job passe deux fois par jour.
NOTIFY_KEY = "radar:market:stale_low:notified"
NOTIFY_TTL_SECONDS = 20 * 3600

# Au-delà, la liste devient un mur : on annonce le reste en une ligne.
MAX_LISTED = 8


@dataclass(frozen=True)
class StaleCertifiedLow:
    """Un ouvrage dont la collecte n’a pas réussi récemment."""

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
    *, historical: bool = True, stale_after_days: int = STALE_AFTER_DAYS,
) -> str:
    """Message d'état : ce qui est figé, et pourquoi cela ne s'auto-corrige pas.

    `total` est le nombre réel de livres figés, quand il dépasse la liste affichée.
    Sans lui, un message tronqué à huit lignes annoncerait « 8 livres » pour cent onze :
    le contrôle qui sous-estime son propre incident vaut moins que pas de contrôle.

    Le délai annoncé vient du seuil qui a servi à sélectionner les lignes. Écrit en
    dur, il affichait « 48 heures » sous un seuil de sept jours — l'utilisateur
    recevait un chiffre que le contrôle n'avait pas appliqué.
    """
    if not items:
        return ""

    listed = items[:MAX_LISTED]
    affected = max(total or 0, len(items))
    lines = "\n".join(
        f"• <i>{item.title}</i> ({_age_line(item)})"
        for item in listed
    )
    remainder = affected - len(listed)
    tail = f"\n• … et {remainder} autre(s)" if remainder > 0 else ""

    title = "Historique fournisseur non actualisé" if historical else "Offres non actualisées"
    return (
        f"⚠️ <b>{title}</b>\n\n"
        f"{affected} livre(s) sans collecte réussie depuis plus de "
        f"{stale_after_days} jours :\n\n"
        f"{lines}{tail}"
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
    session: Any, *, stale_after_days: int = STALE_AFTER_DAYS,
) -> int:
    """Signale une collecte historique absente, jamais un minimum simplement ancien."""
    return await _notify_stale_collections(session, stale_after_days=stale_after_days,
                                           historical=True)


async def notify_stale_current_collections(
    session: Any, *, stale_after_days: int = STALE_AFTER_DAYS,
) -> int:
    """Signale les offres sans collecte réussie depuis le seuil partagé de péremption.

    Le seuil venait d'un `2` écrit en dur, à côté d'une constante qui valait autre
    chose : le même livre pouvait être « périmé » pour un contrôle et pas pour
    l'autre. Les deux lisent maintenant `STALE_AFTER_DAYS`.
    """
    return await _notify_stale_collections(session, stale_after_days=stale_after_days,
                                           historical=False)


async def _notify_stale_collections(
    session: Any,
    *,
    stale_after_days: int = STALE_AFTER_DAYS,
    historical: bool,
) -> int:
    """Prévient l'utilisateur des cotes figées. Renvoie le nombre signalé.

    Ne lève jamais : un contrôle de santé qui casse le rafraîchissement qu'il
    surveille serait le meilleur moyen de ne jamais savoir ce qu'il voyait.
    """
    try:
        stale_after = datetime.now(UTC) - timedelta(days=stale_after_days)
        repo = MarketPriceRepository(session)
        read = repo.find_stale_certified_lows if historical else repo.find_stale_current_collections
        rows, total = await read(stale_after=stale_after, limit=MAX_LISTED)
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
        claimed = (await _claim_notification_slot() if historical else
                   await _claim_slot("radar:market:stale_current:notified", NOTIFY_TTL_SECONDS))
        if not claimed:
            logger.debug(
                "Cotes figées détectées (%s), déjà signalées aujourd'hui.", total
            )
            return 0

        await notify_admins(
            format_stale_certified_lows(
                items, total, historical=historical, stale_after_days=stale_after_days
            )
        )
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
# Même fenêtre pour « rafraîchissement dégradé » : le job glissant passe toutes
# les trente minutes, et cette alerte était la seule à le suivre à ce rythme.
HIGH_ERROR_NOTIFY_KEY = "radar:market:high_error:notified"
HIGH_ERROR_NOTIFY_TTL_SECONDS = 20 * 3600
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


async def _claim_high_error_notification_slot() -> bool:
    return await _claim_slot(HIGH_ERROR_NOTIFY_KEY, HIGH_ERROR_NOTIFY_TTL_SECONDS)


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


def next_full_pass_note() -> str:
    """Annonce la prochaine passe **complète** planifiée, ou rien si elle est inconnue.

    La règle tient en une phrase : on n'annonce que ce que le planificateur confirme.
    Les passes glissantes à :15/:45 ne peuvent pas prendre en charge les livres
    restants — un lot de huit livres par passage ne résorbe pas un arriéré —, donc
    promettre « le reste suivra » sans nommer la passe complète serait une promesse
    que rien ne tient. Si le planificateur ne répond pas, on ne dit rien : une phrase
    en moins vaut mieux qu'une date fausse.

    Les identifiants des passes complètes sont dynamiques (`refresh_prices_HH_MM`,
    dérivés de `PRICE_REFRESH_TIMES`) : ils se cherchent par préfixe, jamais par un
    nom écrit en dur qui cesserait de correspondre au premier changement d'horaire.
    """
    try:
        from app.tasks.scheduler import scheduler

        upcoming = [
            job.next_run_time
            for job in scheduler.get_jobs()
            if job.id.startswith("refresh_prices_")
            and not job.id.endswith("_incremental")
            and getattr(job, "next_run_time", None) is not None
        ]
    except Exception as exc:  # pragma: no cover - dépend de l'état du planificateur
        logger.debug("Prochaine passe complète inconnue : %s", exc)
        return ""
    if not upcoming:
        return ""
    moment = min(upcoming).astimezone(ZoneInfo("Europe/Paris"))
    return f"\nProchaine passe complète prévue : {moment:%d/%m à %H:%M}."


async def notify_incomplete_refresh(
    processed: int,
    total: int,
    *,
    refreshed: int,
    interrupted: bool,
    incremental: bool,
    batch_size: int = 0,
) -> None:
    """Le bilan d'une passe, quand elle a quelque chose à dire.

    Trois situations, et trois seulement :

    - la passe a tout traité : rien à signaler, elle se tait ;
    - elle a été coupée par le plafond de durée — c'est le fonctionnement normal
      d'un lot qui ne tient pas dans sa fenêtre —, la coupure est **décrite dans
      le bilan** sans devenir un avertissement ;
    - elle s'est arrêtée avant la fin pour une autre raison, et le fait est signalé.

    Le bilan ne se répète pas pour la même passe : la clé porte l'identité de la
    passe, pas une fenêtre de temps. Une fenêtre de vingt heures supprimait le bilan
    de l'une des deux passes quotidiennes, séparées de douze heures seulement.
    """
    if processed >= total and not interrupted:
        return

    # « Rafraîchissement » est masculin : l'accord se fait sur le nom, pas sur le mot
    # qui suit. « Rafraîchissement complète » se lisait comme une faute.
    mode = "complet" if not incremental else "incrémental"
    if incremental and batch_size:
        mode = f"incrémental ({batch_size} livres)"

    remaining = max(total - processed, 0)
    lines = [
        f"🔄 <b>Rafraîchissement {mode}</b>",
        "",
        f"Traités : {processed}/{total} livres",
        f"Mis à jour : {refreshed} livres",
    ]
    if remaining:
        lines.append(f"Restants à traiter : {remaining} livres")
    else:
        lines.append("Tous les livres du lot ont été traités.")

    if interrupted:
        lines.append("")
        lines.append("<i>Plafond de durée atteint : les livres restants seront repris "
                     "selon l'ordre de la file.</i>")
        # La date n'est annoncée que pour une passe complète : c'est la seule dont
        # la prise en charge des restants est réellement prévue.
        if not incremental:
            lines.append(next_full_pass_note())

    await notify_admins("\n".join(lines))
    logger.info(
        "Bilan de passe (%s) : %d/%d traités, %d mis à jour, coupée=%s.",
        mode, processed, total, refreshed, interrupted,
    )


def _ratio_is_high(processed: int, count: int) -> bool:
    """Vrai quand le nombre et la part dépassent les deux seuils de la passe."""
    return (
        processed > 0
        and count >= MIN_ERRORS_FOR_ALERT
        and count / processed >= ERROR_RATIO_THRESHOLD
    )


async def notify_high_error_ratio(
    processed: int,
    errors: int,
    *,
    mode_label: str,
    failed_providers: tuple[str, ...] = (),
    degraded: int = 0,
) -> None:
    """Prévient quand une part des livres a échoué au lieu de simplement manquer.

    Une exception réelle et une collecte où les fournisseurs nommés n'ont rien
    rendu sont deux pannes. Un livre sans offre, lui, n'en est pas une. La
    fenêtre est la même que les autres alertes de cote : une panne qui dure ne
    se répète pas à chaque lot.
    """
    provider_outage = bool(failed_providers) and _ratio_is_high(processed, degraded)
    if not _ratio_is_high(processed, errors) and not provider_outage:
        return
    if not await _claim_high_error_notification_slot():
        logger.debug("Rafraîchissement dégradé déjà signalé dans la fenêtre.")
        return
    counted = errors if _ratio_is_high(processed, errors) else degraded
    sources = f"Sources en échec : {', '.join(failed_providers)}.\n" if failed_providers else ""
    message = (
        f"⚠️ <b>Rafraîchissement dégradé</b>\n\n"
        f"Mode {mode_label} : {counted}/{processed} livres en erreur.\n"
        f"{sources}"
        "Les derniers prix connus sont conservés."
    )
    await notify_admins(message)
    logger.warning("Rafraîchissement dégradé : %d/%d livres en erreur.", counted, processed)


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
    cote utilisable, une référence indisponible à l’envoi ou un lot non identifié.
    Une ligne absente est comptée ``pending`` : c'est
    un livre dont la ligne manque, pas une recherche qui n'a pas lieu.
    """
    if market_line == NO_CATALOGUE_LINE:
        return "uncatalogued"
    if market_line == SEARCHING_BOOK_LINE:
        return "searching"
    if is_no_offer_line(market_line):
        return "no_offer"
    if market_line == LOT_UNRESOLVED_LINE:
        return "unavailable"
    if market_line and market_line.startswith(COTE_UNAVAILABLE_LINE):
        return "unavailable"
    if market_line is None or market_line == PENDING_COTE_LINE:
        return "pending"
    return "real"


@observe_background("cote_state")
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

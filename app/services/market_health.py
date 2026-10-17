"""Signale les collectes d'offres ou d'historique absentes depuis sept jours.

La date du prix minimum ne mesure pas la fraîcheur de la collecte. Les preuves
persistées distinguent les deux, sans renouveler un relevé lors d'un échec.
"""

import asyncio
import html
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from app.core.alert_latency import observe_background
from app.core.redis import get_redis_client
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.providers.base import CollectionAttempt
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

STALE_AFTER_DAYS = 7

NOTIFY_KEY = "radar:market:stale_low:notified"
NOTIFY_TTL_SECONDS = 20 * 3600

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


ERROR_RATIO_THRESHOLD = 0.5
MIN_ERRORS_FOR_ALERT = 3

SILENT_PROVIDER_AFTER_HOURS = 48
NO_PRICES_NOTIFY_KEY = "radar:market:no_prices:notified"
NO_PRICES_NOTIFY_TTL_SECONDS = 20 * 3600
HIGH_ERROR_NOTIFY_KEY = "radar:market:high_error:notified"
HIGH_ERROR_NOTIFY_TTL_SECONDS = 20 * 3600
PROVIDER_RECENT_WINDOW_DAYS = 7
SILENT_PROVIDER_MIN_OBSERVATIONS = 3

SILENT_NOTIFY_KEY = "radar:market:silent_provider:notified"
SILENT_NOTIFY_TTL_SECONDS = 20 * 3600

INCOMPLETE_NOTIFY_KEY_PREFIX = "radar:market:pass_report:"
INCOMPLETE_NOTIFY_TTL_SECONDS = 3600


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


def next_full_pass_at() -> datetime | None:
    """Instant de la prochaine passe **complète** planifiée, ou ``None``.

    Les identifiants des passes complètes sont dynamiques (`refresh_prices_HH_MM`,
    dérivés de `PRICE_REFRESH_TIMES`) : ils se cherchent par préfixe, jamais par un
    nom écrit en dur qui cesserait de correspondre au premier changement d'horaire.
    Les passes glissantes à :15/:45 ne sont pas retenues : leur lot ne garantit pas
    qu'un livre donné en fasse partie.
    """
    try:
        from app.tasks.scheduler import scheduler

        now = datetime.now(UTC)
        upcoming = []
        for job in scheduler.get_jobs():
            if not job.id.startswith("refresh_prices_") or job.id.endswith("_incremental"):
                continue
            run_at = getattr(job, "next_run_time", None)
            if run_at is None:
                continue
            aware = run_at if run_at.tzinfo else run_at.replace(tzinfo=UTC)
            if aware > now:
                upcoming.append(aware)
    except Exception as exc:  # pragma: no cover - dépend de l'état du planificateur
        logger.debug("Prochaine passe complète inconnue : %s", exc)
        return None
    return min(upcoming) if upcoming else None


def next_full_pass_note() -> str:
    """Annonce la prochaine passe **complète** planifiée, ou rien si elle est inconnue.

    La règle tient en une phrase : on n'annonce que ce que le planificateur confirme.
    Les passes glissantes à :15/:45 ne peuvent pas prendre en charge les livres
    restants — un lot de huit livres par passage ne résorbe pas un arriéré —, donc
    promettre « le reste suivra » sans nommer la passe complète serait une promesse
    que rien ne tient. Si le planificateur ne répond pas, on ne dit rien : une phrase
    en moins vaut mieux qu'une date fausse.
    """
    moment = next_full_pass_at()
    if moment is None:
        return ""
    local = (
        moment.astimezone(ZoneInfo("Europe/Paris"))
        if moment.tzinfo
        else moment.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Paris"))
    )
    return f"\nProchaine passe complète prévue : {local:%d/%m à %H:%M}."


async def notify_incomplete_refresh(
    processed: int,
    total: int,
    *,
    refreshed: int,
    interrupted: bool,
    incremental: bool,
    batch_size: int = 0,
    occurrence: str | None = None,
) -> None:
    """Le bilan d'une passe, quand elle a quelque chose à dire.

    Le silence se décide sur le **type** de passe, pas sur sa réussite :

    - un passage glissant qui a traité sa sélection se tait — huit livres toutes
      les trente minutes ne se racontent pas, et un message de routine à cette
      fréquence rendrait invisible celui qui compte ;
    - une passe **complète** rend son bilan, qu'elle aille au bout ou non. C'est un
      rendez-vous planifié deux fois par jour, et le seul moment où la file entière
      est reprise : la taire quand elle réussit laissait l'utilisateur sans nouvelle
      du passage qui a effectivement tout rafraîchi. Le bilan d'une passe réussie est
      **court** — quatre nombres et une phrase, sans paragraphe de reprise ;
    - une passe interrompue, complète ou glissante, décrit sa coupure : par le
      plafond de durée — fonctionnement normal d'un lot qui ne tient pas dans sa
      fenêtre, décrit sans devenir un avertissement — ou par une autre raison, qui
      est alors signalée comme telle.

    Le titre dit le type de passe, jamais son achèvement : une passe complète coupée
    reste un « Rafraîchissement complet », et c'est la ligne « Restants à traiter »
    qui dit où elle s'est arrêtée.

    **Un bilan au plus par occurrence planifiée.** `occurrence` est l'identité de
    l'exécution prévue — l'identifiant du job et l'instant qu'il visait —, fournie
    par le planificateur. Deux tentatives pour la même occurrence partagent la clé
    et une seule passe ; deux occurrences distinctes ont deux clés, donc deux
    bilans, même à douze heures d'écart.

    Les nombres publiés satisfont deux identités, et le lecteur peut les vérifier
    de tête : `Traités = Mis à jour + Sans mise à jour`, et
    `Total = Traités + Restants à traiter`. La première vient de ce que « sans mise
    à jour » est **dérivée** de `processed - refreshed` — jamais comptée à part —,
    donc un compteur ne peut pas contredire l'autre. La seconde tient à ce que
    `remaining` est `total - processed`, borné à zéro.

    La garantie est **bornée dans le temps**, et il faut le dire ainsi : la clé
    expire avec `INCOMPLETE_NOTIFY_TTL_SECONDS`. Deux tentatives séparées par moins
    que ce délai produisent un seul bilan ; une reprise plus tardive en produit un
    second, la clé étant retombée. La borne couvre la fenêtre de réexécution prise en
    charge, elle ne prétend pas à l'éternité.

    Ce qui distingue cette clé d'une fenêtre de temps des autres alertes, c'est sa
    durée : à vingt heures elle supprimait le bilan de la seconde passe quotidienne,
    séparée de douze heures seulement. Ici la borne reste très inférieure à l'écart
    entre deux occurrences, donc elle ne peut jamais avaler la suivante.

    Sans `occurrence` — un appel hors planificateur —, le bilan part sans
    dédoublonnage : mieux vaut un doublon qu'un incident passé sous silence.
    """
    if incremental and processed >= total and not interrupted:
        return

    if occurrence is not None:
        claimed = await _claim_slot(
            f"{INCOMPLETE_NOTIFY_KEY_PREFIX}{occurrence}", INCOMPLETE_NOTIFY_TTL_SECONDS
        )
        if not claimed:
            logger.debug("Bilan déjà envoyé pour l'occurrence %s.", occurrence)
            return

    mode = "complet" if not incremental else "incrémental"
    if incremental and batch_size:
        mode = f"incrémental ({batch_size} livres)"

    remaining = max(total - processed, 0)
    unchanged = max(processed - refreshed, 0)
    lines = [
        f"🔄 <b>Rafraîchissement {mode}</b>",
        "",
        f"Traités : {processed}/{total} livres",
        f"Mis à jour : {refreshed} livres",
        f"Sans mise à jour : {unchanged} livres",
    ]
    if remaining:
        lines.append(f"Restants à traiter : {remaining} livres")
    else:
        lines.append("Tous les livres du lot ont été traités.")

    if interrupted:
        lines.append("")
        lines.append("<i>Plafond de durée atteint : les livres restants seront repris "
                     "selon l'ordre de la file.</i>")
        if not incremental:
            lines.append(next_full_pass_note())

    await notify_admins("\n".join(lines))
    logger.info(
        "Bilan de passe (%s) : %d/%d traités, %d mis à jour, %d sans mise à jour, "
        "coupée=%s.",
        mode, processed, total, refreshed, unchanged, interrupted,
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
    if incremental:
        logger.debug(
            "Lot incrémental de %d livres sans cote : aucun verdict, "
            "la sélection est le lot lui-même.",
            processed,
        )
        return
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


@dataclass
class RefreshPassResult:
    total: int
    stats: dict[str, int]
    incremental: bool = False
    occurrence: str | None = None
    interrupted: bool = False
    technical_interruption: bool = False
    selection_known: bool = True
    attempts: list[CollectionAttempt] = field(default_factory=list)
    never_collected: int | None = None
    stale_current: int | None = None
    stale_history: int | None = None


_incident_states: dict[str, dict[str, float]] = {}
_incident_observations: dict[str, dict[str, float]] = {}
_incident_pending: dict[str, str] = {}
_incident_loaded = False
_incident_lock = asyncio.Lock()
_INCIDENT_KEY = "radar:market:incidents:v2"


async def read_refresh_health(result: RefreshPassResult, session: Any) -> None:
    try:
        counts = await MarketPriceRepository(session).collection_health_counts(
            stale_after=datetime.now(UTC) - timedelta(days=STALE_AFTER_DAYS),
        )
        result.never_collected, result.stale_current, result.stale_history = counts
        logger.info("Fraîcheur des collectes", extra={"never_collected": counts[0],
                    "stale_current": counts[1], "stale_history": counts[2]})
    except Exception:
        logger.warning("Diagnostic de fraîcheur indisponible", exc_info=True)


def _incident_changes(
    result: RefreshPassResult, previous: dict[str, dict[str, float]],
) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    updated = dict(previous)
    samples: dict[str, list[bool]] = {}
    for attempt in result.attempts:
        if attempt.status != "skipped":
            samples.setdefault(f"{attempt.provider}/{attempt.operation}", []).append(
                attempt.status == "failed",
            )
    samples["passe"] = [result.technical_interruption]
    samples["traitement"] = [True] * result.stats.get("errors", 0) + [False] * max(
        result.stats["processed"] - result.stats.get("errors", 0), 0,
    )
    changes = {}
    for key, observations in samples.items():
        if not observations:
            continue
        failed = sum(observations)
        old = previous.get(key)
        if failed:
            state = {"ratio": failed / len(observations), "count": float(len(observations))}
            if old is None:
                changes[key] = f"Nouvel incident : {html.escape(key)}"
            elif (state["ratio"] == 1 and old["ratio"] < 1) or (
                len(observations) >= 3 and old["count"] >= 3
                and state["ratio"] - old["ratio"] >= 0.25
            ):
                changes[key] = f"Aggravation : {html.escape(key)}"
            updated[key] = state
        elif old is not None:
            changes[key] = f"Rétablissement : {html.escape(key)}"
            updated.pop(key, None)
    return updated, changes


async def _persist_incidents(client: Any) -> None:
    try:
        await client.set(_INCIDENT_KEY, json.dumps({
            "observed": _incident_observations,
            "notified": _incident_states,
            "pending": _incident_pending,
        }))
    except Exception:
        logger.warning("État des incidents conservé localement")


async def notify_refresh_result(result: RefreshPassResult) -> None:
    global _incident_loaded
    async with _incident_lock:
        client = get_redis_client()
        if not _incident_loaded:
            try:
                stored = await client.get(_INCIDENT_KEY)
                if isinstance(stored, (str, bytes)):
                    loaded = json.loads(stored)
                    if isinstance(loaded, dict):
                        for target, values in (
                            (_incident_observations, loaded.get("observed", loaded)),
                            (_incident_states, loaded.get("notified", loaded)),
                        ):
                            if isinstance(values, dict):
                                target.clear()
                                target.update({key: value for key, value in values.items()
                                               if isinstance(value, dict)
                                               and isinstance(value.get("ratio"), (float, int))
                                               and isinstance(value.get("count"), (float, int))})
                        pending = loaded.get("pending", {})
                        if isinstance(pending, dict):
                            _incident_pending.clear()
                            _incident_pending.update({key: value for key, value in pending.items()
                                                      if isinstance(value, str)})
            except Exception:
                logger.warning("État des incidents Redis indisponible ; repli local")
            _incident_loaded = True
        updated, observed_changes = _incident_changes(result, _incident_observations)
        for component, change in observed_changes.items():
            if component not in _incident_states:
                if component not in updated:
                    if component not in _incident_pending:
                        continue
                    change = (
                        f"Incident observé puis rétabli avant remise : {html.escape(component)}"
                    )
                elif component in _incident_pending:
                    if component in _incident_observations:
                        continue
                    change = (f"{_incident_pending[component]}\n"
                              f"Nouvel incident en cours : {html.escape(component)}")
                else:
                    change = f"Nouvel incident : {html.escape(component)}"
            _incident_pending[component] = change
        _incident_observations.clear()
        _incident_observations.update(updated)
        changes = list(_incident_pending.values())
        await _persist_incidents(client)
        if result.incremental and not changes:
            return
        key = (f"{INCOMPLETE_NOTIFY_KEY_PREFIX}{result.occurrence}"
               if not result.incremental and result.occurrence else None)
        if key and not await _claim_slot(key, INCOMPLETE_NOTIFY_TTL_SECONDS):
            return
        stats = result.stats
        population = str(result.total) if result.selection_known else "?"
        remaining = (str(max(result.total - stats["processed"], 0))
                     if result.selection_known else "inconnu")
        skipped = sum(a.status == "skipped" for a in result.attempts)
        lines = [
            "🔄 <b>Rafraîchissement "
            + ("incrémental" if result.incremental else "complet") + "</b>",
            f"Traités : {stats['processed']}/{population} livres",
            f"Actualisées : {stats['refreshed']} collectes avec offre",
            f"Caches réutilisés : {stats.get('reused', 0)}",
            f"Tentatives fournisseur non effectuées : {skipped}",
            f"Sans offre admissible : {stats.get('empty', stats['unavailable'])}",
            f"Collectes dégradées : {stats['degraded']}",
            f"Exceptions de traitement : {stats['errors']}",
            f"Non parcourus : {remaining}",
        ]
        if result.technical_interruption:
            lines.append("⚠️ Interruption technique de la passe.")
        elif result.interrupted:
            lines.append("Plafond de durée atteint.")
        if result.never_collected is not None:
            lines.extend([f"Jamais collectés : {result.never_collected}",
                          f"Dernière collecte trop ancienne : {result.stale_current}",
                          f"Historique non actualisé : {result.stale_history}"])
        elif not result.incremental:
            lines.append("Diagnostic de fraîcheur indisponible.")
        lines.extend(changes)
        failed = sorted({f"{a.provider}/{a.operation} ({a.reason or 'cause inconnue'})"
                         for a in result.attempts if a.status == "failed"})
        if stats["errors"] and not stats["refreshed"]:
            lines.append("⚠️ Collecte de prix en échec ; aucun relevé courant actualisé.")
        if stats["degraded"]:
            lines.append("⚠️ Rafraîchissement dégradé")
        if failed:
            lines.append("Échecs observés : " + html.escape(", ".join(failed)))
        try:
            if await notify_admins("\n".join(lines)) is False:
                raise RuntimeError("notification non remise")
        except (Exception, asyncio.CancelledError) as exc:
            if key:
                _notification_deadlines.pop(key, None)
                try:
                    await client.delete(key)
                except Exception:
                    logger.warning("Créneau de bilan non libéré", exc_info=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning("Bilan de rafraîchissement non remis", exc_info=True)
            return
        _incident_states.clear()
        _incident_states.update(updated)
        _incident_pending.clear()
        await _persist_incidents(client)

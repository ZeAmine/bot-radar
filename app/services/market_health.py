"""Signale les collectes d'offres ou d'historique absentes depuis sept jours.

La date du prix minimum ne mesure pas la fraîcheur de la collecte. Les preuves
persistées distinguent les deux, sans renouveler un relevé lors d'un échec.
"""

import asyncio
import html
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from app.bot.labels import escape_display
from app.core.alert_latency import observe_background
from app.core.redis import get_redis_client
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.providers.base import CollectionAttempt
from app.providers.cal_governor import CalGovernor, cal_governor
from app.services.market_price_service import (
    COTE_UNAVAILABLE_LINE,
    LOT_UNRESOLVED_LINE,
    NO_CATALOGUE_LINE,
    PENDING_COTE_LINE,
    SEARCHING_BOOK_LINE,
    is_no_offer_line,
)
from app.services.telegram_service import notify_admins, notify_admins_outcome

logger = logging.getLogger(__name__)

CAL_STORAGE_ALERT_AFTER_SECONDS = 300.0
CAL_STORAGE_ALERT_RETRY_SECONDS = 300.0
CAL_STORAGE_ALERT_MAX_ATTEMPTS = 3


class CalStorageHealth:
    def __init__(
        self, *, governor: CalGovernor,
        notify: Callable[[str], Awaitable[str]] = notify_admins_outcome,
    ) -> None:
        self.governor = governor
        self.notify = notify
        self._lock = asyncio.Lock()
        self._episode = 0
        self._attempts = 0
        self._next_attempt_at = 0.0
        self._terminal = False

    async def check(self) -> None:
        async with self._lock:
            await self.governor.snapshot()
            since = self.governor.storage_unavailable_since
            if since is None:
                return
            now = float(self.governor.monotonic_clock())
            if self._episode != self.governor.storage_incident:
                self._episode = self.governor.storage_incident
                self._attempts = 0
                self._terminal = False
                self._next_attempt_at = since + CAL_STORAGE_ALERT_AFTER_SECONDS
            if self._terminal or now < self._next_attempt_at:
                return
            if self._attempts == 0:
                logger.error("Stockage du régulateur CAL indisponible depuis au moins cinq minutes")
            self._attempts += 1
            self._terminal = True
            try:
                outcome = await self.notify(
                    "⚠️ <b>Régulation CAL indisponible</b>\n"
                    "Le stockage Redis du régulateur est inaccessible "
                    "depuis au moins cinq minutes. "
                    "Les nouveaux appels CAL sont refusés ; "
                    "les livres non interrogés sont reportés. "
                    "La reprise sera automatique après rétablissement du stockage."
                )
            except Exception as exc:
                logger.warning("Résultat de l’alerte Redis incertain (%s)", type(exc).__name__)
                return
            if outcome == "failed" and self._attempts < CAL_STORAGE_ALERT_MAX_ATTEMPTS:
                self._terminal = False
                self._next_attempt_at = now + CAL_STORAGE_ALERT_RETRY_SECONDS
            if outcome != "accepted":
                logger.warning("Alerte Redis non confirmée : %s", outcome)


_cal_storage_health = CalStorageHealth(governor=cal_governor)


async def check_cal_storage_health() -> None:
    await _cal_storage_health.check()

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


SILENT_PROVIDER_AFTER_HOURS = 192
PROVIDER_RECENT_WINDOW_DAYS = 14
SILENT_PROVIDER_MIN_OBSERVATIONS = 3

SILENT_NOTIFY_KEY = "radar:market:silent_provider:notified"
SILENT_NOTIFY_TTL_SECONDS = 20 * 3600

# Le bilan quotidien : une fois par jour, trois lignes, trois faits.
MARKET_SUMMARY_JOB_ID = "market_daily_summary"
MARKET_SUMMARY_HOUR = 9
MARKET_SUMMARY_TIMEZONE = "Europe/Paris"


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
            f"• <b>{escape_display(name)}</b> - {_provider_age_line(last_seen, now)}"
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
    # Toute passe est bornée : elle sert ce que le régulateur accorde. Il n'y a donc
    # plus de modes à distinguer — ni « incrémental », ni « complet » —, et plus de
    # compteurs publiés ; `occurrence` ne sert qu'à étiqueter les traces.
    occurrence: str | None = None
    interrupted: bool = False
    technical_interruption: bool = False
    selection_known: bool = True
    attempts: list[CollectionAttempt] = field(default_factory=list)
    never_collected: int | None = None
    stale_current: int | None = None
    stale_history: int | None = None
    # Livres dont une composante dépasse son échéance au-delà de la tolérance
    # d'affichage : ce sont eux qui, à l'écran, ne montrent plus un montant mais
    # une phrase d'âge. Le compte est publié à chaque passe pour que ce retard
    # soit une mesure et non une impression.
    out_of_rhythm: int | None = None


# --------------------------------------------------------------------------- #
# Ce qui mérite un message : une panne de source prouvée, jamais un échantillon
# --------------------------------------------------------------------------- #

# Mesure du 31 octobre (journaux de production, 11:22 → 12:26) : une passe toutes
# les quinze minutes, la moitié sans aucune collecte, et une à deux collectes par
# couple quand il y en a — `CAL_MAX_BOOKS_PER_PASS = 2` et une seule composante
# demandée par passe. Un échantillon en échec y vaut donc un taux de 1,0 sans rien
# prouver : une règle en taux déclare une panne de source sur un accident de
# passage. Les règles ci-dessous se comptent en observations **consécutives** et
# en durée ; un échec isolé, et une alternance succès/échec, ne peuvent donc
# jamais produire de message.
INCIDENT_MIN_FAILURES = 3
INCIDENT_MIN_SPAN_SECONDS = 3600.0
RECOVERY_MIN_ANSWERS = 3
RECOVERY_MIN_SPAN_SECONDS = 3600.0

# Douze heures : deux fois le repos maximal du régulateur (défi), pour qu'un
# sommeil de la source ne l'efface pas. Passé ce délai sans une seule tentative,
# le couple n'existe plus (source purgée, renommée) : il sort de l'état sans
# message, puisque sa panne comme son rétablissement ne concernent plus personne.
INCIDENT_FORGET_SECONDS = 12 * 3600.0

ANSWERED = "answered"
FAILED = "failed"

# Un échec qui n'accuse pas la source : elle a répondu, c'est le livre qui n'était
# pas là (mauvais tome, mauvaise édition) ou notre budget de requêtes qui a manqué
# (`incomplete_offers`, mesuré en HTTP 200). C'est un relevé incomplet, pas une
# panne.
NOT_A_SOURCE_FAULT = frozenset({
    "incomplete_offers", "identity_mismatch", "volume_mismatch", "empty_response",
})

# En 4xx la requête est fautive — un 404 dit que ce livre-là n'est plus en vente —
# sauf ces codes, qui accusent la source : quota, authentification refusée. Un 5xx
# et l'absence de réponse sont déjà des échecs.
SOURCE_FAULT_STATUSES = frozenset({401, 403, 429})

SOURCE_REASON_LABELS = {
    "timeout": "délai dépassé",
    "connection": "connexion impossible",
    "rate_limited": "quota refusé (HTTP 429)",
    "bot_challenge": "défi anti-robot",
    "session_unavailable": "session refusée",
    "invalid_response": "réponse illisible",
    "http_status": "erreur HTTP",
    "internal_error": "erreur interne",
    "unclassified": "échec non classé",
}

# L'heure des messages est celle du capitaine, comme les dates d'affichage.
_INCIDENT_TZ = ZoneInfo("Europe/Paris")


@dataclass
class SourceRun:
    """La suite d'observations identiques la plus récente d'un couple."""

    outcome: str
    samples: int
    first_at: float
    last_at: float
    causes: list[str] = field(default_factory=list)

    def extended(self, outcome: str, cause: str | None, at: float) -> "SourceRun":
        if outcome != self.outcome:
            return SourceRun(outcome, 1, at, at, [cause] if cause else [])
        causes = list(self.causes)
        if cause and cause not in causes:
            causes.append(cause)
        return SourceRun(outcome, self.samples + 1, self.first_at, at, causes)

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome, "samples": self.samples,
            "first_at": self.first_at, "last_at": self.last_at, "causes": list(self.causes),
        }

    @classmethod
    def from_json(cls, raw: Any) -> "SourceRun | None":
        if not isinstance(raw, dict) or raw.get("outcome") not in {ANSWERED, FAILED}:
            return None
        samples = _number(raw.get("samples"))
        first_at = _number(raw.get("first_at"))
        last_at = _number(raw.get("last_at"))
        if samples is None or first_at is None or last_at is None:
            return None
        causes = raw.get("causes", [])
        return cls(
            outcome=raw["outcome"], samples=int(samples),
            first_at=first_at, last_at=last_at,
            causes=[cause for cause in causes if isinstance(cause, str)]
            if isinstance(causes, list) else [],
        )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


_incident_sources: dict[str, SourceRun] = {}
_incident_announced: set[str] = set()
# Les faits décidés mais pas encore remis, avec leur nature : un incident et le
# rétablissement qui l'annule partent ensemble plutôt que de s'effacer l'un l'autre.
_incident_pending: dict[str, tuple[str, str]] = {}
_incident_loaded = False
_incident_lock = asyncio.Lock()
# La version entre dans le nom de la clé : l'état de la règle précédente n'a pas
# d'observations datées et ne peut pas servir. Il n'est pas effacé — le retour en
# arrière doit le retrouver.
_INCIDENT_KEY = "radar:market:incidents:v3"


def _split_by_rhythm(books: Any, moment: datetime) -> tuple[int, int]:
    """Sépare une population de livres en « à jour » et « hors de leur rythme ».

    Le couple est rendu ensemble pour que les deux nombres publiés ne puissent pas
    décrire deux populations différentes, ni se contredire.
    """
    from app.services.market_refresh_policy import out_of_rhythm

    late = sum(1 for book in books if out_of_rhythm(book.market_cache, moment))
    return len(books) - late, late


async def count_out_of_rhythm_books(session: Any) -> int:
    """Livres dont une composante a dépassé son échéance au-delà de la tolérance.

    C'est la mesure du modèle par composante : un livre est hors de son rythme
    quand son retard ne tient plus dans la tolérance d'affichage, c'est-à-dire
    quand sa cote ne peut plus se lire comme un montant. Le seuil est celui de
    l'écran, pas un seuil de surveillance à part — un livre « en retard » pour la
    mesure et « frais » pour l'affichage serait un mensonge de l'un ou de l'autre.
    """
    from app.db.repositories.book_repo import BookRepository

    moment = datetime.now(UTC)
    books = await BookRepository(session).get_books_for_active_bd_alerts()
    return _split_by_rhythm(books, moment)[1]


async def read_refresh_health(result: RefreshPassResult, session: Any) -> None:
    try:
        counts = await MarketPriceRepository(session).collection_health_counts(
            stale_after=datetime.now(UTC) - timedelta(days=STALE_AFTER_DAYS),
        )
        result.never_collected, result.stale_current, result.stale_history = counts
        result.out_of_rhythm = await count_out_of_rhythm_books(session)
        logger.info("Fraîcheur des collectes", extra={"never_collected": counts[0],
                    "stale_current": counts[1], "stale_history": counts[2],
                    "out_of_rhythm": result.out_of_rhythm})
    except Exception:
        logger.warning("Diagnostic de fraîcheur indisponible", exc_info=True)


def source_observation(attempt: CollectionAttempt) -> tuple[str, str | None] | None:
    """Ce que cette tentative dit de la **source** — ou rien du tout.

    Trois verdicts, et ce sont les tentatives réelles du 31 octobre qui les
    séparent :

    - ``None`` : la tentative n'a pas eu lieu (report du régulateur, plafond de la
      passe). Elle ne dit rien de la source et ne doit pas interrompre une suite
      d'observations ;
    - ``ANSWERED`` : la source a répondu. Un relevé incomplet — budget de douze
      requêtes dépassé, mesuré en HTTP 200 avec ``incomplete_offers`` — ou un livre
      écarté (mauvais tome, mauvaise édition) prouvent que la source est debout ;
    - ``FAILED`` : la source ne répond pas ou refuse, c'est-à-dire un délai
      dépassé, une connexion impossible, un quota (HTTP 429), un défi, une
      authentification refusée ou une erreur serveur.
    """
    if attempt.status == "skipped":
        return None
    if attempt.status in {"success", "partial"} or attempt.reason in NOT_A_SOURCE_FAULT:
        return (ANSWERED, None)
    if attempt.http_status is not None and 400 <= attempt.http_status < 500 \
            and attempt.http_status not in SOURCE_FAULT_STATUSES:
        return (ANSWERED, None)
    return (FAILED, attempt.reason or "unclassified")


def _now_seconds() -> float:
    return datetime.now(UTC).timestamp()


def _pass_observations(result: RefreshPassResult) -> dict[str, list[tuple[str, str | None]]]:
    """Les observations d'une passe, couple par couple, dans l'ordre des collectes."""
    observations: dict[str, list[tuple[str, str | None]]] = {}
    for attempt in result.attempts:
        verdict = source_observation(attempt)
        if verdict is not None:
            observations.setdefault(f"{attempt.provider}/{attempt.operation}", []).append(verdict)
    processed = result.stats.get("processed", 0)
    if processed > 0:
        errors = result.stats.get("errors", 0)
        observations["traitement"] = [
            (FAILED, "internal_error") if errors else (ANSWERED, None),
        ]
    return observations


def _record_pass(result: RefreshPassResult, sources: dict[str, SourceRun], now: float) -> None:
    for component, verdicts in _pass_observations(result).items():
        run: SourceRun | None = sources.get(component)
        for outcome, cause in verdicts:
            run = SourceRun(outcome, 0, now, now, []) if run is None else run
            run = run.extended(outcome, cause, now)
        if run is not None:
            sources[component] = run


def _sustained(run: SourceRun, outcome: str, minimum: int, span: float) -> bool:
    return (
        run.outcome == outcome and run.samples >= minimum
        and run.last_at - run.first_at >= span
    )


def _local_time(moment: float) -> str:
    return datetime.fromtimestamp(moment, _INCIDENT_TZ).strftime("%H:%M")


def _cause_label(run: SourceRun) -> str:
    labels = [SOURCE_REASON_LABELS.get(cause, cause) for cause in run.causes]
    return ", ".join(labels) if labels else "cause non classée"


def _incident_line(component: str, run: SourceRun) -> str:
    return (f"Nouvel incident : {html.escape(component)} — en échec depuis "
            f"{_local_time(run.first_at)} ({_cause_label(run)})")


def _recovery_line(component: str, run: SourceRun) -> str:
    return (f"Rétablissement : {html.escape(component)} — la source répond depuis "
            f"{_local_time(run.first_at)}")


def source_decisions(
    sources: dict[str, SourceRun], announced: set[str],
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """Décide, couple par couple, s'il y a un fait à annoncer — et lequel.

    Un incident n'est annoncé qu'une fois par épisode ; tant qu'il dure, la source
    se tait. Son rétablissement exige la même persistance que l'incident : trois
    réponses consécutives couvrant une heure. L'alternance échec/succès ne forme
    donc jamais ni trois échecs ni trois réponses de suite, et ne dit rien.
    """
    decisions: dict[str, tuple[str, str]] = {}
    next_announced = {component for component in announced if component in sources}
    for component, run in sources.items():
        if component not in next_announced:
            if _sustained(run, FAILED, INCIDENT_MIN_FAILURES, INCIDENT_MIN_SPAN_SECONDS):
                decisions[component] = ("incident", _incident_line(component, run))
                next_announced.add(component)
        elif _sustained(run, ANSWERED, RECOVERY_MIN_ANSWERS, RECOVERY_MIN_SPAN_SECONDS):
            decisions[component] = ("recovery", _recovery_line(component, run))
            next_announced.discard(component)
    return decisions, next_announced


def _log_decisions(
    result: RefreshPassResult, decisions: dict[str, tuple[str, str]],
    sources: dict[str, SourceRun],
) -> None:
    """Garde dans les journaux la fenêtre et les échecs qui ont motivé un message."""
    for component, (kind, _) in decisions.items():
        run = sources[component]
        extra = {
            "component": component, "occurrence": result.occurrence, "outcome": run.outcome,
            "samples": run.samples, "span_seconds": run.last_at - run.first_at,
            "first_at": datetime.fromtimestamp(run.first_at, UTC).isoformat(),
            "last_at": datetime.fromtimestamp(run.last_at, UTC).isoformat(),
            "causes": list(run.causes),
        }
        if kind == "incident":
            logger.warning("Incident de source annoncé", extra=extra)
        else:
            logger.info("Rétablissement de source annoncé", extra=extra)


async def _persist_incidents(client: Any) -> None:
    try:
        await client.set(_INCIDENT_KEY, json.dumps({
            "version": 3,
            "sources": {component: run.to_json() for component, run in _incident_sources.items()},
            "announced": sorted(_incident_announced),
            "pending": {component: list(value) for component, value in _incident_pending.items()},
        }))
    except Exception:
        logger.warning("État des incidents conservé localement")


async def _load_incidents(client: Any) -> None:
    global _incident_loaded
    if _incident_loaded:
        return
    try:
        stored = await client.get(_INCIDENT_KEY)
        if isinstance(stored, (str, bytes)):
            loaded = json.loads(stored)
            if isinstance(loaded, dict):
                sources = loaded.get("sources", {})
                if isinstance(sources, dict):
                    _incident_sources.clear()
                    for component, raw in sources.items():
                        run = SourceRun.from_json(raw)
                        if isinstance(component, str) and run is not None:
                            _incident_sources[component] = run
                announced = loaded.get("announced", [])
                if isinstance(announced, list):
                    _incident_announced.clear()
                    _incident_announced.update(
                        component for component in announced if isinstance(component, str)
                    )
                pending = loaded.get("pending", {})
                if isinstance(pending, dict):
                    _incident_pending.clear()
                    _incident_pending.update({
                        component: (value[0], value[1]) for component, value in pending.items()
                        if isinstance(value, list) and len(value) == 2
                        and value[0] in {"incident", "recovery"} and isinstance(value[1], str)
                    })
    except Exception:
        logger.warning("État des incidents Redis indisponible ; repli local", exc_info=True)
    _incident_loaded = True


async def notify_refresh_result(result: RefreshPassResult) -> None:
    """Ne dit que les **faits actionnables** d'une passe ; se tait pour le reste.

    Une passe produit un message pour deux raisons, et deux seulement :

    - une source est durablement en panne, ou durablement revenue — la ligne nomme
      la source, l'opération, la cause et l'heure de début, c'est un fait qu'on
      peut corriger ;
    - la passe a été interrompue par une exception, ce qui n'est pas le
      fonctionnement prévu d'un lot.

    « Durablement » est le contrat de `source_decisions`, et il vaut dans les deux
    sens : un passage unique en échec ne dit rien, une seule passe propre après un
    incident non plus. Un incident annoncé se tait jusqu'à son rétablissement, qui
    n'est annoncé qu'une fois.

    Tout le reste se tait. Les compteurs d'une passe — traités, mis à jour, sans
    mise à jour, restants — décrivent le travail de l'application, pas l'état du
    marché : aucun ne permet de décider quoi que ce soit. Leur contenu quotidien
    vit dans ``send_daily_market_summary``, en trois lignes.

    Dédoublonnage : l'état est partagé via Redis, donc un redémarrage ne rejoue
    pas les incidents en cours, et un fait non remis est réessayé à la passe
    suivante sans être perdu.
    """
    async with _incident_lock:
        client = get_redis_client()
        await _load_incidents(client)
        now = _now_seconds()
        _record_pass(result, _incident_sources, now)
        for component in [
            component for component, run in _incident_sources.items()
            if now - run.last_at >= INCIDENT_FORGET_SECONDS
        ]:
            # Une source qui n'est plus tentée n'existe plus : rien à annoncer.
            del _incident_sources[component]
        decisions, announced = source_decisions(
            _incident_sources, _incident_announced
            | {component for component, (kind, _) in _incident_pending.items()
               if kind == "incident"},
        )
        _log_decisions(result, decisions, _incident_sources)
        # La mémoire des épisodes se tient à jour même quand rien n'est remis : un
        # couple oublié ne doit pas rouvrir un épisode sous le même nom.
        _incident_announced.clear()
        _incident_announced.update(announced)
        for component, (kind, line) in decisions.items():
            previous = _incident_pending.get(component)
            _incident_pending[component] = (
                (kind, f"{previous[1]}\n{line}") if previous is not None and previous[0] != kind
                else (kind, line)
            )
        lines = [line for _, line in _incident_pending.values()]
        await _persist_incidents(client)
        if not lines and not result.technical_interruption:
            # Une passe qui n'a produit aucun fait actionnable ne dit rien : ses
            # compteurs décrivent le travail de l'application, pas l'état du marché.
            return
        if result.technical_interruption:
            lines.append("⚠️ Passe de rafraîchissement interrompue avant la fin.")
        try:
            if await notify_admins("\n".join(lines)) is False:
                raise RuntimeError("notification non remise")
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning("Incident de rafraîchissement non remis", exc_info=True)
            return
        _incident_pending.clear()
        await _persist_incidents(client)


# --------------------------------------------------------------------------- #
# Le bilan quotidien : trois lignes, trois faits
# --------------------------------------------------------------------------- #


def format_daily_market_summary(
    *, up_to_date: int, total: int, out_of_rhythm: int, new_lows: int,
) -> str:
    """Trois lignes, trois faits : à jour, hors de leur rythme, nouveaux prix bas.

    Chaque ligne répond à une question qu'on se pose vraiment : « est-ce que le
    marché est suivi ? », « qu'est-ce qui ne l'est plus ? », « qu'ai-je raté
    hier ? ». Aucun compteur de travail de l'application n'y figure : ni passes,
    ni lots, ni requêtes. Le nombre de livres à jour est dérivé du total et des
    livres hors de leur rythme, donc les deux chiffres ne peuvent pas se
    contredire.
    """
    return "\n".join((
        f"📊 <b>Marché des livres</b> — à jour : {up_to_date}/{total} livres",
        f"Hors de leur rythme : {out_of_rhythm} livre(s)",
        f"Nouveaux prix bas de la veille : {new_lows}",
    ))


async def build_daily_market_summary(session: Any) -> str:
    """Le bilan d'aujourd'hui, lu une seule fois en base.

    Les trois nombres sortent de la même population — les livres des alertes BD
    actives ou en pause — pour qu'un total et un retard parlent du même ensemble.
    """
    from app.db.repositories.book_repo import BookRepository

    moment = datetime.now(UTC)
    books = await BookRepository(session).get_books_for_active_bd_alerts()
    up_to_date, late = _split_by_rhythm(books, moment)
    new_lows = await MarketPriceRepository(session).count_lows_observed_since(
        since=moment - timedelta(days=1),
    )
    return format_daily_market_summary(
        up_to_date=up_to_date,
        total=len(books),
        out_of_rhythm=late,
        new_lows=new_lows,
    )


async def send_daily_market_summary() -> str | None:
    """Envoie le bilan quotidien, une fois par jour ; renvoie le texte envoyé.

    Un bilan qu'on ne peut pas construire ne s'invente pas : l'erreur est
    journalisée et rien n'est envoyé. Un message d'erreur quotidien serait un
    second bruit, pas une information.
    """
    from app.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            message = await build_daily_market_summary(session)
    except Exception:
        logger.warning("Bilan quotidien du marché indisponible", exc_info=True)
        return None
    await notify_admins(message)
    logger.info("Bilan quotidien du marché envoyé")
    return message

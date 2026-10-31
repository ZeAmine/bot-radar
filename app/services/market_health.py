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


_incident_states: dict[str, dict[str, float]] = {}
_incident_observations: dict[str, dict[str, float]] = {}
_incident_pending: dict[str, str] = {}
_incident_loaded = False
_incident_lock = asyncio.Lock()
_INCIDENT_KEY = "radar:market:incidents:v2"


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


def _incident_changes(
    result: RefreshPassResult, previous: dict[str, dict[str, float]],
) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    updated = dict(previous)
    samples: dict[str, list[bool]] = {}
    for attempt in result.attempts:
        if attempt.status != "skipped":
            samples.setdefault(f"{attempt.provider}/{attempt.operation}", []).append(
                attempt.status in {"partial", "failed"},
            )
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
    """Ne dit que les **faits actionnables** d'une passe ; se tait pour le reste.

    Une passe produit un message pour deux raisons, et deux seulement :

    - une source est durablement en échec, ou elle se rétablit — la ligne nomme la
      source et l'opération, c'est un fait qu'on peut corriger ;
    - la passe a été interrompue par une exception, ce qui n'est pas le
      fonctionnement prévu d'un lot.

    Tout le reste se tait. Les compteurs d'une passe — traités, mis à jour, sans
    mise à jour, restants — décrivent le travail de l'application, pas l'état du
    marché : aucun ne permet de décider quoi que ce soit. C'est pourquoi le bilan
    de passe, les modes « incrémental » et « complet », et la promesse d'une
    « prochaine passe complète » ont disparu ; leur contenu quotidien vit dans
    ``send_daily_market_summary``, en trois lignes.

    Dédoublonnage : un incident déjà annoncé n'est pas ré-annoncé tant qu'il ne
    s'aggrave pas, et son rétablissement est annoncé une fois. La mémoire de cet
    état est partagée via Redis, donc un redémarrage ne rejoue pas les incidents
    en cours.
    """
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
        if not changes and not result.technical_interruption:
            # Une passe qui n'a produit aucun fait actionnable ne dit rien : ses
            # compteurs décrivent le travail de l'application, pas l'état du marché.
            return
        # Un fait par ligne, et rien d'autre : ni titre de mode, ni compteurs.
        lines = list(changes)
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
        _incident_states.clear()
        _incident_states.update(updated)
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

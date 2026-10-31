"""Modèle de fraîcheur du marché : une composante, une échéance, un rythme.

La cote et le prix bas sur douze mois ne périment pas à la même vitesse et ne
coûtent pas la même chose. Chacune porte donc **sa** date de dernier relevé
réussi, **son** échéance et **son** rythme. Une échéance n'est jamais déduite
d'un compteur d'échecs : un échec dit qu'une source n'a pas répondu, il ne dit
rien du marché.

Contrat de ``BookMarketCache.refresh_state`` — écrit ici, lu ici
---------------------------------------------------------------

``refresh_state`` est un objet JSON dont chaque clé nomme une composante ::

    {
      "current": {
        "due_at": "2026-10-31T06:00:00+00:00",           # prochaine lecture due
        "last_success_at": "2026-10-31T00:00:00+00:00",  # dernier relevé réussi
        "failures": 2,                                   # échecs consécutifs
        "zone": "warm",                                  # zone du dernier relevé
        "cadence_seconds": 172800.0,                     # rythme appliqué
        "price_cents": 3200                              # cote inscrite ici
      },
      "history": {
        "due_at": "...", "last_success_at": "...", "failures": 0,
        "cadence_seconds": 2592000.0                     # 30 jours, toujours
      },
      "administrative": { ... }                          # voir administrative_deferral
    }

Les règles du contrat, toutes tenues par ce module :

1. ``due_at`` est la **seule** échéance que l'ordonnanceur lit. Elle vaut
   « dernier relevé réussi + le rythme de la composante ».
2. Un échec n'avance jamais l'échéance : le livre reprend sa place dans la file
   ordinaire au rythme suivant, il ne double plus son rythme. ``failures`` sert
   à mesurer une panne, jamais à calculer une date.
3. Sans relevé réussi, l'échéance vaut « maintenant » : jamais lu, donc dû.
4. Une composante absente de l'état se lit par repli sur les colonnes de la cote
   (``last_successful_collection_at``, ``historical_checked_at``) avec le rythme
   de la zone connue, froid par défaut. Une cote déjà en base n'est jamais
   réécrite pour entrer dans ce modèle.
5. Rien n'est écrit hors de ``record_component`` et de
   ``app.services.administrative_deferral``.
6. **La péremption s'affiche.** Au-delà du rythme de la zone plus la tolérance de
   48 h, une cote ne se présente plus comme un prix : elle dit son âge et la date
   de son dernier relevé. Ce module fournit le fait (``cote_display``), la phrase
   reste dans ``app.bot.labels`` — un seul endroit écrit ce que lit l'utilisateur.

Le rythme de la cote dépend de la **zone** du livre, recalculée à chaque relevé
de cote : c'est le voisinage du plafond de l'alerte qui justifie la fréquence,
pas l'existence du livre.

======================  ================================================  ============
Zone                    Définition                                        Rythme cote
======================  ================================================  ============
chaud (``hot``)         cote connue ≤ 1,2 × le plafond de l'alerte        6 heures
tiède (``warm``)        cote connue entre 1,2 × et 2 × le plafond          2 jours
froid (``cold``)        aucune cote, ou cote > 2 × le plafond             7 jours
======================  ================================================  ============

Le prix bas sur douze mois est lu à la création de l'alerte, puis une fois par
mois, et immédiatement après une baisse de cote de plus de 30 %. Il n'entre
jamais dans une collecte de routine.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from app.db.models.book_market_cache import BookMarketCache

_reservation_lock: asyncio.Lock | None = None
_reservation_loop: asyncio.AbstractEventLoop | None = None


def reservation_lock() -> asyncio.Lock:
    global _reservation_lock, _reservation_loop
    loop = asyncio.get_running_loop()
    if _reservation_lock is None or _reservation_loop is not loop:
        _reservation_lock, _reservation_loop = asyncio.Lock(), loop
    return _reservation_lock


Component = Literal["current", "history"]
COMPONENTS: tuple[Component, ...] = ("current", "history")

# Ce qu'une création d'alerte demande en une fois : le seul moment où attendre
# est légitime, et une dépense par livre, pas par jour.
ALL_COMPONENTS: frozenset[str] = frozenset(COMPONENTS)

# Portée d'une collecte. Trois verbes, trois besoins distincts :
#   routine  — une seule composante, la plus anciennement due (jamais les deux) ;
#   creation — tout ce qui manque, donc les deux sur un livre jamais lu ;
#   forced   — les deux, sans lire les échéances : un état qu'on conteste.
CollectionScope = Literal["routine", "creation", "forced"]

# Zones de risque, de la plus proche du plafond à la plus lointaine. L'ordre de
# service de la file suit cette liste.
ZONE_HOT = "hot"
ZONE_WARM = "warm"
ZONE_COLD = "cold"
ZONES: tuple[str, ...] = (ZONE_HOT, ZONE_WARM, ZONE_COLD)

# Seuils des zones, en multiple du plafond de l'alerte.
HOT_RATIO = 1.2
WARM_RATIO = 2.0

HOT_CADENCE = timedelta(hours=6)
WARM_CADENCE = timedelta(days=2)
COLD_CADENCE = timedelta(days=7)
HISTORY_CADENCE = timedelta(days=30)

ZONE_CADENCES: dict[str, timedelta] = {
    ZONE_HOT: HOT_CADENCE,
    ZONE_WARM: WARM_CADENCE,
    ZONE_COLD: COLD_CADENCE,
}

# Au-delà de ce retard, une cote n'est plus présentée comme un montant : elle
# devient une phrase qui dit l'âge du dernier relevé. C'est la même tolérance
# qui rend un livre « hors de son rythme » pour la mesure et pour l'affichage.
DISPLAY_TOLERANCE = timedelta(hours=48)

# Une baisse de cote de plus de 30 % déclenche une lecture immédiate de
# l'historique : le filet anti-oubli doit être tendu le jour où le marché bouge.
BIG_DROP_RATIO = 0.30


def aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Zones et rythmes
# --------------------------------------------------------------------------- #


def cote_zone(price_cents: int | None, ceiling_cents: int | None) -> str:
    """Zone d'un livre, lue sur sa cote et le plafond de son alerte.

    Sans plafond connu — une alerte sans prix maximum, ou une création qui n'en
    a pas encore —, il n'y a pas de voisinage à surveiller : le livre est froid.
    """
    if price_cents is None or not ceiling_cents or ceiling_cents <= 0:
        return ZONE_COLD
    if price_cents <= ceiling_cents * HOT_RATIO:
        return ZONE_HOT
    if price_cents <= ceiling_cents * WARM_RATIO:
        return ZONE_WARM
    return ZONE_COLD


def zone_cadence(zone: str) -> timedelta:
    """Rythme d'une zone ; une zone inconnue se lit comme froide."""
    return ZONE_CADENCES.get(zone, COLD_CADENCE)


def component_cadence(component: Component, zone: str) -> timedelta:
    """Rythme d'une composante : la zone pour la cote, trente jours pour l'historique."""
    return HISTORY_CADENCE if component == "history" else zone_cadence(zone)


def zone_rank(zone: str) -> int:
    """Priorité de service d'une zone : chaud (0) avant tiède (1) avant froid (2)."""
    try:
        return ZONES.index(zone)
    except ValueError:
        return len(ZONES)


# --------------------------------------------------------------------------- #
# Lecture des échéances
# --------------------------------------------------------------------------- #


def component_state(
    cache: BookMarketCache | None, component: Component,
) -> dict[str, Any]:
    """État écrit d'une composante, ou un dictionnaire vide."""
    if cache is None:
        return {}
    state: object = (cache.refresh_state or {}).get(component)
    return cast("dict[str, Any]", state) if isinstance(state, dict) else {}


def component_success_at(
    cache: BookMarketCache | None, component: Component,
) -> datetime | None:
    """Date du dernier relevé réussi d'une composante, quelle que soit sa source."""
    if cache is None:
        return None
    stored = component_state(cache, component).get("last_success_at")
    if isinstance(stored, str):
        try:
            return aware(datetime.fromisoformat(stored))
        except ValueError:
            pass
    if component == "current":
        found = cache.last_successful_collection_at
    else:
        found = cache.historical_checked_at
    # Une valeur qui n'est pas une date n'en est pas une : elle ne devient pas un
    # âge, et l'affichage retombe sur « jamais relevé » au lieu de calculer faux.
    return aware(found) if isinstance(found, datetime) else None


def cote_zone_of(cache: BookMarketCache | None) -> str:
    """Zone retenue au dernier relevé de cote, froide à défaut d'historique."""
    stored = component_state(cache, "current").get("zone")
    return stored if stored in ZONES else ZONE_COLD


def component_due(cache: BookMarketCache | None, component: Component, now: datetime) -> datetime:
    """Échéance d'une composante, seule date que lit l'ordonnanceur."""
    if cache is None:
        return now
    stored = component_state(cache, component).get("due_at")
    if isinstance(stored, str):
        try:
            return aware(datetime.fromisoformat(stored))
        except ValueError:
            pass
    successful = component_success_at(cache, component)
    if successful is None:
        return now
    return successful + component_cadence(component, cote_zone_of(cache))


def next_component_due(cache: BookMarketCache | None, now: datetime) -> datetime:
    """Prochaine échéance toutes composantes confondues, bornée à maintenant."""
    if cache is None:
        return now
    return max(now, min(component_due(cache, component, now) for component in COMPONENTS))


def due_components(cache: BookMarketCache | None, now: datetime) -> tuple[Component, ...]:
    """Composantes dont l'échéance est atteinte, la cote d'abord."""
    return tuple(
        component for component in COMPONENTS
        if component_due(cache, component, now) <= now
    )


def collection_due(cache: BookMarketCache | None, now: datetime) -> bool:
    """Vrai si au moins une composante attend sa lecture."""
    return bool(due_components(cache, now))


def scope_components(
    cache: BookMarketCache | None, now: datetime, scope: CollectionScope = "routine",
) -> frozenset[str]:
    """Ce qu'une collecte a le droit de demander, selon sa portée.

    ``routine`` ne demande jamais les deux composantes : c'est le partage qui
    rend à l'historique le budget que la cote absorbait. ``creation`` demande
    tout ce qui manque — les deux sur un livre jamais lu, une seule si l'autre
    vient d'être relevé —, car une alerte qui naît sans son prix bas naît sans
    son filet anti-oubli. ``forced`` ne consulte aucune échéance : l'appelant
    sait que l'état en base est faux.
    """
    if scope == "forced":
        return ALL_COMPONENTS
    if scope == "creation":
        return frozenset(due_components(cache, now))
    routine = routine_component(cache, now)
    return frozenset({routine}) if routine else frozenset()


def routine_component(cache: BookMarketCache | None, now: datetime) -> Component | None:
    """La composante qu'une collecte de routine demande — une seule, jamais deux.

    C'est la plus ancienne échéance qui passe : celle qui attend depuis le plus
    longtemps. Le choix est donc *monotone* — après sa lecture, son échéance
    repart devant l'autre —, ce qui garantit qu'aucune composante n'est affamée
    par une autre, même sur un livre chaud relu toutes les six heures. À
    échéances égales la cote passe : c'est elle qui sert à décider.

    ``None`` quand rien n'est dû : la collecte n'a alors rien à demander.
    """
    due = due_components(cache, now)
    if not due:
        return None
    return min(due, key=lambda component: (component_due(cache, component, now),
                                           COMPONENTS.index(component)))


def component_late_by(
    cache: BookMarketCache | None, component: Component, now: datetime,
) -> timedelta:
    """Retard d'une composante sur son échéance ; nul si elle n'est pas en retard."""
    late = now - component_due(cache, component, now)
    return late if late > timedelta(0) else timedelta(0)


def component_out_of_rhythm(
    cache: BookMarketCache | None, component: Component, now: datetime,
) -> bool:
    """Vrai quand le retard dépasse la tolérance d'affichage.

    Le seuil est celui de la phase d'affichage : un livre est *en retard* dès son
    échéance passée, il est **hors de son rythme** quand le retard n'est plus
    couvert par la tolérance — c'est-à-dire quand sa cote ne peut plus s'afficher
    comme un montant. Une seule notion, donc, pour la mesure et pour l'écran.
    """
    return component_late_by(cache, component, now) > DISPLAY_TOLERANCE


def out_of_rhythm(cache: BookMarketCache | None, now: datetime) -> bool:
    """Vrai si une composante du livre dépasse son échéance au-delà de la tolérance."""
    if cache is None:
        return False
    return any(component_out_of_rhythm(cache, component, now) for component in COMPONENTS)


# --------------------------------------------------------------------------- #
# Péremption à l'affichage
# --------------------------------------------------------------------------- #


def cote_success_at(cache: BookMarketCache | None) -> datetime | None:
    """Date du dernier relevé réussi de la cote."""
    return component_success_at(cache, "current")


def cote_age(cache: BookMarketCache | None, now: datetime) -> timedelta | None:
    """Âge du dernier relevé réussi de la cote ; ``None`` s'il n'y en a jamais eu."""
    successful = cote_success_at(cache)
    if successful is None:
        return None
    age = now - successful
    return age if age > timedelta(0) else timedelta(0)


def cote_cadence(cache: BookMarketCache | None) -> timedelta:
    """Rythme applicé au dernier relevé de cote, celui de sa zone."""
    stored = component_state(cache, "current").get("cadence_seconds")
    if isinstance(stored, (int, float)) and not isinstance(stored, bool) and stored > 0:
        return timedelta(seconds=float(stored))
    return component_cadence("current", cote_zone_of(cache))


def cote_is_stale(cache: BookMarketCache | None, now: datetime) -> bool:
    """Vrai quand la cote a dépassé son rythme, tolérance de 48 h comprise.

    Un prix plus vieux que cela ne s'affiche plus comme un montant : il devient
    une phrase qui dit l'âge du dernier relevé, avec sa date.
    """
    age = cote_age(cache, now)
    if age is None:
        return False
    return age > cote_cadence(cache) + DISPLAY_TOLERANCE


def fresh_cote_cents(cache: BookMarketCache | None, now: datetime) -> int | None:
    """Cote affichable en montant, ``None`` quand elle est trop vieille pour l'être."""
    if cache is None or cote_is_stale(cache, now):
        return None
    price = cache.lowest_delivered_price_cents
    return price if isinstance(price, int) and not isinstance(price, bool) and price > 0 else None


@dataclass(frozen=True)
class CoteDisplay:
    """Ce qu'un écran peut dire d'une cote : son montant, et l'âge qui va avec.

    Les deux voyagent ensemble parce qu'ils ne se décident pas séparément : un
    montant périmé ne s'affiche pas, et un âge sans montant n'a de sens que
    rapporté à celui qu'il périme. L'écran ne recalcule ni cadence ni tolérance ;
    il reçoit le fait et choisit sa phrase, qui vit dans ``app.bot.labels``.
    """

    cents: int | None
    age: timedelta | None
    since: datetime | None
    stale: bool


def cote_display(cache: BookMarketCache | None, now: datetime | None = None) -> CoteDisplay:
    """Le montant de la cote et son âge, lus une seule fois pour tout l'affichage."""
    moment = now or datetime.now(UTC)
    price = cache.lowest_delivered_price_cents if cache is not None else None
    cents = (
        price if isinstance(price, int) and not isinstance(price, bool) and price > 0 else None
    )
    return CoteDisplay(
        cents=cents,
        age=cote_age(cache, moment),
        since=cote_success_at(cache),
        stale=cote_is_stale(cache, moment),
    )


# --------------------------------------------------------------------------- #
# Écriture d'une échéance
# --------------------------------------------------------------------------- #


def _history_forced_early(cache: BookMarketCache, now: datetime) -> None:
    """Rend l'historique dû immédiatement : une baisse notable vient d'être vue."""
    state: dict[str, Any] = dict(cache.refresh_state or {})
    entry = dict(component_state(cache, "history"))
    entry["due_at"] = now.isoformat()
    entry.setdefault("cadence_seconds", HISTORY_CADENCE.total_seconds())
    state["history"] = entry
    cache.refresh_state = state


def record_component(
    cache: BookMarketCache,
    component: Component,
    *,
    succeeded: bool,
    now: datetime,
    price_cents: int | None = None,
    ceiling_cents: int | None = None,
) -> None:
    """Inscrit le résultat d'une lecture dans l'échéance de sa composante.

    Succès : l'échéance repart du relevé qui vient d'avoir lieu, au rythme de la
    zone recalculée pour la cote (une baisse de plus de 30 % rend en plus
    l'historique dû immédiatement).

    Échec : l'échéance repart de maintenant, au même rythme. Le livre reprend sa
    place dans la file ordinaire au lieu de doubler son rythme ; aucun barreau
    de relance ne s'intercale.
    """
    state = dict(cache.refresh_state or {})
    previous = state.get(component)
    entry = dict(previous) if isinstance(previous, dict) else {}
    failures = 0 if succeeded else int(entry.get("failures", 0)) + 1
    zone = cote_zone_of(cache)
    if component == "current" and succeeded:
        zone = cote_zone(price_cents, ceiling_cents)
    cadence = component_cadence(component, zone)
    entry["failures"] = failures
    entry["cadence_seconds"] = cadence.total_seconds()
    entry["due_at"] = (now + cadence).isoformat()
    if succeeded:
        entry["last_success_at"] = now.isoformat()
    if component == "current":
        entry["zone"] = zone
        if succeeded and price_cents is not None:
            previous_price = component_state(cache, "current").get("price_cents")
            entry["price_cents"] = price_cents
            if (
                isinstance(previous_price, int) and not isinstance(previous_price, bool)
                and previous_price > 0
                and price_cents < previous_price * (1 - BIG_DROP_RATIO)
            ):
                state[component] = entry
                cache.refresh_state = state
                _history_forced_early(cache, now)
                cache.next_refresh_at = next_component_due(cache, now)
                return
    state[component] = entry
    cache.refresh_state = state
    cache.next_refresh_at = next_component_due(cache, now)

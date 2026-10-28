"""Décision de cote pour l'aperçu de création : une offre retenue, ou un état nommé.

L'aperçu annonçait « Cote indisponible / Dernière offre connue : Non renseigné » là
où la collecte avait des offres : aucun état ne manquait, c'est la décision qui
n'était pas prise. Ce module la prend, sans I/O, sur des offres déjà collectées.

**Quatre états, jamais « Non renseigné »** — « Non renseigné » décrit une donnée
absente de la fiche, pas une recherche qui a échoué :

- `cote_trouvee` : une offre a été retenue, avec son prix total port compris ;
- `aucune_offre` : la collecte a abouti et n'a rien rendu ;
- `erreur_recuperation` : la collecte n'a pas abouti (réseau, source muette) ;
- `offres_ecartees_par_les_filtres` : des offres existent, aucune ne passe le
  plafond ou les états acceptés — avec leur nombre et la moins chère écartée, pour
  que l'écart soit vérifiable au lieu d'être un mur.

**Le plafond de prix ne se modifie jamais.** Il filtre la cote, il ne la plafonne
pas : retenir une offre au-dessus du plafond reviendrait à créer une alerte dont
le prix maximum ment sur ce qu'elle surveille. Si l'écartement vient de lui, le
message le dit.

**Deux vocabulaires d'état, distingués à dessein.** Les offres portent les codes du
référentiel dont elles viennent (NAE, NSE, TBE — neuf avec étiquette, neuf sans
étiquette, très bon état), ceux qu'accepte une alerte. L'historique d'un marchand
porte **ses** libellés (« Très bon état + », « Comme neuf + ») : ils sont conservés
tels quels pour l'affichage, parce que « + » veut dire quelque chose pour ce
marchand et qu'un code du référentiel l'effacerait. Les deux ne se comparent donc
jamais entre eux.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

# États d'offre acceptés pour une cote de création, en codes du référentiel.
# Un tome neuf sous blister et un exemplaire « comme neuf » sont de vrais achats
# possibles du volume : les écarter parce qu'ils ne sont pas de l'occasion
# refuserait la seule offre qui existe, comme sur un tome récent.
ETATS_COTE_ACCEPTES: frozenset[str] = frozenset({"NAE", "NSE", "TBE"})

# Libellés d'historique qu'un marchand publie et qu'on accepte de citer tel quel.
ETATS_HISTORIQUE_ACCEPTES: frozenset[str] = frozenset({"Très bon état +", "Comme neuf +"})

# Ordre de citation à prix égal : le palier « très bon état » d'abord, celui dont
# la cote du jour parle quand elle retient un TBE. Deux paliers au même prix ne
# sont pas deux réponses équivalentes — citer le plus favorable des deux ferait
# dire au plus bas douze mois autre chose que le prix qu'il a réellement atteint.
ETATS_HISTORIQUE_PRIORITE: tuple[str, ...] = ("Très bon état +", "Comme neuf +")

CoteState = Literal[
    "cote_trouvee",
    "aucune_offre",
    "erreur_recuperation",
    "offres_ecartees_par_les_filtres",
]

PrixBasState = Literal[
    "prix_bas_trouve",
    "aucun_releve_12_mois",
    "erreur_recuperation_historique",
]


@dataclass(frozen=True)
class OffreCote:
    """Une offre comparable : un marchand, un état codé, un prix total port compris.

    `etat` est `None` quand la source ne le conserve pas — la cote en base garde le
    prix retenu et son marchand, pas le libellé d'état qui l'a qualifié. Un état
    absent n'est pas un état refusé : c'est la règle déjà posée par
    `app.domain.rules.market_offer` (« on ne rejette jamais une offre d'occasion
    simplement parce que le marchand ne détaille pas l'exemplaire »). Un état
    déclaré, lui, se compare aux états acceptés.
    """

    marchand: str
    etat: str | None
    total_cents: int
    url: str | None = None


@dataclass(frozen=True)
class CoteOutcome:
    """Résultat de la sélection, état nommé et offre retenue s'il y en a une.

    `derniere_offre_cents` n'est pas une cote du jour : c'est le prix retenu lors
    d'une collecte précédente, cité quand celle du jour n'a rien donné. Le taire
    effacerait un prix connu ; le présenter comme l'offre du jour mentirait sur sa
    fraîcheur. Il accompagne donc l'absence, il ne la remplace pas.
    """

    state: CoteState
    offre: OffreCote | None = None
    offres_examinees: int = 0
    moins_chere_ecartee_cents: int | None = None
    derniere_offre_cents: int | None = None

    @property
    def found(self) -> bool:
        return self.state == "cote_trouvee"


def select_cote_offer(
    offres: Sequence[OffreCote] | None,
    etats_acceptes: Iterable[str] = ETATS_COTE_ACCEPTES,
    prix_max: int | None = None,
    derniere_offre_cents: int | None = None,
) -> CoteOutcome:
    """Retient la moins chère des offres admissibles, ou nomme pourquoi il n'y en a pas.

    `offres is None` distingue l'échec de collecte d'une collecte vide : une source
    muette n'est pas une absence d'offre, et les confondre rejouerait le mensonge
    que ce module corrige. `prix_max` n'est lu que pour filtrer, jamais réécrit.
    `derniere_offre_cents` ne sert qu'à documenter l'absence : une cote trouvée ne
    le cite jamais, puisqu'elle a sa propre offre.
    """
    if offres is None:
        return CoteOutcome("erreur_recuperation", derniere_offre_cents=derniere_offre_cents)
    examined = tuple(offres)
    if not examined:
        return CoteOutcome("aucune_offre", derniere_offre_cents=derniere_offre_cents)
    accepted_states = frozenset(etats_acceptes)
    admissible = sorted(
        (offre for offre in examined
         if _state_accepted(offre, accepted_states) and offre.total_cents > 0),
        key=lambda offre: (offre.total_cents, offre.marchand),
    )
    if not admissible:
        return CoteOutcome(
            "offres_ecartees_par_les_filtres",
            offres_examinees=len(examined),
            moins_chere_ecartee_cents=_cheapest_cents(examined),
            derniere_offre_cents=derniere_offre_cents,
        )
    cheapest = admissible[0]
    if prix_max is not None and cheapest.total_cents > prix_max:
        return CoteOutcome(
            "offres_ecartees_par_les_filtres",
            offres_examinees=len(examined),
            moins_chere_ecartee_cents=cheapest.total_cents,
            derniere_offre_cents=derniere_offre_cents,
        )
    return CoteOutcome("cote_trouvee", offre=cheapest, offres_examinees=len(examined))


def _state_accepted(offre: OffreCote, accepted_states: frozenset[str]) -> bool:
    """Un état déclaré doit être accepté ; un état absent ne refuse rien."""
    return offre.etat is None or offre.etat in accepted_states


def _cheapest_cents(offres: Sequence[OffreCote]) -> int | None:
    prices = [offre.total_cents for offre in offres if offre.total_cents > 0]
    return min(prices) if prices else None


@dataclass(frozen=True)
class ReleveHistorique:
    """Un point d'historique d'un marchand, port compris, dans son vocabulaire."""

    etat: str
    total_cents: int
    at: str | None = None


@dataclass(frozen=True)
class PrixBasOutcome:
    """Plus bas relevé sur douze mois, ou l'état qui explique son absence."""

    state: PrixBasState
    cents: int | None = None
    etat: str | None = None

    @property
    def found(self) -> bool:
        return self.state == "prix_bas_trouve"


def select_historical_low(
    releves: Sequence[ReleveHistorique] | None,
    etats_acceptes: Iterable[str] = ETATS_HISTORIQUE_ACCEPTES,
) -> PrixBasOutcome:
    """Plus bas relevé sur un an, et l'état marchand qui le qualifie.

    `None` signifie que l'historique n'a pas pu être lu (réponse vide ou invalide) :
    « aucun relevé en douze mois » est un fait daté, une lecture ratée n'en est pas
    un — et c'est justement la distinction qui manquait à l'affichage.
    """
    if releves is None:
        return PrixBasOutcome("erreur_recuperation_historique")
    accepted_states = frozenset(etats_acceptes)
    points = [point for point in releves
              if point.etat in accepted_states and point.total_cents > 0]
    if not points:
        return PrixBasOutcome("aucun_releve_12_mois")
    best = min(points, key=lambda point: (point.total_cents, _history_rank(point.etat)))
    return PrixBasOutcome("prix_bas_trouve", cents=best.total_cents, etat=best.etat)


def _history_rank(etat: str) -> int:
    """Rang de citation d'un libellé d'historique, à prix égal (hors liste : après)."""
    return (
        ETATS_HISTORIQUE_PRIORITE.index(etat)
        if etat in ETATS_HISTORIQUE_PRIORITE
        else len(ETATS_HISTORIQUE_PRIORITE)
    )

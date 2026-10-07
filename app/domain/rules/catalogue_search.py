"""Verdict d'une recherche de catalogue : « personne ne l'a » n'est pas « on ne sait pas ».

Une recherche qui ne rend aucune notice dit deux choses opposées selon ce qui s'est
passé. Quand toutes les autorités interrogées ont répondu, et qu'aucune ne connaît
l'ouvrage, l'absence est une **information** : la veille peut le dire. Quand une
autorité s'est tue — panne, quota, refus — l'absence de notice n'apprend rien, et
l'annoncer ferait passer un silence pour un verdict.

C'est le seul endroit qui tranche : les deux lectures (le verdict d'une recherche,
et celui qu'une alerte porte en base) vivent ici.
"""

from datetime import datetime
from typing import Protocol


class CatalogueVerdictLike(Protocol):
    """Ce qu'un verdict de recherche doit porter pour être jugé.

    ``consulted`` nomme les autorités réellement interrogées, ``unavailable``
    celles dont l'échec est établi, ``notices`` ce qu'elles ont rendu. Le
    protocole évite au domaine de dépendre du service qui produit ce verdict :
    quand le contrat partagé saura composer une série, c'est le même jugement
    qui s'appliquera à ses résultats.

    Les membres sont des propriétés en lecture seule : un verdict est un fait
    constaté, pas un objet qu'un appelant corrige.
    """

    @property
    def consulted(self) -> tuple[str, ...]: ...

    @property
    def unavailable(self) -> tuple[str, ...]: ...

    @property
    def notices(self) -> tuple[object, ...]: ...


def is_catalogue_exhausted(verdict: CatalogueVerdictLike) -> bool:
    """Vrai quand les autorités ont répondu, et qu'aucune ne connaît l'ouvrage.

    Trois conditions, et il en manque une à chaque fois que le fait n'est pas
    établi : au moins une autorité réellement interrogée (sans quoi personne n'a
    rien dit), aucune autorité en échec (un silence ne prouve rien), aucune
    notice retenue (sinon l'ouvrage est identifié, et la question ne se pose plus).
    """
    return bool(verdict.consulted) and not verdict.unavailable and not verdict.notices


def is_uncatalogued(alert: object) -> bool:
    """Vrai si l'alerte porte le verdict « aucune source ne catalogue cet ouvrage ».

    Le verdict est persisté comme la **date** du relevé qui l'a établi : une date,
    pas un drapeau, pour qu'il se lise comme une observation datée et qu'il se
    remplace tout seul au relevé suivant. Un attribut absent ou d'une autre nature
    vaut « pas de verdict » — l'absence de colonne ne doit jamais se confondre avec
    un verdict rendu par une édition antérieure du code.
    """
    return isinstance(getattr(alert, "book_search_exhausted_at", None), datetime)

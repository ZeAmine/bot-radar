"""Détection d'une série multi-tomes à partir d'une notice déjà résolue.

Chercher « Absolute Martian Manhunter » sans préciser de tome a rattaché l'alerte au
**volume 2** : la notice rendue portait « Vol. 2 », et son ISBN contraignait la
recherche Vinted, le matching et la cote à ce seul tome. L'utilisateur, lui, voulait
la série entière.

La preuve est déjà dans la résolution : quand la saisie ne demande aucun numéro et que
la notice en porte un, c'est que le titre désigne une œuvre numérotée et que l'autorité
a répondu par un tome. Rien de plus n'est interrogé — le module est pur, sans I/O, et
`detect_series` ne lève jamais : une liste vide, un titre vide ou une notice sans
numéro donnent simplement « ce n'est pas une série prouvée ».

Ce module ne décide pas de ce qu'on affiche : il rend un fait (`is_series`, les tomes
repérés), la présentation du récap en fait une phrase.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.utils.text import normalize_text

# Marqueur explicite de tome, lu sur un titre **normalisé** (« Vol. 2 » y devient
# « vol 2 », « T.2 » devient « t 2 », « T2 » reste « t2 »). Le `\b` devant la lettre
# évite de confondre le « t » final d'un mot avec un marqueur : « nuit 2 » n'en est
# pas un, « blacksad t2 » en est un.
_MARKER = re.compile(
    r"\b(?:tomes?|t|vols?|volumes?|livres?|books?|parties?|parts?)\s*(\d{1,4})\b"
)

# Numéro collé en fin de titre, sans mot-marqueur : « One Piece 12 ». Borné à trois
# chiffres : une année de publication à quatre chiffres n'est pas un numéro de tome.
_TRAILING_NUMBER = re.compile(r"\s(\d{1,3})$")

# Un préfixe plus court que cela n'identifie rien : « 2 » seul n'est pas un titre.
_MIN_PREFIX_LENGTH = 3


@dataclass(frozen=True)
class _TomeSplit:
    """Découpage d'un titre de notice autour de son numéro, ou l'absence de numéro."""

    prefix: str
    label: str
    number: int


@dataclass(frozen=True)
class SeriesSignal:
    """Ce que la résolution prouve de la nature de l'œuvre.

    `volumes` liste les tomes réellement repérés sur les notices, triés par numéro et
    dédupliqués. Vide, il ne dit rien de plus que `is_series` : aucune preuve.
    """

    is_series: bool = False
    volumes: tuple[str, ...] = field(default_factory=tuple)


def _names_a_tome(normalized_query: str) -> bool:
    """La saisie demande-t-elle un tome précis ? Romains et ordinaux ne comptent pas.

    « Absolute Martian Manhunter Vol. 2 » est une demande de tome ; « … 2 volumes »
    n'en est pas une, faute de numéro (le mot est au pluriel sans chiffre).
    """
    return bool(_MARKER.search(normalized_query) or _TRAILING_NUMBER.search(normalized_query))


def _split_tome(title: str) -> _TomeSplit | None:
    """Sépare le préfixe du numéro d'un titre de notice, ou `None` s'il n'en porte pas.

    Trois formes, dans cet ordre : le mot-marqueur (« Tome 2 », « Vol. 2 », « T2 »),
    puis le numéro collé en fin de titre (« One Piece 12 »). Le préfixe doit rester
    substantiel — un titre qui n'est qu'un numéro ne dit rien.
    """
    normalized = normalize_text(title)
    if not normalized:
        return None

    marker = _MARKER.search(normalized)
    if marker:
        prefix = normalized[: marker.start()].strip()
        if len(prefix) < _MIN_PREFIX_LENGTH:
            return None
        number = int(marker.group(1))
        return _TomeSplit(prefix=prefix, label=f"Tome {number}", number=number)

    trailing = _TRAILING_NUMBER.search(normalized)
    if trailing:
        prefix = normalized[: trailing.start()].strip()
        if len(prefix) < _MIN_PREFIX_LENGTH:
            return None
        number = int(trailing.group(1))
        return _TomeSplit(prefix=prefix, label=f"Tome {number}", number=number)

    return None


def detect_series(*, query_title: str, notices: Sequence[str]) -> SeriesSignal:
    """La résolution montre-t-elle une œuvre numérotée que la saisie n'a pas demandée ?

    `notices` porte les titres réellement rendus par les autorités pour cette saisie.
    Une notice dont le préfixe — le titre débarrassé de son numéro — ne correspond pas
    à la saisie ne prouve rien : c'est une autre œuvre, pas une série.

    Quatre cas, tous rendus sans exception : saisie vide, saisie qui demande un tome,
    notices sans numéro, notice dont le titre numéroté n'est pas celui demandé.
    """
    normalized_query = normalize_text(query_title)
    if not normalized_query or _names_a_tome(normalized_query):
        return SeriesSignal()

    volumes: dict[int, str] = {}
    found = False
    for notice in notices:
        split = _split_tome(notice or "")
        if split is None or split.prefix != normalized_query:
            continue
        found = True
        volumes.setdefault(split.number, split.label)

    if not found:
        return SeriesSignal()
    return SeriesSignal(
        is_series=True,
        volumes=tuple(volumes[number] for number in sorted(volumes)),
    )

"""Hygiène des mots-clés d'une alerte : variantes et exclusions issues du moteur IA.

Rien ne bornait ce que le moteur rendait. Sur un titre porteur d'un numéro
(« Absolute Martian Manhunter Vol. 2 »), il déclinait toutes les écritures du « 2 »
jusqu'à « … 2a », « … 2b », … « … 2z », et il glissait un « 1 » nu dans les exclusions.

Ces deux familles ne sont pas seulement laides, elles sont dangereuses :

- une variante est une **porte de sortie** du matching — une seule suffit à valider une
  annonce, donc une variante dégénérée élargit la surveillance au hasard ;
- une exclusion est un **rejet OU** — une exclusion « 1 » rejette toute annonce qui
  contient un chiffre, donc elle tait des occasions légitimes.

Le nettoyeur est une fonction pure du domaine : aucune I/O, aucune dépendance aux
services. Il est appelé par les chemins d'écriture (module IA, création, édition,
auto-tuning) et, sous forme de prédicats légers, par le filtrage en lecture — une
alerte déjà stockée n'a pas besoin d'une migration pour cesser de mal matcher.

Deux plafonds, une seule idée : une liste courte est une décision, une liste longue
est l'aveu que le moteur n'a pas tranché.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.utils.text import normalize_text

# Nombre maximal de variantes conservées sur une alerte. Au-delà, le moteur énumère.
MAX_VARIANTS = 8

# Nombre maximal d'exclusions conservées sur une alerte.
MAX_EXCLUSIONS = 6

# Un mot-clé plus long qu'un titre vraisemblable n'est pas un mot-clé.
_MAX_TERM_LENGTH = 40

# Un ISBN est un code, pas une orthographe : « les enfants perdus 9782214080627 »
# n'est pas une variante du titre.
_ISBN_LIKE = re.compile(r"\d[\d\-\s]{8,}\d")

# Énumération dégénérée : « 2a », « 10b ». Aucun vendeur n'écrit un titre ainsi ;
# cette famille n'existe que parce que le moteur a décliné un suffixe sur le numéro
# du titre. Les lettres collées au chiffre sont le signal : « 13mini » n'est pas
# concerné (la lettre est suivie d'une autre lettre, donc pas de frontière de mot).
_DEGENERATE_ENUMERATION = re.compile(r"\b\d[a-z]{1,2}\b")

# Mots qui ne désignent qu'un découpage du titre, quand ils suivent celui-ci :
# « one piece tome 2 ». Ils ne portent aucune confusion propre.
_VOLUME_MARKERS = frozenset(
    {
        "t",
        "tome",
        "tomes",
        "vol",
        "vols",
        "volume",
        "volumes",
        "livre",
        "livres",
        "book",
        "books",
        "partie",
        "parties",
        "part",
        "parts",
    }
)

# Un numéro de tome tient sur trois chiffres au plus. Au-delà, c'est un millésime
# (« Blade Runner 2049 ») : une exclusion qui nomme une année peut viser un autre
# ouvrage, elle n'est pas un simple numéro collé au titre.
_MAX_VOLUME_NUMBER_DIGITS = 3


def carries_isbn_like(value: str) -> bool:
    """Le terme contient-il une suite de chiffres assimilable à un ISBN ?

    La suite est volontairement plus large que la clé de contrôle : un ISBN recopié
    avec ou sans tirets doit tomber dans les deux cas.
    """
    return _ISBN_LIKE.sub("", value) != value


def is_degenerate_variant(value: str) -> bool:
    """Variante née d'une énumération de suffixe (« … 2a », « … 10b »).

    Prédicat léger, sans titre : c'est lui que le matching applique en lecture aux
    variantes déjà enregistrées. Il vise la famille qui a réellement été produite.
    """
    normalized = normalize_text(value)
    return bool(normalized) and bool(_DEGENERATE_ENUMERATION.search(normalized))


def is_degenerate_exclusion(value: str) -> bool:
    """Exclusion trop vague pour trancher : un chiffre nu, une lettre seule.

    Le terme doit porter assez de sens pour désigner une confusion. « hs » ou « vf »
    restent exploitables ; « 1 » rejette tout ce qui contient un chiffre.
    """
    stripped = re.sub(r"[^a-z0-9]", "", normalize_text(value))
    if not stripped:
        return True
    return stripped.isdigit() or len(stripped) < 2


def _repeats_the_title(value: str, title: str) -> bool:
    """Le terme et le titre se recouvrent, dans un sens ou dans l'autre.

    Règle de la **variante**. Une variante est une porte d'entrée : un terme qui
    reprend le titre, en tout ou en partie, n'ajoute rien à la veille — le mot-clé
    d'inclusion valide déjà ces annonces-là.
    """
    normalized_term = normalize_text(value)
    normalized_title = normalize_text(title)
    if not normalized_term or not normalized_title:
        return False
    return normalized_term in normalized_title or normalized_title in normalized_term


def names_only_a_volume(value: str, title: str) -> bool:
    """Le terme n'ajoute au titre qu'un numéro de tome : il ne désigne aucune confusion.

    « absolute martian manhunter #2 » n'est pas un faux positif identifié : c'est le
    titre auquel le moteur a collé un numéro. Le matching y verrait un rejet du tome 2 —
    une alerte qui surveille la série se tairait donc sur l'un de ses tomes, exactement
    ce qu'elle veut voir. Famille observée en production : « … #1 » jusqu'à « … #6 ».

    La règle ne peut s'armer que sur un titre **sans numéro**. Dès que le titre en porte
    un, le terme ne le commence plus (« one piece tome 2 » ne commence pas « one piece
    tome 1 ») et l'exclusion reste : elle nomme alors un autre tome que celui visé, ce
    qui est bien un rejet utile.

    Deux limites, assumées : une exclusion qui ne reprend pas le titre (« vol 2 » nu)
    est laissée au jugement de l'utilisateur — la retirer supprimerait une exclusion
    volontaire, le reproche qu'ADR-027 adresse déjà au nettoyeur ; et un millésime à
    quatre chiffres n'est pas un numéro de tome, donc « blade runner 2049 » survit.
    """
    normalized_term = normalize_text(value)
    normalized_title = normalize_text(title)
    if not normalized_term or not normalized_title:
        return False
    if not normalized_term.startswith(normalized_title):
        return False

    remainder = normalized_term[len(normalized_title) :]
    numbers = re.findall(r"\d+", remainder)
    if not numbers or any(len(number) > _MAX_VOLUME_NUMBER_DIGITS for number in numbers):
        return False
    return all(word in _VOLUME_MARKERS for word in re.findall(r"[a-z]+", remainder))


def _swallows_the_title(value: str, title: str) -> bool:
    """Le terme tient *dans* le titre, sans rien y ajouter.

    Règle de l'**exclusion**, et elle est volontairement plus étroite que celle de la
    variante. Une exclusion est une porte de sortie : « deep it coffret » précise le
    titre (« Deep It »), c'est la forme normale d'un rejet utile, et la confondre avec
    un simple recouvrement la supprimait en silence. Une exclusion *plus étroite* que
    le titre (« couleur des choses » pour « La couleur des choses »), elle, rejetterait
    l'annonce cible elle-même : celle-là doit tomber.

    Un titre vide ne doit rien décider. La comparaison est une inclusion de chaîne,
    or `'' in terme` est toujours vrai : sans cette garde, un titre absent — un
    brouillon sans nom, une alerte au nom vide — effacerait toutes ses variantes au
    premier passage du nettoyeur, y compris dans `scripts/clean_alert_keywords.py`.
    """
    normalized_term = normalize_text(value)
    normalized_title = normalize_text(title)
    if not normalized_term or not normalized_title:
        return False
    return normalized_term in normalized_title


class Reason(StrEnum):
    """Pourquoi la règle a écarté un terme — et elle est la seule à pouvoir le dire.

    Le motif est écrit **ici**, au moment où la décision est prise, et non rejoué à
    côté par celui qui la relit. Un filtre ajouté ici rend le motif exact ; un filtre
    ajouté sans motif rendrait une explication fausse, ce qui est pire que pas
    d'explication du tout : c'est sur elle qu'on autorise une écriture destructive.

    `is_degenerate` distingue ce qui est dégénéré **par lui-même** — donc décidable
    sans connaître le titre — de ce qui dépend du titre, d'un doublon ou du plafond.
    C'est cette frontière que `--only-degenerate` de `scripts/clean_alert_keywords.py`
    refuse de franchir, et elle n'a de sens que si elle vit à côté des filtres.
    """

    EMPTY = "vide, ou trop long pour une recherche"
    ISBN = "porte un code d'ouvrage (ISBN)"
    ENUMERATION = "né d'une énumération de suffixe"
    BARE_TOKEN = "chiffre nu ou caractère seul : ne tranche rien"
    TITLE_VARIANT = "reprend le titre sans rien y ajouter"
    TITLE_EXCLUSION = "tient dans le titre : il tairait l'annonce cible"
    VOLUME_ONLY = "n'ajoute au titre qu'un numéro de tome : il tairait le tome voisin"
    DUPLICATE = "doublon d'un terme déjà présent"
    LIMIT = "trop long pour une recherche, ou au-delà du plafond de la famille"

    @property
    def is_degenerate(self) -> bool:
        """Ce motif se décide-t-il sans le titre de l'alerte ?"""
        return self in _DEGENERATE_REASONS


_DEGENERATE_REASONS = frozenset(
    {Reason.EMPTY, Reason.ISBN, Reason.ENUMERATION, Reason.BARE_TOKEN}
)


@dataclass(frozen=True)
class TermRemoval:
    """Un terme écarté, avec sa raison et sa place dans la liste d'entrée.

    `index` est la position d'origine : c'est elle qui permet de rejouer la liste
    sans perdre l'ordre, et de distinguer « ce terme a disparu » de « cette
    occurrence a disparu ». Deux écritures identiques se ressemblent, pas leur rang.
    """

    index: int
    raw: str
    reason: Reason


@dataclass(frozen=True)
class SanitizedValues:
    """Verdict complet d'une famille de mots-clés : ce qui reste, et ce qui part.

    `values` porte la liste exploitable, celle que `sanitize_variants` et
    `sanitize_exclusions` rendent. `removals` porte, pour chaque terme écarté, le
    motif — ce que la règle sait et que la liste cleaned ne dit pas.
    """

    values: list[str]
    kept_indexes: tuple[int, ...]
    removals: tuple[TermRemoval, ...]


@dataclass(frozen=True)
class _Family:
    """Ce en quoi une famille s'écarte de l'autre : plafond, dégénérescence, titre.

    Une variante et une exclusion consultent le titre différemment et dégénèrent
    différemment. Le verdict du titre est une **fonction qui rend son motif** plutôt
    qu'un booléen doublé d'un libellé : une exclusion doit maintenant dire laquelle de
    ses deux façons de ne rien dire face au titre l'a écartée, et deux règles pour un
    seul libellé forceraient à choisir lequel ment.
    """

    max_size: int
    is_degenerate: Callable[[str], bool]
    title_verdict: Callable[[str, str], Reason | None]
    reason_degenerate: Reason


def _variant_title_verdict(value: str, title: str) -> Reason | None:
    """Une variante qui reprend le titre n'ouvre aucune porte de plus."""
    return Reason.TITLE_VARIANT if _repeats_the_title(value, title) else None


def _exclusion_title_verdict(value: str, title: str) -> Reason | None:
    """Les deux façons pour une exclusion de ne rien dire face au titre.

    *Plus étroite* que le titre, elle tairait l'annonce cible (`_swallows_the_title`) ;
    *plus large* d'un simple numéro, elle tairait le tome voisin (`names_only_a_volume`).
    Entre les deux vit la forme utile du rejet, celle qui précise le titre.
    """
    if _swallows_the_title(value, title):
        return Reason.TITLE_EXCLUSION
    if names_only_a_volume(value, title):
        return Reason.VOLUME_ONLY
    return None


VARIANT_FAMILY = _Family(
    max_size=MAX_VARIANTS,
    is_degenerate=is_degenerate_variant,
    title_verdict=_variant_title_verdict,
    reason_degenerate=Reason.ENUMERATION,
)

EXCLUSION_FAMILY = _Family(
    max_size=MAX_EXCLUSIONS,
    is_degenerate=is_degenerate_exclusion,
    title_verdict=_exclusion_title_verdict,
    reason_degenerate=Reason.BARE_TOKEN,
)


def _removal_reason(value: str, title: str, family: _Family) -> Reason | None:
    """Motif d'un écart, ou `None` si le terme passe les filtres.

    L'ordre est celui des filtres, et il est unique : c'est le même enchaînement qui
    décide et qui explique.
    """
    if not value or len(value) > _MAX_TERM_LENGTH or not normalize_text(value):
        return Reason.EMPTY
    if carries_isbn_like(value):
        return Reason.ISBN
    title_verdict = family.title_verdict(value, title)
    if title_verdict is not None:
        return title_verdict
    if family.is_degenerate(value):
        return family.reason_degenerate
    return None


def _clean_values(
    values: list[str] | None,
    title: str,
    family: _Family,
) -> SanitizedValues:
    """Applique les règles communes, rejette les dégénérés, déduplique, plafonne.

    Une seule passe décide **et** motive : il n'existe aucun chemin qui puisse écarter
    un terme sans motif, ni aucun motif qui ne provienne pas de cet enchaînement.

    La déduplication porte sur la forme **normalisée** : deux écritures qui ne
    diffèrent que par un accent ou un espace sont la même variante, et les garder
    toutes les deux ne dit rien de plus au matching. Le plafond, lui, ne tronque plus
    en silence — les termes au-delà sont comptés et nommés.
    """
    cleaned: list[str] = []
    kept_indexes: list[int] = []
    removals: list[TermRemoval] = []
    seen: set[str] = set()

    for index, raw in enumerate(values or []):
        value = str(raw).strip().lower()
        reason = _removal_reason(value, title, family)
        if reason is None:
            key = normalize_text(value)
            if key in seen:
                reason = Reason.DUPLICATE
            elif len(cleaned) >= family.max_size:
                reason = Reason.LIMIT
        if reason is not None:
            removals.append(TermRemoval(index=index, raw=str(raw), reason=reason))
            continue
        seen.add(normalize_text(value))
        cleaned.append(value)
        kept_indexes.append(index)

    return SanitizedValues(
        values=cleaned,
        kept_indexes=tuple(kept_indexes),
        removals=tuple(removals),
    )


def assess_variants(values: list[str] | None, title: str) -> SanitizedValues:
    """Verdict motivé du côté variantes : ce qui reste, et ce qui part et pourquoi.

    Un titre à numéro est la source de l'explosion : le moteur décline le numéro au
    lieu d'écrire le titre. Le suffixe collé au chiffre (`is_degenerate_variant`) est
    le signal qui la coupe ; un terme qui répète le titre est écarté par
    `_repeats_the_title`. Reste à plafonner.
    """
    return _clean_values(values, title, VARIANT_FAMILY)


def assess_exclusions(values: list[str] | None, title: str) -> SanitizedValues:
    """Verdict motivé du côté exclusions : ce qui reste, et ce qui part et pourquoi.

    Le titre est consulté pour écarter une exclusion qui tient *dans* lui
    (`_swallows_the_title`) — celle-là tairait l'annonce cible — et une exclusion qui ne
    fait que lui ajouter un numéro de tome (`names_only_a_volume`) — celle-là tairait le
    tome voisin. Une exclusion qui le précise, elle, est conservée : c'est la forme
    normale d'un rejet.
    """
    return _clean_values(values, title, EXCLUSION_FAMILY)


def sanitize_variants(values: list[str] | None, title: str) -> list[str]:
    """Variantes exploitables : capables de valider une annonce, sans énumération."""
    return assess_variants(values, title).values


def sanitize_exclusions(values: list[str] | None, title: str) -> list[str]:
    """Exclusions qui tranchent réellement : ni chiffre nu, ni lettre seule, plafonnées."""
    return assess_exclusions(values, title).values

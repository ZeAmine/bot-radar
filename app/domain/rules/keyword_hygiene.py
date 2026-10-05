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


def _clean_values(
    values: list[str] | None,
    title: str,
    *,
    max_size: int,
    is_degenerate: Callable[[str], bool],
    title_rule: Callable[[str, str], bool],
) -> list[str]:
    """Applique les règles communes, rejette les dégénérés, déduplique, plafonne.

    `title_rule` est le seul point où les deux familles divergent : ce qui rend une
    variante inutile ne rend pas une exclusion dangereuse, et l'inverse est vrai aussi.

    La déduplication porte sur la forme **normalisée** : deux écritures qui ne
    diffèrent que par un accent ou un espace sont la même variante, et les garder
    toutes les deux ne dit rien de plus au matching.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = str(raw).strip().lower()
        if not value or len(value) > _MAX_TERM_LENGTH:
            continue
        if not normalize_text(value) or carries_isbn_like(value):
            continue
        if title_rule(value, title):
            continue
        if is_degenerate(value):
            continue
        key = normalize_text(value)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
        if len(cleaned) == max_size:
            break
    return cleaned


def sanitize_variants(values: list[str] | None, title: str) -> list[str]:
    """Variantes exploitables : capables de valider une annonce, sans énumération.

    Un titre à numéro est la source de l'explosion : le moteur décline le numéro au
    lieu d'écrire le titre. Le suffixe collé au chiffre (`is_degenerate_variant`) est
    le signal qui la coupe ; un terme qui répète le titre est écarté par
    `_repeats_the_title`. Reste à plafonner.
    """
    return _clean_values(
        values,
        title,
        max_size=MAX_VARIANTS,
        is_degenerate=is_degenerate_variant,
        title_rule=_repeats_the_title,
    )


def _qualifies_the_title_without_being_one(value: str, title: str) -> bool:
    """Les deux façons pour une exclusion de ne rien dire face au titre.

    *Plus étroite* que le titre, elle tairait l'annonce cible (`_swallows_the_title`) ;
    *plus large* d'un simple numéro, elle ne nomme aucune confusion (`names_only_a_volume`).
    Entre les deux vit la forme utile du rejet, celle qui précise le titre.
    """
    return _swallows_the_title(value, title) or names_only_a_volume(value, title)


def sanitize_exclusions(values: list[str] | None, title: str) -> list[str]:
    """Exclusions qui tranchent réellement : ni chiffre nu, ni lettre seule, plafonnées.

    Le titre est consulté pour écarter une exclusion qui tient *dans* lui
    (`_swallows_the_title`) — celle-là tairait l'annonce cible — et une exclusion qui ne
    fait que lui ajouter un numéro de tome (`names_only_a_volume`) — celle-là tairait le
    tome voisin. Une exclusion qui le précise, elle, est conservée : c'est la forme
    normale d'un rejet.
    """
    return _clean_values(
        values,
        title,
        max_size=MAX_EXCLUSIONS,
        is_degenerate=is_degenerate_exclusion,
        title_rule=_qualifies_the_title_without_being_one,
    )

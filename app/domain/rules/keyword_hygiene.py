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
    return normalized_term in normalized_title or normalized_title in normalized_term


def _swallows_the_title(value: str, title: str) -> bool:
    """Le terme tient *dans* le titre, sans rien y ajouter.

    Règle de l'**exclusion**, et elle est volontairement plus étroite que celle de la
    variante. Une exclusion est une porte de sortie : « deep it coffret » précise le
    titre (« Deep It »), c'est la forme normale d'un rejet utile, et la confondre avec
    un simple recouvrement la supprimait en silence. Une exclusion *plus étroite* que
    le titre (« couleur des choses » pour « La couleur des choses »), elle, rejetterait
    l'annonce cible elle-même : celle-là doit tomber.
    """
    return normalize_text(value) in normalize_text(title)


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


def sanitize_exclusions(values: list[str] | None, title: str) -> list[str]:
    """Exclusions qui tranchent réellement : ni chiffre nu, ni lettre seule, plafonnées.

    Le titre n'est consulté que pour écarter une exclusion qui tient *dans* lui
    (`_swallows_the_title`) — celle-là tairait l'annonce cible. Une exclusion qui le
    précise, elle, est conservée : c'est la forme normale d'un rejet.
    """
    return _clean_values(
        values,
        title,
        max_size=MAX_EXCLUSIONS,
        is_degenerate=is_degenerate_exclusion,
        title_rule=_swallows_the_title,
    )

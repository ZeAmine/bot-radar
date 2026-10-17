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

Une variante doit en plus **rester une écriture du même produit**. Les règles qui
existaient ne rejettent que ce qui est dégénéré ; elles laissaient donc passer
« galaxy s24 » pour « Galaxy S23 » (un modèle voisin), « bladerunner » pour
« Blade Runner 2049 » (le millésime perdu, donc l'œuvre de 1982) ou « op » pour
« One Piece » (un sigle, pas une orthographe). Comme une variante suffit à valider une
annonce, chacune de ces écritures élargissait la veille à un autre produit.

Deux règles d'identité, volontairement asymétriques : **les nombres** partout (écriture
et lecture), le **mot reconnaissable** à l'écriture seulement. Rattraper une variante
déjà stockée coûte une annonce manquée ; guider le moteur avant qu'il stocke ne coûte
rien. C'est la frontière qu'ADR-024 avait posée, et que ces deux règles ne franchissent
pas.

Deux plafonds, une seule idée : une liste courte est une décision, une liste longue
est l'aveu que le moteur n'a pas tranché.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.utils.text import contains_normalized_keyword, normalize_text

# Nombre maximal de variantes conservées sur une alerte. Au-delà, le moteur énumère.
MAX_VARIANTS = 8

# Nombre maximal d'exclusions conservées sur une alerte.
MAX_EXCLUSIONS = 6

# Un mot-clé plus long qu'un titre vraisemblable n'est pas un mot-clé.
_MAX_TERM_LENGTH = 40

# Un mot assez long pour qu'un début commun veuille dire quelque chose. En dessous, un
# « op » et un « one » se ressemblent par accident : le préfixe n'est plus une preuve.
_WORD_PREFIX_MIN = 3

# Proximité d'un **mot** admise comme faute de frappe. Le seuil vit sur le mot, jamais sur
# la phrase : comparer « geforce rtx 4070 » à « NVIDIA RTX 4070 » ne rend que 0.5, alors
# que c'est le même produit — une similarité globale rejetterait l'alias de marque.
_WORD_SIMILARITY_MIN = 0.8

# Un ISBN est un code, pas une orthographe : « les enfants perdus 9782214080627 »
# n'est pas une variante du titre.
_ISBN_LIKE = re.compile(r"\d[\d\-\s]{8,}\d")

# Énumération dégénérée : « 2a ». Aucun vendeur n'écrit un titre ainsi ; cette famille
# n'existe que parce que le moteur a décliné un suffixe sur le numéro du titre. Les lettres
# collées au chiffre sont le signal : « 13mini » n'est pas concerné (la lettre est suivie
# d'une autre lettre, donc pas de frontière de mot).
#
# Deux limites connues, laissées telles quelles : le motif exige **un seul** chiffre devant
# les lettres, donc « 10b » (deux chiffres) passe ; et il attrape aussi des suffixes
# d'unité légitimes (« 5g », « 4k », « 3d »). Le faux positif reste rare — il faut un
# token chiffre+lettre, un titre qui ne le recouvre pas, et les mêmes nombres des deux
# côtés (« router 5g » pour « 5G Router ») — et il va dans le sens sûr, celui du retrait.
# Les deux règles d'identité, elles, ne le rattraperaient pas : elles laisseraient passer
# cette variante. Le rendre exact demanderait de rendre ce prédicat dépendant du titre, ce
# qui lui ferait perdre son rôle de frontière titre-indépendante (`Reason.is_degenerate`,
# `--only-degenerate`). À rouvrir si un cas réel se présente.
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


def _digit_runs(value: str) -> tuple[str, ...]:
    """Suites de chiffres d'un texte normalisé, dans l'ordre où elles apparaissent."""
    return tuple(re.findall(r"\d+", normalize_text(value)))


def _is_the_same_word_variably_spelled(candidate: str, reference_word: str) -> bool:
    """Un mot reconnu dans un autre, faute de frappe comprise.

    Trois formes comptent : l'égalité, le préfixe d'au moins trois caractères
    (« onepiece » devant « one »), et la proximité d'écriture au-delà de
    `_WORD_SIMILARITY_MIN` (« peice » devant « piece », « blacksat » devant
    « blacksad »). Les trois sont ce qu'un vendeur écrit à la place du titre.

    Le seuil est court et par **mot**, jamais sur la phrase entière : « op » devant
    « one » vaut 0.4 et reste dehors, ce qu'une similarité globale aurait admis pour une
    phrase presque semblable.
    """
    if candidate == reference_word:
        return True
    shorter, longer = sorted((candidate, reference_word), key=len)
    if len(shorter) < _WORD_PREFIX_MIN:
        return False
    if longer.startswith(shorter):
        return True
    return difflib.SequenceMatcher(None, shorter, longer).ratio() >= _WORD_SIMILARITY_MIN


def _shares_no_distinctive_word(value: str, references: Sequence[str]) -> bool:
    """Le terme n'a-t-il aucun mot reconnaissable en commun avec les références ?

    Règle de la **variante**, et elle ne juge qu'à l'**écriture**. Une variante est censée
    être le même produit écrit autrement : elle doit donc en porter au moins un morceau.
    Un sigle ou un mot sans rapport (« op » pour « One Piece », « amm » pour
    « Absolute Martian Manhunter ») n'ajoute rien — et comme une seule variante suffit à
    valider une annonce, celui-là ouvrirait la veille à tout ce qui contient ces lettres.

    Un alias de marque passe, et il le faut : « geforce rtx 4070 » n'a de commun avec
    « NVIDIA RTX 4070 » que « rtx » et « 4070 », mais c'est le même produit. Une
    similarité globale à la place de cette règle l'aurait rejeté — elle compare 0.5 contre
    0.72 exigé, et l'alerte tech aurait cessé de matcher une annonce qu'elle attrape
    aujourd'hui. C'est la leçon du parcours d'ingestion de bout en bout.

    Volontairement absente du filtrage en lecture : refuser une variante déjà stockée
    ferait manquer une annonce, qui est le plus cher des deux défauts. La règle guide le
    moteur quand il propose, elle ne rattrape pas l'historique.
    """
    value_words = normalize_text(value).split()
    if not value_words:
        return True
    # Aucune référence exploitable — brouillon sans nom, alerte au nom vide — ne décide
    # rien, comme partout dans cette famille : `'' in terme` est toujours vrai.
    if not any(normalize_text(reference).split() for reference in references):
        return False
    for reference in references:
        reference_words = normalize_text(reference).split()
        if any(
            _is_the_same_word_variably_spelled(word, reference_word)
            for word in value_words
            for reference_word in reference_words
        ):
            return False
    return True


def variant_identity_verdict(value: str, references: Sequence[str]) -> Reason | None:
    """Motif pour lequel une variante n'est plus une écriture de ce que l'alerte cherche.

    Une variante est une **porte d'entrée** du matching : une seule suffit à valider une
    annonce quand les mots-clés requis manquent. Elle doit donc rester une écriture de
    l'intention de recherche, pas d'un produit voisin.

    **Une seule règle, et elle est ici** : les nombres. Un nombre est une identité, pas
    une orthographe. « galaxy s24 » n'est pas une écriture de « Galaxy S23 », « blade
    runner 2047 » n'est pas « Blade Runner 2049 », et « bladerunner » en perd le
    millésime — l'alerte surveillerait alors l'œuvre de 1982. Le numéro d'un autre tome
    tombe par la même règle, et un ISBN recopié aussi.

    La comparaison porte sur les **suites de chiffres**, jamais sur les tokens :
    « iphone13 » garde les chiffres de « iPhone 13 », « geforce rtx 4070 » garde ceux de
    « NVIDIA RTX 4070 » — ce sont exactement les écritures qu'une variante existe pour
    rattraper. La variante doit porter les nombres d'**au moins une** référence, et non
    de chacune : une inclusion qualifiée ne doit pas perdre un alias qui aurait, lui,
    conservé les bons nombres.

    Les références sont le titre **et** les mots-clés inclus. Une variante existe pour
    remplacer un mot-clé qui ne matche pas : « blacksad integral » est une écriture
    légitime d'une alerte dont l'inclusion est « blacksad intégrale », même quand le titre
    seul est « Blacksad ». Comparer au seul titre refuserait cette variante utile — une
    annonce manquée, c'est-à-dire le pire des deux défauts.

    Ce verdict est le seul que le **filtrage en lecture** applique. Il est étroit par
    construction : `_shares_no_distinctive_word` et `_repeats_the_title` restent à
    l'écriture, parce que rattraper une variante déjà stockée coûte une annonce.

    Aucune référence exploitable — un brouillon sans nom, une alerte au nom vide — ne
    décide rien : `'' in terme` est toujours vrai, et sans cette garde le premier passage
    du nettoyeur effacerait toutes les variantes, `clean_alert_keywords.py` compris.
    """
    usable = tuple(reference for reference in references if normalize_text(reference))
    if not usable:
        return None
    variant_numbers = _digit_runs(value)
    if any(_digit_runs(reference) == variant_numbers for reference in usable):
        return None
    return Reason.FOREIGN_NUMBER


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
    FOREIGN_NUMBER = "nomme un numéro que ni le titre ni les mots-clés ne portent"
    DRIFTED_WRITING = "aucun mot du titre ni des mots-clés : ce n'est pas une écriture du produit"
    TITLE_EXCLUSION = "tient dans le titre : il tairait l'annonce cible"
    IDENTITY_EXCLUSION = "contredit l’identité bibliographique confirmée"
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

    Une variante et une exclusion consultent les références différemment et dégénèrent
    différemment. Le verdict est une **fonction qui rend son motif** plutôt qu'un booléen
    doublé d'un libellé : une exclusion doit maintenant dire laquelle de ses deux façons
    de ne rien dire face au titre l'a écartée, et deux règles pour un seul libellé
    forceraient à choisir lequel ment.

    Les références sont une tuple, pas un titre : une variante se juge face au titre
    **et** aux mots-clés inclus — c'est ce qui l'autorise à être l'écriture d'une
    inclusion qualifiée —, tandis qu'une exclusion se juge face au titre seul.
    """

    max_size: int
    is_degenerate: Callable[[str], bool]
    title_verdict: Callable[[str, tuple[str, ...]], Reason | None]
    reason_degenerate: Reason
    # Une famille peut distinguer ce que le moteur **propose** de ce que
    # l'utilisateur **écrit**. Le drapeau évite de faire porter au verdict de titre
    # un argument dont la famille des exclusions n'a que faire.
    honours_user_authorship: bool = False


def _variant_title_verdict(value: str, references: tuple[str, ...]) -> Reason | None:
    """Le verdict d'écriture d'une variante : redondante, étrangère, ou hors sujet.

    Trois questions, et c'est le seul endroit qui les pose ensemble :

    1. la variante ajoute-t-elle quelque chose (`_repeats_the_title`) ? Rien qui soit déjà
       couvert par une référence ;
    2. porte-t-elle au moins un mot reconnaissable de la référence
       (`_shares_no_distinctive_word`) ? Un sigle n'est pas une écriture ;
    3. conserve-t-elle les nombres du produit (`variant_identity_verdict`) ?

    Les trois dépendent du titre, donc `--only-degenerate` ne franchit aucune.

    Le filtrage en lecture ne reprend que la question 3. Un terme étranger n'élargit rien
    à lui seul — c'est la 2e qui manque au matching, et elle se juge sans briser de
    variante stockée —, et rattraper les deux premières rouvrirait la porte qu'ADR-024
    avait fermée : on ne rattrape pas une liste silencieusement au prix d'une occasion.
    """
    if any(_repeats_the_title(value, reference) for reference in references):
        return Reason.TITLE_VARIANT
    return _variant_identity_verdict(value, references)


def _variant_identity_verdict(value: str, references: tuple[str, ...]) -> Reason | None:
    """Le verdict d'identité d'une variante, sans la question de redondance.

    Les deux questions qui restent sont celles qui protègent le **produit** : un terme
    doit partager un mot reconnaissable avec la référence et conserver ses nombres.

    Elles se posent à l'identique pour le moteur et pour l'utilisateur. La question de
    redondance (`_repeats_the_title`), elle, ne vaut que pour le moteur : « une variante
    qui ne fait que reprendre le titre n'ajoute rien » est un jugement sur une
    **proposition**, pas sur une **écriture**. Un utilisateur qui saisit « blacksad
    integrale » pour une inclusion « blacksad » décrit exactement ce qu'est une variante
    — la même œuvre sous une autre écriture — et la lui refuser vidait sa saisie en
    silence, ce qui était le défaut.
    """
    if _shares_no_distinctive_word(value, references):
        return Reason.DRIFTED_WRITING
    return variant_identity_verdict(value, references)


def _exclusion_title_verdict(value: str, references: tuple[str, ...]) -> Reason | None:
    """Les deux façons pour une exclusion de ne rien dire face au titre.

    *Plus étroite* que le titre, elle tairait l'annonce cible (`_swallows_the_title`) ;
    *plus large* d'un simple numéro, elle tairait le tome voisin (`names_only_a_volume`).
    Entre les deux vit la forme utile du rejet, celle qui précise le titre.

    Une exclusion se juge face au titre et aux métadonnées confirmées fournies.
    """
    title = references[0] if references else ""
    if any(contains_normalized_keyword(normalize_text(identity), value)
           for identity in references[1:] if identity):
        return Reason.IDENTITY_EXCLUSION
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
    honours_user_authorship=True,
)

EXCLUSION_FAMILY = _Family(
    max_size=MAX_EXCLUSIONS,
    is_degenerate=is_degenerate_exclusion,
    title_verdict=_exclusion_title_verdict,
    reason_degenerate=Reason.BARE_TOKEN,
)


def _removal_reason(
    value: str,
    references: tuple[str, ...],
    family: _Family,
    *,
    from_user: bool = False,
) -> Reason | None:
    """Motif d'un écart, ou `None` si le terme passe les filtres.

    L'ordre est celui des filtres, et il est unique : c'est le même enchaînement qui
    décide et qui explique.

    `from_user` ne change qu'une chose : pour une famille qui honore l'écriture de
    l'utilisateur, la redondance avec le titre cesse d'être un motif de rejet. Les
    autres filtres — vide, ISBN, identité du produit, dégénérescence — sont intacts.
    """
    if not value or len(value) > _MAX_TERM_LENGTH or not normalize_text(value):
        return Reason.EMPTY
    if carries_isbn_like(value):
        return Reason.ISBN
    if family.honours_user_authorship and from_user:
        title_verdict = _variant_identity_verdict(value, references)
    else:
        title_verdict = family.title_verdict(value, references)
    if title_verdict is not None:
        return title_verdict
    if family.is_degenerate(value):
        return family.reason_degenerate
    return None


def _clean_values(
    values: list[str] | None,
    references: tuple[str, ...],
    family: _Family,
    *,
    from_user: bool = False,
) -> SanitizedValues:
    """Applique les règles communes, rejette les dégénérés, déduplique, plafonne.

    Une seule passe décide **et** motive : il n'existe aucun chemin qui puisse écarter
    un terme sans motif, ni aucun motif qui ne provienne pas de cet enchaînement.

    `references` est la tuple sur laquelle le verdict de famille se prononce — le titre
    d'abord, puis ce que l'appelant y ajoute (les mots-clés inclus, pour les variantes).

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
        reason = _removal_reason(value, references, family, from_user=from_user)
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


def assess_variants(
    values: list[str] | None,
    title: str,
    *,
    references: Sequence[str] = (),
    from_user: bool = False,
) -> SanitizedValues:
    """Verdict motivé du côté variantes : ce qui reste, et ce qui part et pourquoi.

    Un titre à numéro est la source de l'explosion : le moteur décline le numéro au lieu
    d'écrire le titre. Le suffixe collé au chiffre (`is_degenerate_variant`) est le signal
    qui la coupe ; un terme qui répète une référence est écarté par `_repeats_the_title` ;
    et les deux règles d'identité (`variant_identity_verdict`) refusent un nombre
    étranger ou un écart d'écriture trop grand. Reste à plafonner.

    `references` reçoit les mots-clés inclus de l'alerte, quand l'appelant les connaît :
    une variante peut être l'écriture d'une inclusion qualifiée et pas seulement du titre.

    `from_user` dit que le terme vient d'être **écrit** par l'utilisateur, et non proposé
    par le moteur. Seule la redondance avec le titre cesse alors d'être un motif : une
    variante est par définition une autre écriture de la même œuvre, et la refuser pour
    ce motif vidait la saisie en silence. Les garde-fous d'identité du produit restent.
    """
    return _clean_values(values, (title, *references), VARIANT_FAMILY, from_user=from_user)


def assess_exclusions(
    values: list[str] | None, title: str, *, author: str | None = None,
    publisher: str | None = None,
) -> SanitizedValues:
    """Verdict motivé du côté exclusions : ce qui reste, et ce qui part et pourquoi.

    Le titre est consulté pour écarter une exclusion qui tient *dans* lui
    (`_swallows_the_title`) — celle-là tairait l'annonce cible — et une exclusion qui ne
    fait que lui ajouter un numéro de tome (`names_only_a_volume`) — celle-là tairait le
    tome voisin. Une exclusion qui le précise, elle, est conservée : c'est la forme
    normale d'un rejet.
    """
    return _clean_values(values, (title, author or "", publisher or ""), EXCLUSION_FAMILY)


def sanitize_variants(
    values: list[str] | None,
    title: str,
    *,
    references: Sequence[str] = (),
    from_user: bool = False,
) -> list[str]:
    """Variantes exploitables : capables de valider une annonce, sans énumération.

    `references` porte les mots-clés inclus de l'alerte : la variante est jugée comme
    une écriture de l'intention de recherche entière, pas du seul titre. `from_user`
    distingue une écriture de l'utilisateur d'une proposition du moteur.
    """
    return assess_variants(
        values, title, references=references, from_user=from_user
    ).values


def sanitize_exclusions(
    values: list[str] | None, title: str, *, author: str | None = None,
    publisher: str | None = None,
) -> list[str]:
    """Exclusions qui tranchent réellement : ni chiffre nu, ni lettre seule, plafonnées."""
    return assess_exclusions(values, title, author=author, publisher=publisher).values

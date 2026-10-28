"""Admission d'un tome de série : ce qui prouve qu'il existe, et ce qui l'écarte.

`add « La Passe-miroir » de Christelle Dabos` a retenu **quatre** tomes, dont deux
qui n'ont jamais paru : la sélection des tomes savait dédoublonner et prioriser,
elle ne savait pas vérifier qu'un livre est sorti. Un ISBN ne prouve donc pas
l'existence d'un tome — la BNF publie une notice dès l'annonce.

**Ce que la zone fixe `100$a` dit, et ne dit pas.** Les positions 0-7 portent la
date d'**entrée de la notice au catalogue** : c'est le jour où la BNF a créé la
notice, pas celui où le livre est sorti. Une réimpression de 1998 cataloguée en
2026 y porte 2026. Cette date ne sert donc **jamais** de date de parution ici : le
champ s'appelle `entry_date`, et le récapitulatif ne l'affiche pas.

La parution se lit ailleurs : position 8 = **type de date**, positions 9-12 =
**Date 1** (l'année de publication déclarée). Seuls certains types autorisent à
conclure ; `ADMITTED_DATE_TYPES` les énumère et le module justifie l'ensemble.
`214$d` (« date de publication, diffusion ») est la seule zone qui puisse porter
un **jour** de parution : quand elle en porte un, il est plus précis que l'année
et il l'emporte.

**Limite assumée, en l'état du 28 octobre 2026.** Une année seule ne porte pas de
jour, donc elle ne sait pas distinguer un livre déjà en librairie d'un livre prévu
plus tard dans la **même** année civile : une notice de type `d` avec Date 1 = 2026
et aucun jour dans `214$d` est admise dès aujourd'hui, y compris si le tome n'est
annoncé que pour novembre 2026. Aujourd'hui, seuls les tomes annoncés portant le
type `u` — la BNF disant elle-même qu'elle ne sait pas — sont écartés ; le type `d`
n'ajoute pas cette nuance. Ce n'est pas un défaut masqué mais le bord de ce que la
zone prouve, et il est daté pour être revu quand la règle changera :
`tests/unit/test_tome_admission.py::test_the_reference_notice_is_admitted_on_its_proven_year_not_on_its_entry_day`
le documente (référence `20260206d2026`, `today` au 27/10/2026, admise sur l'année
seule, `proven_day is None`).

Ce que la règle garantit déjà : aucun tome d'une autre série, d'un autre auteur ou
d'une autre maison ; aucune date inventée (un jour ne se déduit jamais d'une année,
ni les positions 0-7 d'une date de parution) ; aucun tome en dehors du catalogue —
pas d'existence sans notice, pas d'admission sur une vérification impossible. Ce
qu'elle ne garantit pas encore : le jour réel de parution d'un tome de l'année en
cours.

Le module est pur : aucun accès réseau, aucune horloge implicite (`today` est
passé), aucun état. Il ne lève jamais et ne décide pas de l'affichage : il rend un
fait — accepté, ou refusé pour tel motif — que le récapitulatif met en phrase.

**Ce qu'il ne fait pas.** Il ne trie pas les tomes, ne choisit pas d'édition et ne
remplace pas `select_series_volumes` : celui-ci dédoublonne et priorise, celui-ci
décide si une ligne a le droit d'être proposée à l'ajout. Les deux se composent, ils
ne se dupliquent pas.

**« Au moindre doute, refus » (arbitrage commanditaire).** Un motif ne s'invente
pas : une date non prouvée s'écrit « date de parution non prouvée », jamais « tome
à paraître ». Un tome réellement à paraître le dit — il a une date certaine, dans
le futur — et cela reste une information, pas un refus silencieux.

**Un ISBN n'est pas une notice, et deux notices peuvent se contredire.** La BNF
répond parfois deux fois pour le même ISBN — une réédition, une adaptation — et
l'une des deux porte alors une date certaine que l'autre n'a pas. Admettre la plus
favorable faisait entrer la bande dessinée tirée de « La Passe-miroir » comme tome
premier d'une série dont les autres tomes sont des romans de poche.
`admit_tome_notices` juge donc l'ISBN entier, dans cet ordre :

1. une notice qui décrit **une autre édition** — `200$g` « d'après l'œuvre de »
   l'œuvre demandée, ou une collection `225$a` différente de celle que la série
   porte ailleurs — fait refuser l'ISBN en `different_edition`, affiché « édition
   différente détectée » ;
2. des notices qui **se contredisent** sur la maison d'édition, le format ou le
   type de date font refuser l'ISBN en `notices_contradict` : aucune d'elles ne
   prouve plus l'édition demandée, et c'est l'incohérence de la BNF qui est dite ;
3. sinon, une notice favorable suffit et `admit_tome` décide seule.

L'ordre suit la même règle qu'entre les contrôles d'une notice : d'abord ce que la
notice dit de l'ouvrage (une autre édition), ensuite la qualité de la preuve. Une
zone **absente** ne contredit jamais rien : seules deux mentions qui disent des
choses différentes s'opposent.

**Comparer deux maisons d'édition.** « Éditions Gallimard Jeunesse » et « Gallimard
jeunesse » sont la même maison, mais « Gallimard jeunesse » et « Gallimard bande
dessinée » sont deux départements : ne garder que le premier mot distinctif les
rendait égaux, et la BD passait pour le roman.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from typing import Any, Literal

from app.domain.rules.bibliographic_identity import author_tokens
from app.utils.isbn import is_valid_isbn, isbn_from_identifier
from app.utils.text import normalize_text

# Motifs de refus, du plus proche de la donnée au plus proche de l'œuvre.
TomeRejection = Literal[
    "invalid_isbn",
    "no_bnf_notice",
    "bnf_unavailable",
    "date_unproven",
    "not_published_yet",
    "series_mismatch",
    "tome_number_inconsistent",
    "author_mismatch",
    "publisher_mismatch",
    "different_edition",
    "notices_contradict",
]

# Un motif qui dit « cette notice parle d'un autre ouvrage » n'est pas une simple
# absence de preuve : la ligne décrit alors autre chose que le tome demandé, et une
# date finira par la rendre publiable. Elle quitte donc le récapitulatif, au lieu
# d'y attendre. Les autres restent : le tome existe, c'est sa preuve qui manque.
_OTHER_WORK_REASONS = frozenset({
    "invalid_isbn",
    "series_mismatch",
    "tome_number_inconsistent",
    "author_mismatch",
})
# `different_edition` et `notices_contradict` n'en font pas partie : ils ne parlent
# pas d'un autre ouvrage, ils disent que cet ISBN ne prouve pas le tome demandé. La
# ligne reste donc au récapitulatif, nommée avec son motif, jamais rattachée.

# Zone fixe `100$a` : 0-7 = entrée de la notice au catalogue, 8 = type de date,
# 9-12 = Date 1 (année de publication déclarée). Trois positions, trois sens
# différents : les confondre fut l'erreur d'origine.
_ENTRY_SLICE = slice(0, 8)
_DATE_TYPE_INDEX = 8
_YEAR_SLICE = slice(9, 13)

# Types de date de publication admis comme preuve d'une année de parution.
#
# `d` = « monographie publiée en une seule année civile » : l'année de Date 1 est
# alors celle de la parution, et rien d'autre. C'est le seul type qui affirme cela
# d'une monographie terminée.
#
# Exclus, avec leur raison :
# - `u` = inconnue / non renseignée : la notice elle-même dit qu'elle ne sait pas,
#   et c'est le cas des tomes annoncés mais non parus (« La Passe-miroir » 2 et 4) ;
# - `a`, `b`, `g` = ressources continues (publication en cours ou arrêtée) : une
#   année de Date 1 y borne un intervalle, elle ne date pas le volume ;
# - `e` = monographie à plusieurs dates (réimpression, édition composite) : la
#   Date 1 est celle de l'édition reproduite, pas celle du tirage proposé ;
# - `f`, `h`, `i`, `j` = dates détaillées, copyright ou dépôt : ni l'une ni l'autre
#   ne dit que l'exemplaire est en librairie un jour donné ;
# - `m`, `n`, `q`, `r`, `s` = encodages de dates partielles ou douteuses.
ADMITTED_DATE_TYPES = frozenset({"d"})

# Mots de maison d'édition qui ne distinguent rien : « Éditions Pocket » et
# « Pocket » sont la même maison, et un refus pour ce motif serait une erreur.
_HOUSE_NOISE = frozenset({
    "edition", "editions", "ed", "eds", "l", "le", "la", "les", "de", "du", "des",
    "d", "et", "cie", "compagnie", "groupe", "sa", "sas",
})


@dataclass(frozen=True)
class TomeNotice:
    """Ce qu'une notice BNF prouve d'un tome, et rien de plus.

    La notice vient de la BNF par construction : c'est le seul référentiel consulté
    pour une création série. Un tome sans notice est donc un tome sans preuve, et
    l'absence de notice est un motif (`no_bnf_notice`), pas un champ vide.

    `series_statement` est la mention `225$a` : la collection éditoriale. Elle sert
    deux fois, et ce sont deux questions différentes — nomme-t-elle la série
    demandée (`_belongs_to_series`), et désigne-t-elle la collection que les autres
    tomes de cette série portent (`_different_edition`) ? « Fétiche » peut nommer
    une collection de romans, « La passe-miroir » celle de la BD qui les adapte.
    """

    isbn13: str | None
    title: str = ""
    date_type: str | None = None
    entry_date: date | None = None
    declared_year: int | None = None
    publication_date: date | None = None
    declared_publication_year: int | None = None
    authors: tuple[str, ...] = ()
    series_statement: str | None = None
    number: int | None = None
    publisher: str | None = None
    # `200$g` : la mention qui fait d'une notice l'adaptation d'une autre œuvre
    # (« d'après l'œuvre de Christelle Dabos »). Vide sur une notice sans adaptation.
    adaptation_statement: str | None = None
    # Caractéristiques matérielles (`215$c` illustration, `215$d` dimensions) : ce qui
    # distingue un album d'un poche, sans l'étendue qui s'écrit de dix façons.
    illustration: str | None = None
    dimensions: str | None = None

    @property
    def proven_day(self) -> date | None:
        """Jour de parution prouvé, ou `None`.

        Un seul champ le porte : `214$d`, et seulement quand il nomme un jour. Une
        année ne se convertit jamais en 1er janvier — ce serait réinventer le jour
        qu'on refuse d'annoncer.
        """
        return self.publication_date

    @property
    def proven_year(self) -> int | None:
        """Année de parution prouvée, ou `None`.

        Trois règles, dans cet ordre :

        1. un jour prouvé (`214$d`) donne son année — le jour contient l'année ;
        2. hors type de date admis, **rien** ne prouve l'année : une année de dépôt
           légal (`DL 2026`) est déclarée par l'éditeur avant l'impression, et la
           notice des tomes 2 et 4 en porte une alors que son type de date dit
           précisément « inconnue ». La promouvoir en preuve ferait entrer en
           librairie des volumes qui n'y sont pas ;
        3. type admis : `214$d` quand il nomme une année, sinon la Date 1 de `100$a`.
        """
        if self.proven_day is not None:
            return self.proven_day.year
        if self.date_type not in ADMITTED_DATE_TYPES:
            return None
        return self.declared_publication_year or self.declared_year


@dataclass(frozen=True)
class TomeAdmission:
    """Verdict d'admission d'un tome, avec son motif quand il est refusé."""

    reason: TomeRejection | None = None

    @property
    def accepted(self) -> bool:
        return self.reason is None

    @property
    def other_work(self) -> bool:
        """La notice décrit un autre ouvrage : la ligne ne reste pas au récapitulatif."""
        return self.reason in _OTHER_WORK_REASONS


def admit_tome(
    *,
    isbn13: str | None,
    notice: TomeNotice | None,
    series_title: str | None,
    author_hint: str | None = None,
    number: int | None = None,
    reference_publisher: str | None = None,
    reference_collection: str | None = None,
    today: date | None = None,
) -> TomeAdmission:
    """Admet une notice, ou nomme le motif du refus.

    L'ordre des contrôles dit la précision du motif : on établit d'abord **de quel
    ouvrage** parle la notice (série, numéro, auteur, édition), puis seulement si
    sa parution est prouvée. Un refus pour « date non prouvée » ne doit pas
    couvrir une notice qui décrit un autre livre : le motif affiché serait faux,
    alors qu'il est la seule chose que le récapitulatif retient du tome écarté.

    `reference_collection` est la collection que les autres tomes de la série
    attestent ; une notice qui en porte une autre décrit une autre édition du même
    tome, et se refuse en `different_edition` — avant l'éditeur, parce que c'est plus
    précis, et avant la date, parce que ce n'est pas une question de preuve.

    `today` est requis pour juger « déjà paru » : un appelant qui l'omet juge la
    parution contre le jour réel, jamais contre une date de référence inventée.
    """
    code = isbn_from_identifier(isbn13)
    if code is None or not is_valid_isbn(code):
        return TomeAdmission("invalid_isbn")
    if notice is None:
        return TomeAdmission("no_bnf_notice")
    if not _belongs_to_series(notice, series_title):
        return TomeAdmission("series_mismatch")
    if number is not None and notice.number is not None and notice.number != number:
        return TomeAdmission("tome_number_inconsistent")
    if author_hint and not _same_author(notice.authors, author_hint):
        return TomeAdmission("author_mismatch")
    if _different_edition(notice, author_hint=author_hint,
                          reference_collection=reference_collection):
        return TomeAdmission("different_edition")
    if (reference_publisher and notice.publisher
            and not _same_house(notice.publisher, reference_publisher)):
        return TomeAdmission("publisher_mismatch")
    reference_day = today or date.today()
    published_on = notice.proven_day
    if published_on is not None:
        if published_on > reference_day:
            return TomeAdmission("not_published_yet")
        return TomeAdmission()
    published_year = notice.proven_year
    if published_year is None:
        return TomeAdmission("date_unproven")
    if published_year > reference_day.year:
        return TomeAdmission("not_published_yet")
    return TomeAdmission()


def admit_tome_notices(
    *,
    isbn13: str | None,
    notices: Sequence[TomeNotice],
    series_title: str | None,
    author_hint: str | None = None,
    number: int | None = None,
    reference_publisher: str | None = None,
    reference_collection: str | None = None,
    today: date | None = None,
) -> tuple[TomeAdmission, TomeNotice | None]:
    """Admet un ISBN d'après **toutes** ses notices, ou nomme le motif du refus.

    Un ISBN ne répond pas toujours d'une seule notice. Quand la BNF en rend deux —
    une réédition, une adaptation —, elles ne prouvent pas la même édition et ne
    portent pas la même date. Choisir la plus favorable revenait à laisser la
    meilleure notice décider seule : c'est ainsi qu'une bande dessinée est devenue
    le tome 1 d'une série de romans. L'ISBN se juge donc entier, et l'ordre des
    refus dit ce qu'on sait de plus précis :

    1. `different_edition` : une notice décrit une autre édition (adaptation de
       l'œuvre demandée, ou collection différente de celle de la série) ;
    2. `notices_contradict` : les notices se contredisent sur l'éditeur, le format
       ou le type de date — aucune d'elles ne peut plus être la preuve de l'autre ;
    3. une notice admise suffit alors, et c'est elle qui est rendue.

    La notice rendue accompagne le motif du refus comme l'acceptation : le
    récapitulatif n'en lit rien quand le tome est écarté, mais l'appelant garde la
    même forme de retour dans les deux cas.
    """
    code = isbn_from_identifier(isbn13)
    if code is None or not is_valid_isbn(code):
        return TomeAdmission("invalid_isbn"), None
    if not notices:
        return TomeAdmission("no_bnf_notice"), None
    judged = [
        (admit_tome(
            isbn13=code, notice=notice, series_title=series_title,
            author_hint=author_hint, number=number,
            reference_publisher=reference_publisher,
            reference_collection=reference_collection, today=today,
        ), notice)
        for notice in notices
    ]
    for admission, notice in judged:
        if admission.reason == "different_edition":
            return admission, notice
    if notice_contradictions(notices):
        return TomeAdmission("notices_contradict"), judged[0][1]
    for admission, notice in judged:
        if admission.accepted:
            return admission, notice
    return judged[0]


def notice_contradictions(notices: Sequence[TomeNotice]) -> tuple[str, ...]:
    """Axes sur lesquels les notices d'un même ISBN se contredisent.

    Trois axes, nommés dans l'ordre de lecture d'une notice : `publisher` (deux
    mentions qui ne désignent pas la même maison), `format` (illustration ou
    dimensions données des deux côtés et différentes) et `date_type` (deux types
    de date différents).

    Une zone **absente** ne contredit rien : c'est une autre notice qui parle, pas
    un fait contraire. Une notice qui ne dit rien du format ne s'oppose donc pas à
    celle qui le décrit ; seules deux affirmations différentes s'opposent.
    """
    axes: list[str] = []
    publishers = [notice.publisher for notice in notices if notice.publisher]
    if any(not _same_house(first, second) for first, second in combinations(publishers, 2)):
        axes.append("publisher")
    formats = [key for key in (_format_key(notice) for notice in notices) if key]
    if len(set(formats)) > 1:
        axes.append("format")
    date_types = [notice.date_type for notice in notices if notice.date_type]
    if len(set(date_types)) > 1:
        axes.append("date_type")
    return tuple(axes)


def series_reference_collection(groups: Iterable[Sequence[TomeNotice]]) -> str | None:
    """Collection que la série atteste, un groupe de notices par ISBN.

    Un seul tome ne prouve pas la collection d'une série : le tome 1 peut paraître
    dans une collection (« Fétiche ») et les tomes 2 et 4 dans une autre (« Pôle
    fiction »). Deux ISBN qui s'accordent, si — et c'est cette collection-là qui
    sert de référence. Une série dont chaque tome porte une collection différente
    n'en a donc aucune, et rien n'est écarté pour ce motif : l'absence de preuve
    n'accuse personne.

    Rend le nom **normalisé**, parce que « Pöle fiction » et « Pôle fiction » sont
    la même collection écrite deux fois.
    """
    attested: dict[str, set[int]] = {}
    for index, notices in enumerate(groups):
        names = {normalize_text(notice.series_statement) for notice in notices}
        for name in sorted(names):
            if name:
                attested.setdefault(name, set()).add(index)
    corroborated = sorted(
        ((len(indices), name) for name, indices in attested.items() if len(indices) >= 2),
        key=lambda attested_name: (-attested_name[0], attested_name[1]),
    )
    return corroborated[0][1] if corroborated else None


_ADAPTATION_PATTERN = re.compile(
    r"\bd apres (?:l )?(?:(?:œ|oe)uvres?|(?:le )?(?:roman|ouvrage)s?) de\b"
)


def _adapted_author(statement: str | None) -> str | None:
    """Auteur de l'œuvre adaptée, tel que `200$g` le nomme, ou `None`.

    « d'après l'œuvre de Christelle Dabos » rend « Christelle Dabos » ; une mention
    qui n'est pas une adaptation (« texte de… ») rend `None`. La normalisation
    ramène apostrophes et accents à leur forme comparable, sans rien réécrire : la
    ligature « œ » survit à la décomposition Unicode et se cherche donc telle quelle.
    """
    normalized = normalize_text(statement)
    match = _ADAPTATION_PATTERN.search(normalized)
    if not match:
        return None
    return normalized[match.end():].strip() or None


def _different_edition(
    notice: TomeNotice, *, author_hint: str | None, reference_collection: str | None,
) -> bool:
    """La notice décrit-elle une autre édition du même tome ?

    Deux repères, tous deux lus dans la notice :

    - `200$g` « d'après l'œuvre de … » désigne la **même œuvre adaptée**, quand
      l'auteur nommé est celui qu'on cherche : la notice décrit alors la bande
      dessinée tirée du roman, pas le roman — même ISBN, même numéro de tome, autre
      ouvrage. Une adaptation d'un autre auteur est le cas normal d'une série de BD
      adaptées : elle n'est donc pas retenue contre le tome ;
    - une collection (`225$a`) différente de celle que la série porte ailleurs : un
      tome publié dans une autre collection n'est pas l'édition veillée.

    `reference_collection` ne vaut que pour une collection attestée par plusieurs
    tomes, et une collection absente ne contredit rien.
    """
    adapted = _adapted_author(notice.adaptation_statement)
    if adapted and author_hint and _same_author((adapted,), author_hint):
        return True
    collection = normalize_text(notice.series_statement)
    return bool(
        reference_collection and collection
        and collection != normalize_text(reference_collection)
    )


def _format_key(notice: TomeNotice) -> str | None:
    """Caractéristiques matérielles d'une notice : illustration et dimensions.

    L'étendue (« 273 p. ») n'en fait pas partie : elle s'écrit de dix façons et ne
    distingue pas deux éditions. Ce qui sépare un album d'un poche tient dans
    l'illustration et le format en centimètres.
    """
    parts = [normalize_text(part) for part in (notice.illustration, notice.dimensions)]
    written = [part for part in parts if part]
    return " / ".join(written) or None


def _belongs_to_series(notice: TomeNotice, series_title: str | None) -> bool:
    """La notice décrit-elle un tome de la série demandée ?

    Deux preuves valent accord : la mention de série (`225$a`) quand elle est
    présente, et à défaut le titre de la notice (`200`), qui reprend le nom de la
    série suivi du titre du tome (« La passe-miroir les disparus du Clairdelune »).
    Beaucoup de notices n'ont pas de `225` : exiger la seule mention de série
    refuserait des tomes réels pour une zone absente, et non pour un fait contraire.
    Une série non demandée ne contredit rien.
    """
    expected = normalize_text(series_title)
    if not expected:
        return True
    statement = normalize_text(notice.series_statement)
    if statement and statement == expected:
        return True
    return expected in normalize_text(notice.title)


def _same_author(authors: tuple[str, ...], author_hint: str) -> bool:
    """Au moins une mention d'auteur recoupe la saisie, par mots significatifs.

    Même règle que `same_author` du service : les notices BNF nomment les créateurs
    dans un ordre variable (« Dabos, Christelle »), on compare donc des ensembles
    de mots, et l'absence de mention ne vaut pas accord.
    """
    expected = author_tokens(author_hint)
    if not expected:
        return False
    for author in authors:
        actual = author_tokens(author)
        if actual and (actual <= expected or expected <= actual):
            return True
    return False


def _publishing_house(name: str | None) -> frozenset[str]:
    """Mots distinctifs d'une maison d'édition, pour comparer sans le bruit.

    Les formules creuses (« Éditions », « les », « de ») ne distinguent plus rien et
    disparaissent ; ce qui reste distingue en revanche un département de sa maison.
    Ne garder que le **premier** mot distinctif rendait « Gallimard jeunesse » et
    « Gallimard bande dessinée » égaux, et faisait admettre la bande dessinée sous
    l'ISBN du roman : c'est cette égalité-là qu'il fallait casser.
    """
    return frozenset(
        token for token in normalize_text(name).split() if token not in _HOUSE_NOISE
    )


def _same_house(left: str | None, right: str | None) -> bool:
    """Deux mentions désignent-elles la même maison d'édition ?

    « Éditions Gallimard Jeunesse » et « Gallimard Jeunesse » sont la même maison :
    une mention plus courte que l'autre ne la contredit pas (« Gallimard » contre
    « Gallimard jeunesse »). Deux mentions dont chacune ajoute un mot que l'autre
    n'a pas disent deux choses différentes (« jeunesse » contre « bande dessinée »),
    et l'une n'est pas l'autre. Une mention absente ne contredit rien.
    """
    first, second = _publishing_house(left), _publishing_house(right)
    if not first or not second:
        return True
    return first <= second or second <= first


def notice_from_bnf_record(record: dict[str, Any]) -> TomeNotice | None:
    """Projette une notice BNF déjà extraite en `TomeNotice`, ou `None` si illisible.

    Ce module reste pur : il reçoit un dictionnaire déjà lu du XML MarcXchange, il
    ne parse pas de flux. `None` signifie « la notice ne prouve rien d'exploitable »,
    ce que l'admission traduit en `no_bnf_notice` plutôt qu'en admission muette.

    Deux zones de date, deux rôles : `100$a` (`date_code`) donne l'entrée au
    catalogue, le type de date et la Date 1 ; `214$d` (`publication_field`) est la
    seule à pouvoir porter un jour de parution.

    Trois zones disent enfin ce que la notice **est** : `200$g` signale une
    adaptation, et `215$c` / `215$d` ses caractéristiques matérielles — ce qui
    distingue une édition d'une autre sans lire les dates.
    """
    if not isinstance(record, dict):
        return None
    title = record.get("title")
    if not isinstance(title, str):
        return None
    entry = parse_catalogue_entry(record.get("date_code"))
    stated_day, stated_year = parse_publication_field(record.get("publication_field"))
    return TomeNotice(
        isbn13=isbn_from_identifier(record.get("isbn13") or record.get("isbn")),
        title=title,
        date_type=entry.date_type,
        entry_date=entry.entry_date,
        declared_year=entry.declared_year,
        publication_date=stated_day,
        declared_publication_year=stated_year,
        authors=tuple(record.get("authors") or ()),
        series_statement=record.get("series_statement"),
        number=record.get("number"),
        publisher=record.get("publisher"),
        adaptation_statement=record.get("adaptation_statement"),
        illustration=record.get("illustration"),
        dimensions=record.get("dimensions"),
    )


@dataclass(frozen=True)
class CatalogueEntry:
    """Ce que dit la zone fixe `100$a`, et ce qu'elle ne dit pas.

    `entry_date` est la date d'**entrée de la notice au catalogue** — le jour où la
    BNF a créé la notice. Elle n'est jamais une date de parution : une réimpression
    de 1998 cataloguée en 2026 porte 2026 à cet endroit. `declared_year` est la
    Date 1 (positions 9-12), et `date_type` la position 8 qui dit ce que vaut cette
    année.
    """

    date_type: str | None = None
    entry_date: date | None = None
    declared_year: int | None = None


def parse_catalogue_entry(code: str | None) -> CatalogueEntry:
    """Lit la zone fixe `100$a` d'une notice BNF (`20260206d2026…`).

    Trois lectures, aux trois positions qui ont chacune un sens : 0-7 = entrée au
    catalogue, 8 = type de date, 9-12 = Date 1. L'année ne se cherche **jamais**
    par expression régulière dans la zone : celle-ci contient aussi la date
    d'entrée, et prendre l'une pour l'autre fait d'une notice récente un livre paru
    — c'est l'erreur que ce découpage rend impossible.
    """
    if not isinstance(code, str) or len(code) <= _DATE_TYPE_INDEX:
        return CatalogueEntry()
    character = code[_DATE_TYPE_INDEX].strip().lower() or None
    year_text = code[_YEAR_SLICE]
    year = int(year_text) if year_text.isdigit() else None
    entry_text = code[_ENTRY_SLICE]
    entry_date = None
    if len(entry_text) == 8 and entry_text.isdigit():
        try:
            entry_date = date(
                int(entry_text[:4]), int(entry_text[4:6]), int(entry_text[6:]),
            )
        except ValueError:
            entry_date = None
    return CatalogueEntry(character, entry_date, year)


_PUBLICATION_DAY_PATTERNS = (
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"),
    re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})"),
)
_YEAR_PATTERN = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def parse_publication_field(value: str | None) -> tuple[date | None, int | None]:
    """Lit `214$d` : la date de publication telle que la BNF l'écrit.

    Rend `(jour, année)`, l'un ou l'autre pouvant manquer :

    - `« DL 2026 »`, `« 2026 »`, `« [2026] »` → année seule, pas de jour ;
    - `« DL 06/02/2026 »`, `« 2026-02-06 »` → jour réel, qui vaut mieux que l'année.

    Aucune date n'est déduite ni complétée : ce qui n'est pas écrit reste absent,
    et c'est l'appelant qui décide ce que vaut une année sans jour.
    """
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    for pattern in _PUBLICATION_DAY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        try:
            if pattern is _PUBLICATION_DAY_PATTERNS[0]:
                day = date(int(groups[0]), int(groups[1]), int(groups[2]))
            else:
                day = date(int(groups[2]), int(groups[1]), int(groups[0]))
        except ValueError:
            continue
        return day, day.year
    year = _YEAR_PATTERN.search(text)
    return None, int(year.group(1)) if year else None

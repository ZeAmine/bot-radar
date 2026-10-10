"""Règles de matching entre une annonce et une alerte. Aucun I/O."""

import re
from functools import lru_cache

from app.core.constants import (
    DEFAULT_GAMING_EXCLUSIONS,
    DEFAULT_TECH_EXCLUSIONS,
    AlertStatus,
)
from app.db.models.alert import Alert
from app.domain.rules.keyword_hygiene import (
    is_degenerate_exclusion,
    is_degenerate_variant,
    names_only_a_volume,
)
from app.utils.text import contains_keyword, contains_normalized_keyword, normalize_text

MATCH_STEPS = (
    "status",
    "category",
    "price",
    "isbn",
    "default_exclusions",
    "user_exclusions",
    "gaming_language",
    "includes",
    "condition",
)


class MatchResult:
    """Résultat d'évaluation d'une annonce contre une alerte."""

    def __init__(
        self,
        is_match: bool,
        keyword_match_score: int = 0,
        condition_matches: bool = True,
        price_matches: bool = True,
        reason: str = "",
        reason_code: str = "unexplained",
        acceptance_path: str | None = None,
        decisive_details: tuple[tuple[str, int | bool], ...] = (),
        evaluated_count: int = 0,
    ) -> None:
        self.is_match = is_match
        self.keyword_match_score = keyword_match_score
        self.condition_matches = condition_matches
        self.price_matches = price_matches
        self.reason = reason
        self.reason_code = reason_code
        self.acceptance_path = acceptance_path
        self.decisive_details = decisive_details
        self._evaluated_count = evaluated_count

    @property
    def evaluated_steps(self) -> tuple[str, ...]:
        """Étapes atteintes ; sans contrainte applicable, leur garde est néanmoins évaluée."""
        return MATCH_STEPS[: self._evaluated_count]

    @property
    def not_evaluated_steps(self) -> tuple[str, ...]:
        """Étapes non atteintes après le premier rejet, jamais présumées réussies."""
        return MATCH_STEPS[self._evaluated_count :]


DEFAULT_BD_GOODIES_EXCLUSIONS: list[str] = [
    "ex-libris",
    "exlibris",
    "ex libris",
    "marque-page",
    "marquepage",
    "marque page",
    "affiche",
    "poster",
    "figurine",
    "fourreau seul",
    "fourreau vide",
    "coffret vide",
    "boite vide",
    "jaquette seule",
]

# Dictionnaire d'équivalences multilingues pour éradiquer les faux positifs
# transfrontaliers (ES, DE, IT, EN)
MULTILINGUAL_EXCLUSION_MAPPING: dict[str, list[str]] = {
    # Boîtes et emballages vides
    "boite vide": [
        "caja vacia", "solo caja", "caja sin", "nur ovp", "nur huelle",
        "leere ovp", "leere huelle", "solo scatola", "empty box",
    ],
    "boite seule": ["solo caja", "nur ovp", "nur huelle", "solo scatola", "box only"],
    "boitier vide": ["caja vacia", "nur huelle", "solo scatola", "empty case"],
    "boitier seul": ["solo caja", "nur huelle", "solo custodia", "case only"],
    "jaquette seule": ["solo caratula", "nur cover", "solo copertina", "cover only"],

    # Éléments sans jeu / sans cartouche
    "sans jeu": ["sin juego", "ohne spiel", "senza gioco", "no game"],
    "sans cartouche": ["sin cartucho", "ohne modul", "senza cartuccia", "no cartridge"],

    # Accessoires et alimentations
    "chargeur": ["cargador", "netzteil", "ladekabel", "alimentatore", "charger"],
    "chargeur seul": ["solo cargador", "nur netzteil", "charger only"],
    "alimentation": ["cargador", "netzteil", "alimentatore", "power supply"],
    "alim": ["cargador", "netzteil", "alimentatore"],
    "cable seul": ["solo cable", "nur kabel", "solo cavo", "cable only"],
    "coque": ["funda", "huelle", "custodia", "case", "cover"],
    "coque seule": ["solo funda", "nur huelle", "case only"],
    "housse": ["funda", "tasche", "custodia", "pouch", "sleeve"],

    # État défectueux / pour pièces
    "pour pieces": ["para piezas", "defekt", "bastler", "per ricambi", "per parti", "for parts"],
    "hs": ["averiado", "defekt", "non funzionante", "broken"],
    "en panne": ["averiado", "defekt", "guasto"],
    "casse": ["roto", "kaputt", "rotto", "broken"],

    # Vêtements : défauts
    "tache": ["mancha", "fleck", "macchia", "stain"],
    "trou": ["agujero", "loch", "buco", "hole"],
    "fake": ["falso", "falsa", "falschung", "imitacion", "replica"],
    "contrefacon": ["falso", "falschung", "imitacion"],
}


@lru_cache(maxsize=64)
def _expand_exclusions_cached(exclusions: tuple[str, ...]) -> tuple[str, ...]:
    """Extension multilingue mémoïsée, les entrées étant les listes constantes du module.

    L'ordre de sortie est celui de l'entrée, puis celui du dictionnaire d'équivalents.
    Un `set` donnait un ordre dépendant du hash des chaînes — donc différent d'un
    démarrage à l'autre — alors que cette liste décide du motif affiché à l'utilisateur
    (« Mot-clé exclu détecté : … ») dès que plusieurs exclusions correspondent. Mémoïsée
    sans plus, elle rejouait ce tirage à chaque évaluation.
    """
    expanded: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value not in seen:
            seen.add(value)
            expanded.append(value)

    for excl in exclusions:
        add(excl)
    for excl in exclusions:
        norm_excl = normalize_text(excl)
        for fr_key, foreign_list in MULTILINGUAL_EXCLUSION_MAPPING.items():
            if fr_key == norm_excl:
                for foreign in foreign_list:
                    add(foreign)
    return tuple(expanded)


def expand_exclusions_multilingual(exclusions: list[str]) -> list[str]:
    """Étend une liste d'exclusions en y ajoutant automatiquement les équivalents multilingues.

    Les exclusions par défaut de catégorie et les exclusions utilisateur reviennent à
    l'identique à chaque évaluation : l'extension était donc recalculée des centaines
    de fois par passe de matching pour le même résultat. Mémoïsée sur la tuple d'entrée,
    et déterministe : deux exécutions renvoient la même liste, dans le même ordre.
    """
    return list(_expand_exclusions_cached(tuple(exclusions)))


# Marqueurs de langue étrangère pour les jeux vidéo (rejet des jaquettes non-françaises)
FOREIGN_GAME_LANGUAGE_PATTERNS = [
    # Espagnol
    r"\ben\s+castellano\b",
    r"\ben\s+espa[nñ]ol\b",
    r"\bcaja\s+en\s+espa[nñ]ol\b",
    r"\bidioma\s+espa[nñ]ol\b",
    r"\btotalmente\s+en\s+espa[nñ]ol\b",
    r"\bprecintado\b",
    r"\bcomo\s+nuevo\b",
    r"\bhago\s+env[ií]os\b",
    r"\bvendo\s+juego\b",
    r"\bversi[oó]n\s+espa[nñ]ola\b",
    r"\bcaja\s+y\s+manual\s+en\s+espa[nñ]ol\b",
    # Allemand
    r"\bauf\s+deutsch\b",
    r"\bdeutsche\s+version\b",
    r"\bdeutsche\s+texte\b",
    r"\busk\s*(?:0|6|12|16|18)?\b",
    r"\bneuwertig\b",
    r"\bsehr\s+guter\s+zustand\b",
    r"\bversand\s+m[oö]glich\b",
    # Italien
    r"\bin\s+italiano\b",
    r"\blingua\s+italiana\b",
    r"\bscatola\s+in\s+italiano\b",
    r"\bsigillato\b",
    r"\bottime\s+condizioni\b",
    r"\bversione\s+italiana\b",
    r"\bgioco\s+per\s+nintendo\b",
]

# Marqueurs explicites attestant d'une édition française PAL-FR
FRENCH_PAL_FR_PATTERNS = [
    r"\bpal\s+fr\b",
    r"\bversion\s+fran[cç]aise\b",
    r"\bvf\b",
    r"\bbo[iî]te\s+en\s+fran[cç]ais\b",
    r"\bjeu\s+en\s+fran[cç]ais\b",
    r"\bjaquette\s+fran[cç]aise\b",
    r"\bcomplet\s+fr\b",
]


CONDITION_ALIASES: dict[str, str] = {
    "very_good": "Très bon état",
    "very good": "Très bon état",
    "good": "Bon état",
    "satisfactory": "Satisfaisant",
    "new_with_tag": "Neuf avec étiquette",
    "new with tag": "Neuf avec étiquette",
    "new_without_tag": "Neuf sans étiquette",
    "new without tag": "Neuf sans étiquette",
}

FRANCHISE_PREFIXES: set[str] = {
    "spider man",
    "spiderman",
    "batman",
    "superman",
    "x men",
    "xmen",
    "star wars",
    "avengers",
    "wolverine",
    "hulk",
    "iron man",
    "captain america",
    "daredevil",
    "thor",
    "deadpool",
    "flash",
    "justice league",
}


def isbn_digits(value: str | None) -> str:
    """Retient uniquement les chiffres et le X d'un ISBN."""
    if not value:
        return ""
    return re.sub(r"[^\dX]", "", value.upper())


# Suite de chiffres assez longue pour être un ISBN recopié (avec ou sans tirets).
_ISBN_LIKE_RUN = re.compile(r"\d[\d\-\s]{8,}\d")


def carries_foreign_isbn(variant: str, alert_isbn: str | None) -> bool:
    """Cette variante cite-t-elle un ISBN qui n'est pas celui de l'alerte ?

    Une variante est une porte de sortie du matching : une seule suffit à valider une
    annonce. Un ISBN qui n'appartient pas à l'alerte ne peut donc pas aider à trouver
    CET ouvrage — il ne peut que faire sonner l'alerte sur celui qu'il désigne. C'est
    ce qui arrivait avec « les enfants perdus 9782214080627 », variante tirée de
    l'homonyme de 1989 d'une BD de 2025.

    Les alertes déjà enregistrées gardent leurs anciennes variantes : le filtre les
    protège aussi, sans migration.
    """
    if not alert_isbn:
        return False
    if _ISBN_LIKE_RUN.sub("", variant) == variant:
        return False
    return isbn_digits(variant) != alert_isbn


def usable_variants(alert_variants: list[str] | None, alert_isbn: str | None) -> list[str]:
    """Variantes de l'alerte qui peuvent la valider, ISBN étranger retiré.

    Le filtrage vaut aussi pour les alertes **déjà enregistrées** : une variante
    dégénérée (« … 2a ») héritée d'une ancienne énumération du moteur ne peut plus
    valider d'annonce, sans migration ni réécriture en base.
    """
    return [
        variant
        for variant in (alert_variants or [])
        if variant
        and not carries_foreign_isbn(variant, alert_isbn)
        and not is_degenerate_variant(variant)
    ]


def usable_exclusions(
    alert_exclusions: list[str] | None, alert_title: str | None = None
) -> list[str]:
    """Exclusions capables de trancher : ni chiffre nu, ni lettre seule, ni numéro collé.

    Équivalent de `usable_variants` en lecture : une exclusion « 1 » stockée par une
    ancienne version ne peut plus rejeter toute annonce contenant un chiffre. Le titre
    de l'alerte, quand il est fourni, écarte en plus l'exclusion qui n'est que le titre
    plus un numéro de tome (« … #2 ») : elle tairait le tome voisin d'une série
    surveillée, sans qu'aucune migration ne soit nécessaire pour la rattraper.
    """
    return [
        exclusion
        for exclusion in (alert_exclusions or [])
        if exclusion
        and not is_degenerate_exclusion(exclusion)
        and not (alert_title and names_only_a_volume(exclusion, alert_title))
    ]


def price_is_above_max(
    max_item_price_cents: int | None,
    listing_item_price_cents: int,
) -> bool:
    """True si un plafond est défini et que le prix article le dépasse."""
    return max_item_price_cents is not None and listing_item_price_cents > max_item_price_cents


def condition_is_accepted(
    accepted_conditions: list[str] | None,
    listing_condition: str | None,
) -> bool:
    """True si l'alerte n'a pas de filtre d'état, ou si l'état déclaré est dans la liste."""
    if not accepted_conditions:
        return True
    if not listing_condition:
        return False
    resolved_cond = CONDITION_ALIASES.get(listing_condition.lower(), listing_condition)
    norm_cond = normalize_text(resolved_cond)
    return norm_cond in [normalize_text(c) for c in accepted_conditions]


def first_matching_exclusion(text: str, exclusions: list[str]) -> str | None:
    """Premier mot-clé d'exclusion présent dans le texte, sinon None.

    Le texte est normalisé une fois, puis interrogé exclusion par exclusion : le
    renormaliser à chaque mot-clé donnait le même résultat (la normalisation est
    idempotente) pour autant de traversées NFKD.
    """
    if not text or not exclusions:
        return None
    normalized = normalize_text(text)
    for raw_exclude in exclusions:
        if contains_normalized_keyword(normalized, raw_exclude):
            return raw_exclude
    return None


def default_exclusion_hit(
    title: str,
    exclusions: list[str],
    include_keywords: list[str] | None,
) -> str | None:
    """Exclusion de catégorie sur le titre, ignorée si le mot-clé est explicitement requis."""
    includes = include_keywords or []
    normalized_title = normalize_text(title)
    for c_excl in exclusions:
        if not contains_normalized_keyword(normalized_title, c_excl):
            continue
        # Les mots-clés inclus restent bruts : ils viennent de l'alerte, pas du texte
        # déjà normalisé, et ils sont peu nombreux — c'est le titre qui est long.
        if any(contains_keyword(kw, c_excl) for kw in includes):
            continue
        return c_excl
    return None


def evaluate_listing_against_alert(
    *,
    alert: Alert,
    listing_category_slug: str,
    listing_normalized_title: str,
    listing_normalized_description: str | None,
    listing_condition: str | None,
    listing_item_price_cents: int,
    listing_isbn13: str | None = None,
    listing_raw_url: str | None = None,
) -> MatchResult:
    """Évalue si une annonce correspond aux critères d'une alerte."""
    # 1. Vérification du statut de l'alerte
    if alert.status != AlertStatus.ACTIVE:
        return MatchResult(
            is_match=False,
            reason="Alerte non active",
            reason_code="alert_inactive",
            evaluated_count=1,
        )

    # 2. Vérification de la catégorie
    alert_cat_slug: str | None = None
    if hasattr(alert, "category") and alert.category:
        alert_cat_slug = alert.category.slug
    elif hasattr(alert, "category_slug") and alert.category_slug:
        cat_val = alert.category_slug
        alert_cat_slug = cat_val.value if hasattr(cat_val, "value") else str(cat_val)

    if alert_cat_slug and alert_cat_slug != listing_category_slug:
        return MatchResult(
            is_match=False,
            reason="Catégorie différente",
            reason_code="category_mismatch",
            evaluated_count=2,
        )

    # 3. Vérification du prix maximum de l'article
    if price_is_above_max(alert.max_item_price_cents, listing_item_price_cents):
        return MatchResult(
            is_match=False,
            price_matches=False,
            reason="Prix supérieur au seuil maximum",
            reason_code="price_above_max",
            evaluated_count=3,
        )

    # Texte consultable normalisé une fois : les regex n'enlèvent pas les accents.
    searchable_text = normalize_text(
        f"{listing_normalized_title} {listing_normalized_description or ''}"
    )

    # 4. Règle stricte anti-faux-positifs par ISBN (Priorité absolue)
    # Si l'alerte est configurée avec un ISBN spécifique, la correspondance
    # DOIT être exacte sur cet ISBN.
    hybrid_matched = False
    isbn_route = ""
    if getattr(alert, "isbn", None):
        clean_alert_isbn = isbn_digits(alert.isbn)
        isbn_matched = False

        if listing_isbn13:
            clean_listing_isbn = isbn_digits(listing_isbn13)
            if clean_alert_isbn == clean_listing_isbn:
                isbn_matched = True
                isbn_route = "isbn_field"

        if not isbn_matched and clean_alert_isbn:
            raw_text = (
                f"{listing_normalized_title} "
                f"{listing_normalized_description or ''} "
                f"{listing_raw_url or ''}"
            ).upper()
            raw_clean = re.sub(r"[- ]", "", raw_text)
            if clean_alert_isbn in raw_clean:
                isbn_matched = True
                isbn_route = "isbn_text"

        if not isbn_matched:
            # Fallback hybride pour vendeurs occasionnels sans ISBN renseigné
            hybrid_matched = False
            if listing_category_slug == "bd_comics" and alert.name:
                clean_alert_isbn = isbn_digits(alert.isbn)

                # 1. Découpage du nom de l'alerte
                clean_title = alert.name.split("—")[0].split(" - ")[0].strip()
                norm_work_title = normalize_text(clean_title)
                norm_alert_name = normalize_text(alert.name)

                # Protection franchise comics (Spider-Man, Batman, etc.)
                is_franchise = norm_work_title in FRANCHISE_PREFIXES
                sub_title = (
                    alert.name.split("—")[-1].split(" - ")[-1].strip()
                    if ("—" in alert.name or " - " in alert.name)
                    else ""
                )
                norm_sub_title = normalize_text(sub_title)

                stop_words = {
                    "bd",
                    "tome",
                    "volume",
                    "vol",
                    "edition",
                    "integrale",
                    "le",
                    "la",
                    "les",
                    "de",
                    "du",
                    "des",
                    "un",
                    "une",
                    "en",
                }
                title_sig_words = [
                    w for w in norm_work_title.split() if w not in stop_words and len(w) > 2
                ]

                # Titre complet ou sous-titre spécifique présent dans l'annonce.
                # Le titre de l'annonce et `searchable_text` sont déjà normalisés, et
                # les trois Norm* ci-dessus aussi : ce sont des Confrontations sur du
                # texte normalisé, pas des normalisations.
                title_in_listing = (
                    contains_normalized_keyword(listing_normalized_title, norm_alert_name)
                    or contains_normalized_keyword(searchable_text, norm_alert_name)
                    or (
                        bool(norm_sub_title)
                        and (
                            contains_normalized_keyword(
                                listing_normalized_title, norm_sub_title
                            )
                            or contains_normalized_keyword(searchable_text, norm_sub_title)
                        )
                    )
                    or (
                        not is_franchise
                        and (
                            contains_normalized_keyword(
                                listing_normalized_title, norm_work_title
                            )
                            or contains_normalized_keyword(searchable_text, norm_work_title)
                        )
                    )
                )

                # 2. Vérification stricte des include_keywords : en l'absence d'ISBN textuel,
                # les mots-clés choisis par l'utilisateur (ou une variante) DOIVENT être présents !
                non_isbn_kws = [
                    kw
                    for kw in (alert.include_keywords or [])
                    if isbn_digits(kw) != clean_alert_isbn
                ]
                if non_isbn_kws:
                    user_kws_match = all(
                        contains_normalized_keyword(searchable_text, kw) for kw in non_isbn_kws
                    )
                else:
                    user_kws_match = title_in_listing

                variant_matched = any(
                    contains_normalized_keyword(searchable_text, var)
                    for var in usable_variants(alert.variants, clean_alert_isbn)
                )

                if not user_kws_match and not variant_matched:
                    return MatchResult(
                        is_match=False,
                        keyword_match_score=0,
                        reason=f"ISBN '{alert.isbn}' non trouvé et mots-clés requis absents",
                        reason_code="isbn_keywords_missing",
                        evaluated_count=4,
                    )

                if is_franchise:
                    # Pour les franchises (Spider-Man, Batman...), le préfixe seul ne suffit pas.
                    # Il faut soit le sous-titre d'album, soit que les
                    # mots-clés choisis soient spécifiques
                    has_specific_kws = (
                        user_kws_match
                        and bool(alert.include_keywords)
                        and not all(
                            normalize_text(kw) in FRANCHISE_PREFIXES
                            for kw in alert.include_keywords
                        )
                    )
                    if title_in_listing or has_specific_kws:
                        hybrid_matched = True
                elif title_in_listing or user_kws_match:
                    if (
                        len(title_sig_words) >= 2
                        or (user_kws_match and len(norm_work_title) >= 6)
                    ):
                        # Titre distinctif ou Lot détecté
                        hybrid_matched = True
                    else:
                        # Titre court / 1 mot / chiffres (ex: "1984", "RIP", "Blast")
                        # Exige un contexte BD (mot bd, tome, volume, etc.)
                        # ou la mention de l'auteur / variante
                        author_part = alert.name.split("—")[-1].strip() if "—" in alert.name else ""
                        author_words = [
                            w for w in normalize_text(author_part).split() if len(w) >= 4
                        ]
                        # Mots-clés additionnels (auteur ou variante) présents dans include_keywords
                        for kw in (alert.include_keywords or []):
                            for w in normalize_text(kw).split():
                                if (
                                    len(w) >= 4
                                    and w not in norm_work_title
                                    and w not in author_words
                                ):
                                    author_words.append(w)
                        has_author_or_context = (
                            variant_matched
                            or any(
                                contains_normalized_keyword(searchable_text, aw)
                                for aw in author_words
                            )
                            or bool(
                                re.search(
                                    r"\b(bd|tome|vol|volume|edition|integrale|album|illustre|graphique)\b",
                                    searchable_text,
                                    re.IGNORECASE,
                                )
                            )
                        )
                        if has_author_or_context:
                            hybrid_matched = True

            if not hybrid_matched:
                return MatchResult(
                    is_match=False,
                    keyword_match_score=0,
                    reason=f"ISBN '{alert.isbn}' non trouvé et fallback titre non satisfait",
                    reason_code="isbn_fallback_rejected",
                    evaluated_count=4,
                )

    if hybrid_matched:
        isbn_route = "isbn_hybrid"

    # 5. Vérification des exclusions par défaut selon la catégorie
    if listing_category_slug == "bd_comics":
        category_defaults = DEFAULT_BD_GOODIES_EXCLUSIONS
    elif listing_category_slug == "tech":
        category_defaults = DEFAULT_TECH_EXCLUSIONS
    elif listing_category_slug == "gaming":
        category_defaults = DEFAULT_GAMING_EXCLUSIONS
    else:
        category_defaults = []

    expanded_defaults = expand_exclusions_multilingual(category_defaults)
    default_hit = default_exclusion_hit(
        searchable_text,
        expanded_defaults,
        alert.include_keywords,
    )
    if default_hit:
        return MatchResult(
            is_match=False,
            reason=f"Élément exclu automatiquement par défaut : '{default_hit}'",
            reason_code="default_exclusion",
            evaluated_count=5,
            decisive_details=(("expanded_exclusion_index", expanded_defaults.index(default_hit)),),
        )

    # Vérification des mots-clés exclus configurés par l'utilisateur (avec extension multilingue)
    expanded_user_excludes = expand_exclusions_multilingual(
        usable_exclusions(alert.exclude_keywords, alert.name)
    )
    user_hit = first_matching_exclusion(searchable_text, expanded_user_excludes)
    if user_hit:
        return MatchResult(
            is_match=False,
            reason=f"Mot-clé exclu détecté : '{user_hit}'",
            reason_code="user_exclusion",
            evaluated_count=6,
            decisive_details=(
                ("expanded_exclusion_index", expanded_user_excludes.index(user_hit)),
            ),
        )

    # 5bis. Filtre qualité Jeux Vidéo : Rejet des éditions étrangères non-PAL-FR
    if listing_category_slug == "gaming":
        has_foreign_language = any(
            bool(re.search(pat, searchable_text, re.IGNORECASE))
            for pat in FOREIGN_GAME_LANGUAGE_PATTERNS
        )
        if has_foreign_language:
            has_french_proof = any(
                bool(re.search(pat, searchable_text, re.IGNORECASE))
                for pat in FRENCH_PAL_FR_PATTERNS
            )
            if not has_french_proof:
                return MatchResult(
                    is_match=False,
                    reason="Édition étrangère non-PAL-FR rejetée (jaquette non française)",
                    reason_code="gaming_language_rejected",
                    evaluated_count=7,
                )

    # 6. Vérification des mots-clés inclus (Règle stricte anti-faux-positifs)
    # Si le texte de l'annonce ne contient pas textuellement les chiffres de l'ISBN,
    # les mots-clés de l'alerte doivent obligatoirement être présents !
    isbn_textually_present = False
    clean_alert_isbn = ""
    if getattr(alert, "isbn", None):
        clean_alert_isbn = isbn_digits(alert.isbn)
        raw_text = (
            f"{listing_normalized_title} "
            f"{listing_normalized_description or ''} "
            f"{listing_raw_url or ''}"
        ).upper()
        raw_clean = re.sub(r"[- ]", "", raw_text)
        if clean_alert_isbn and clean_alert_isbn in raw_clean:
            isbn_textually_present = True

    inclusion_route = "unconstrained"
    details: tuple[tuple[str, int | bool], ...] = ()
    if isbn_textually_present:
        inclusion_route = "isbn_text"
        all_includes_match = True
    elif alert.include_keywords:
        inclusion_route = "includes"
        all_includes_match = True
        for keyword_index, raw_kw in enumerate(alert.include_keywords):
            if clean_alert_isbn and isbn_digits(raw_kw) == clean_alert_isbn:
                if not hybrid_matched:
                    all_includes_match = False
                    details = (("missing_include_index", keyword_index),)
                    break
            elif not contains_normalized_keyword(searchable_text, raw_kw):
                all_includes_match = False
                details = (("missing_include_index", keyword_index),)
                break
    else:
        all_includes_match = True

    if not all_includes_match:
        # Vérification des variantes alternatives (OU logique entre variantes)
        variant_matched = False
        for variant_index, raw_variant in enumerate(
            usable_variants(alert.variants, clean_alert_isbn)
        ):
            if contains_normalized_keyword(searchable_text, raw_variant):
                variant_matched = True
                inclusion_route = "variant"
                details = (("usable_variant_index", variant_index),)
                break

        if not variant_matched:
            return MatchResult(
                is_match=False,
                keyword_match_score=0,
                reason="Mots-clés requis non trouvés",
                reason_code="required_keywords_missing",
                evaluated_count=8,
                decisive_details=details,
            )

    matched_keywords_count = len(alert.include_keywords or [])

    # 6. Vérification de l'état si l'alerte a des conditions restreintes
    if not condition_is_accepted(alert.accepted_conditions, listing_condition):
        return MatchResult(
            is_match=False,
            condition_matches=False,
            reason="État de l'article non accepté",
            reason_code="condition_rejected",
            evaluated_count=9,
        )

    # Tous les critères sont validés
    return MatchResult(
        is_match=True,
        keyword_match_score=matched_keywords_count,
        condition_matches=True,
        price_matches=True,
        reason="Match confirmé",
        reason_code="accepted",
        evaluated_count=9,
        acceptance_path=(
            f"{isbn_route}/{inclusion_route}"
            if isbn_route and inclusion_route != "isbn_text"
            else inclusion_route
        ),
        decisive_details=details,
    )

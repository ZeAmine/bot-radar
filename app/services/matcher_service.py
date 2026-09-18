"""Moteur de matching robuste entre les annonces ingérées et les alertes actives."""

import re
import unicodedata

from app.core.constants import (
    DEFAULT_GAMING_EXCLUSIONS,
    DEFAULT_TECH_EXCLUSIONS,
    AlertStatus,
)
from app.db.models.alert import Alert
from app.services.bundle_detector import BundleDetector
from app.utils.text import contains_keyword, normalize_text


class MatchResult:
    """Résultat d'évaluation d'une annonce contre une alerte."""

    def __init__(
        self,
        is_match: bool,
        keyword_match_score: int = 0,
        condition_matches: bool = True,
        price_matches: bool = True,
        reason: str = "",
        is_bundle: bool = False,
    ) -> None:
        self.is_match = is_match
        self.keyword_match_score = keyword_match_score
        self.condition_matches = condition_matches
        self.price_matches = price_matches
        self.reason = reason
        self.is_bundle = is_bundle


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

# Dictionnaire d'équivalences multilingues pour éradiquer les faux positifs transfrontaliers (ES, DE, IT, EN)
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


def expand_exclusions_multilingual(exclusions: list[str]) -> list[str]:
    """Étend une liste d'exclusions en y ajoutant automatiquement les équivalents multilingues."""
    expanded = set(exclusions)
    for excl in exclusions:
        norm_excl = normalize_text(excl)
        for fr_key, foreign_list in MULTILINGUAL_EXCLUSION_MAPPING.items():
            if fr_key == norm_excl or fr_key in norm_excl or norm_excl in fr_key:
                expanded.update(foreign_list)
    return list(expanded)


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
        return MatchResult(is_match=False, reason="Alerte non active")

    # 2. Vérification de la catégorie
    alert_cat_slug = alert.category.slug if alert.category else None
    if alert_cat_slug and alert_cat_slug != listing_category_slug:
        return MatchResult(is_match=False, reason="Catégorie différente")

    # Détection de lot / pack avantageux
    bundle_res = BundleDetector.detect(
        listing_normalized_title, listing_normalized_description or ""
    )
    is_bundle = bundle_res.is_bundle

    # 3. Vérification du prix maximum de l'article seul ou du lot
    if alert.max_item_price_cents is not None:
        if is_bundle:
            multiplier = min(4, max(2, bundle_res.estimated_item_count or 2))
            max_bundle_price = alert.max_item_price_cents * multiplier
            if listing_item_price_cents > max_bundle_price:
                return MatchResult(
                    is_match=False,
                    price_matches=False,
                    reason=f"Prix du lot supérieur au seuil maximum ({max_bundle_price // 100} €)",
                    is_bundle=True,
                )
        elif listing_item_price_cents > alert.max_item_price_cents:
            return MatchResult(
                is_match=False,
                price_matches=False,
                reason="Prix supérieur au seuil maximum",
                is_bundle=False,
            )

    # Concaténation du texte consultable de l'annonce
    searchable_text = listing_normalized_title
    if listing_normalized_description:
        searchable_text += " " + listing_normalized_description

    # 4. Règle stricte anti-faux-positifs par ISBN (Priorité absolue)
    # Si l'alerte est configurée avec un ISBN spécifique, la correspondance DOIT être exacte sur cet ISBN.
    hybrid_matched = False
    if getattr(alert, "isbn", None):
        clean_alert_isbn = re.sub(r"[^\dX]", "", alert.isbn.upper())
        isbn_matched = False

        if listing_isbn13:
            clean_listing_isbn = re.sub(r"[^\dX]", "", listing_isbn13.upper())
            if clean_alert_isbn == clean_listing_isbn:
                isbn_matched = True

        if not isbn_matched and clean_alert_isbn:
            raw_text = f"{listing_normalized_title} {listing_normalized_description or ''} {listing_raw_url or ''}".upper()
            raw_clean = re.sub(r"[- ]", "", raw_text)
            if clean_alert_isbn in raw_clean:
                isbn_matched = True

        if not isbn_matched:
            # Fallback hybride pour vendeurs occasionnels sans ISBN renseigné
            hybrid_matched = False
            if listing_category_slug == "bd_comics" and alert.name:
                clean_alert_isbn = re.sub(r"[^\dX]", "", (alert.isbn or "").upper())

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

                # Titre complet ou sous-titre spécifique présent dans l'annonce
                title_in_listing = (
                    contains_keyword(listing_normalized_title, norm_alert_name)
                    or contains_keyword(searchable_text, norm_alert_name)
                    or (
                        bool(norm_sub_title)
                        and (
                            contains_keyword(listing_normalized_title, norm_sub_title)
                            or contains_keyword(searchable_text, norm_sub_title)
                        )
                    )
                    or (
                        not is_franchise
                        and (
                            contains_keyword(listing_normalized_title, norm_work_title)
                            or contains_keyword(searchable_text, norm_work_title)
                        )
                    )
                )

                # 2. Vérification stricte des include_keywords : en l'absence d'ISBN textuel,
                # les mots-clés choisis par l'utilisateur (ou une variante) DOIVENT être présents !
                non_isbn_kws = [
                    kw
                    for kw in (alert.include_keywords or [])
                    if re.sub(r"[^\dX]", "", kw.upper()) != clean_alert_isbn
                ]
                if non_isbn_kws:
                    user_kws_match = all(
                        contains_keyword(searchable_text, kw) for kw in non_isbn_kws
                    )
                else:
                    user_kws_match = title_in_listing

                variant_matched = any(
                    contains_keyword(searchable_text, var)
                    for var in alert.variants
                    if var
                )

                if not user_kws_match and not variant_matched:
                    return MatchResult(
                        is_match=False,
                        keyword_match_score=0,
                        reason=f"ISBN '{alert.isbn}' non trouvé et mots-clés requis absents",
                    )

                if is_franchise:
                    # Pour les franchises (Spider-Man, Batman...), le préfixe seul ne suffit pas.
                    # Il faut soit le sous-titre d'album, soit que les mots-clés choisis soient spécifiques
                    has_specific_kws = (
                        user_kws_match
                        and bool(alert.include_keywords)
                        and not all(
                            normalize_text(kw) in FRANCHISE_PREFIXES for kw in alert.include_keywords
                        )
                    )
                    if title_in_listing or has_specific_kws:
                        hybrid_matched = True
                elif title_in_listing or user_kws_match:
                    if (
                        len(title_sig_words) >= 2
                        or (user_kws_match and len(norm_work_title) >= 6)
                        or is_bundle
                    ):
                        # Titre distinctif ou Lot détecté
                        hybrid_matched = True
                    else:
                        # Titre court / 1 mot / chiffres (ex: "1984", "RIP", "Blast")
                        # Exige un contexte BD (mot bd, tome, volume, etc.) ou la mention de l'auteur / variante
                        author_part = alert.name.split("—")[-1].strip() if "—" in alert.name else ""
                        author_words = [
                            w for w in normalize_text(author_part).split() if len(w) >= 4
                        ]
                        # Mots-clés additionnels (auteur ou variante) présents dans include_keywords
                        for kw in (alert.include_keywords or []):
                            for w in normalize_text(kw).split():
                                if len(w) >= 4 and w not in norm_work_title and w not in author_words:
                                    author_words.append(w)
                        has_author_or_context = (
                            variant_matched
                            or any(contains_keyword(searchable_text, aw) for aw in author_words)
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
                )

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
    for c_excl in expanded_defaults:
        if contains_keyword(listing_normalized_title, c_excl):
            is_explicit = any(c_excl in kw.lower() for kw in alert.include_keywords)
            if not is_explicit:
                return MatchResult(
                    is_match=False,
                    reason=f"Élément exclu automatiquement par défaut : '{c_excl}'",
                )

    # Vérification des mots-clés exclus configurés par l'utilisateur (avec extension multilingue)
    expanded_user_excludes = expand_exclusions_multilingual(alert.exclude_keywords)
    for raw_exclude in expanded_user_excludes:
        if contains_keyword(searchable_text, raw_exclude):
            return MatchResult(
                is_match=False,
                reason=f"Mot-clé exclu détecté : '{raw_exclude}'",
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
                )

    # 6. Vérification des mots-clés inclus (Règle stricte anti-faux-positifs)
    # Si le texte de l'annonce ne contient pas textuellement les chiffres de l'ISBN,
    # les mots-clés de l'alerte doivent obligatoirement être présents !
    isbn_textually_present = False
    clean_alert_isbn = ""
    if getattr(alert, "isbn", None):
        clean_alert_isbn = re.sub(r"[^\dX]", "", alert.isbn.upper())
        raw_text = f"{listing_normalized_title} {listing_normalized_description or ''} {listing_raw_url or ''}".upper()
        raw_clean = re.sub(r"[- ]", "", raw_text)
        if clean_alert_isbn and clean_alert_isbn in raw_clean:
            isbn_textually_present = True

    if isbn_textually_present:
        all_includes_match = True
    elif alert.include_keywords:
        all_includes_match = True
        for raw_kw in alert.include_keywords:
            if clean_alert_isbn and re.sub(r"[^\dX]", "", raw_kw.upper()) == clean_alert_isbn:
                if not hybrid_matched:
                    all_includes_match = False
                    break
            elif not contains_keyword(searchable_text, raw_kw):
                all_includes_match = False
                break
    else:
        all_includes_match = True

    if not all_includes_match:
        # Vérification des variantes alternatives (OU logique entre variantes)
        variant_matched = False
        for raw_variant in alert.variants:
            if contains_keyword(searchable_text, raw_variant):
                variant_matched = True
                break

        if not variant_matched:
            return MatchResult(
                is_match=False,
                keyword_match_score=0,
                reason="Mots-clés requis non trouvés",
            )

    matched_keywords_count = len(alert.include_keywords)

    # 6. Vérification de l'état si l'alerte a des conditions restreintes
    condition_matches = True
    if alert.accepted_conditions:
        if not listing_condition:
            condition_matches = False
        else:
            resolved_cond = CONDITION_ALIASES.get(listing_condition.lower(), listing_condition)
            norm_cond = normalize_text(resolved_cond)
            norm_accepted = [normalize_text(c) for c in alert.accepted_conditions]
            if norm_cond not in norm_accepted:
                condition_matches = False

    if not condition_matches:
        return MatchResult(
            is_match=False,
            condition_matches=False,
            reason="État de l'article non accepté",
        )

    # Tous les critères sont validés
    return MatchResult(
        is_match=True,
        keyword_match_score=matched_keywords_count,
        condition_matches=True,
        price_matches=True,
        reason="Match confirmé",
        is_bundle=is_bundle,
    )

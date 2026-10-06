"""Constantes métier. Les enums vivent dans app.domain.enums."""

from app.domain.enums import (
    AlertStatus,
    BookSearchStatus,
    CategorySlug,
    ComparisonStatus,
    ItemCondition,
    MatchConfidence,
)

__all__ = [
    "AlertStatus",
    "BookSearchStatus",
    "CategorySlug",
    "ComparisonStatus",
    "ItemCondition",
    "MatchConfidence",
    "CATEGORY_LABELS",
    "CATEGORY_EMOJIS",
    "STANDARD_CONDITIONS",
]


CATEGORY_LABELS = {
    CategorySlug.BD_COMICS: "Livres",
    CategorySlug.CLOTHES: "Vêtements",
    CategorySlug.TECH: "Électronique",
    CategorySlug.GAMING: "Jeux Vidéo",
}

CATEGORY_EMOJIS = {
    CategorySlug.BD_COMICS: "📚",
    CategorySlug.CLOTHES: "👕",
    CategorySlug.TECH: "💻",
    CategorySlug.GAMING: "🎮",
}

# États standards d'articles d'occasion (une seule définition : ItemCondition)
STANDARD_CONDITIONS = [condition.value for condition in ItemCondition]

# États sélectionnés par défaut par catégorie (libellés de ItemCondition)
_NEW_AND_VERY_GOOD = [
    ItemCondition.NEW_WITH_TAG.value,
    ItemCondition.NEW_WITHOUT_TAG.value,
    ItemCondition.VERY_GOOD.value,
]
DEFAULT_BD_CONDITIONS = list(_NEW_AND_VERY_GOOD)
DEFAULT_CLOTHES_CONDITIONS = list(_NEW_AND_VERY_GOOD)
DEFAULT_TECH_CONDITIONS = [*_NEW_AND_VERY_GOOD, ItemCondition.GOOD.value]
DEFAULT_GAMING_CONDITIONS = [*_NEW_AND_VERY_GOOD, ItemCondition.GOOD.value]

# Mots-clés d'exclusion anti-pièges par défaut
DEFAULT_TECH_EXCLUSIONS = [
    "boitier seul",
    "coque seule",
    "cable seul",
    "pour pieces",
    "hs",
    "en panne",
    "boite vide",
    "netzteil",
    "alimentation",
    "chargeur",
    "alim",
    "power supply",
    "adaptateur",
]

DEFAULT_GAMING_EXCLUSIONS = [
    "boite vide",
    "boite seule",
    "boitier vide",
    "boitier seul",
    "sans jeu",
    "sans cartouche",
    "jaquette seule",
    "pochette seule",
    "carte seule",
    "amiibo",
    "figurine",
    "poster",
    "hs",
    "pour pieces",
    "cassé",
]

DEFAULT_CLOTHES_EXCLUSIONS = [
    "fake",
    "fausse",
    "contrefacon",
    "style vintage",
    "poster",
    "affiche",
]



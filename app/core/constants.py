"""Constantes métier et énumérations du projet Radar Bot."""

from enum import StrEnum


class CategorySlug(StrEnum):
    BD_COMICS = "bd_comics"
    CLOTHES = "clothes"
    TECH = "tech"
    GAMING = "gaming"


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

# États standards d'articles d'occasion
STANDARD_CONDITIONS = [
    "Neuf avec étiquette",
    "Neuf sans étiquette",
    "Très bon état",
    "Bon état",
    "Satisfaisant",
]

# États sélectionnés par défaut par catégorie
DEFAULT_BD_CONDITIONS = [
    "Neuf avec étiquette",
    "Neuf sans étiquette",
    "Très bon état",
]

DEFAULT_TECH_CONDITIONS = [
    "Neuf avec étiquette",
    "Neuf sans étiquette",
    "Très bon état",
    "Bon état",
]

DEFAULT_GAMING_CONDITIONS = [
    "Neuf avec étiquette",
    "Neuf sans étiquette",
    "Très bon état",
    "Bon état",
]

DEFAULT_CLOTHES_CONDITIONS = [
    "Neuf avec étiquette",
    "Neuf sans étiquette",
    "Très bon état",
]

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


class AlertStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


class MatchConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ComparisonStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    STALE = "stale"

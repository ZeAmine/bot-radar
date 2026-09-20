"""Énumérations métier du domaine."""

from enum import StrEnum


class CategorySlug(StrEnum):
    """Identifiants des catégories d'articles surveillées."""

    BD_COMICS = "bd_comics"
    CLOTHES = "clothes"
    TECH = "tech"
    GAMING = "gaming"


class AlertStatus(StrEnum):
    """Statut du cycle de vie d'une alerte."""

    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


class MatchConfidence(StrEnum):
    """Niveau de certitude de la correspondance d'une annonce."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ComparisonStatus(StrEnum):
    """Statut de la comparaison avec les prix du marché."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class ItemCondition(StrEnum):
    """États standardisés des articles d'occasion."""

    NEW_WITH_TAG = "Neuf avec étiquette"
    NEW_WITHOUT_TAG = "Neuf sans étiquette"
    VERY_GOOD = "Très bon état"
    GOOD = "Bon état"
    SATISFACTORY = "Satisfaisant"

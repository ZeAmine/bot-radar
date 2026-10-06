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


class BookSearchStatus(StrEnum):
    """Statut normalisé d'une recherche bibliographique (Lot 0).

    Ne remplace pas les verdicts métier (`IsbnVerdict`, `BookResolution`) :
    `app/use_cases/book_search.py` définit le passage explicite de l'un à l'autre.
    """

    FOUND = "found"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"

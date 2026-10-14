"""Énumérations métier du domaine."""

from enum import StrEnum


class NotificationState(StrEnum):
    """Progression persistée ; unknown n'autorise jamais un nouvel envoi."""

    READY = "ready"
    SENDING = "sending"
    NOTIFIED = "notified"
    UNKNOWN = "unknown"
    DEFERRED = "deferred"
    ABANDONED = "abandoned"


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


class ProviderQueryStatus(StrEnum):
    """Issue typée d'une interrogation de fournisseur (Lot 1 : Google Books).

    Une liste vide ne dit rien seule : `success` + `items=()` est une absence
    de résultat, tout autre statut est une panne à ne pas confondre avec un
    vide. Seule la BNF reste une autorité ISBN (ADR-014) : ce statut décrit
    le transport, jamais une confirmation d'ouvrage.
    """

    SUCCESS = "success"
    DISABLED = "disabled"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"


class VintedCheckoutStatus(StrEnum):
    """Issue d'une préparation de checkout Vinted (bouton « Acheter »).

    Une préparation réussie rend un lien de checkout ; toute autre issue
    conserve un accès fonctionnel à l'annonce, et son nom dit à l'utilisateur
    laquelle des causes s'est présentée.
    """

    READY = "ready"
    ALREADY_READY = "already_ready"
    BUSY = "busy"
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    NOT_ASSOCIATED = "not_associated"
    SELLER_UNKNOWN = "seller_unknown"
    UNAVAILABLE = "unavailable"
    AUTH_REQUIRED = "auth_required"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK = "network"
    UNEXPECTED = "unexpected"

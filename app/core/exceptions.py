"""Hiérarchie d'exceptions typées pour BotRadar.

Permet d'isoler les erreurs de domaine, de providers externes et de persistance
sans jamais faire fuiter de secrets ni de détails techniques non gérés.
"""


class BotRadarError(Exception):
    """Exception racine de l'application BotRadar."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


# Alias de compatibilité
RadarBotError = BotRadarError


# --- Erreurs de Domaine (Métier pur) ---


class DomainError(BotRadarError):
    """Erreur survenue au niveau des règles de gestion métier."""


class MatchingError(DomainError):
    """Erreur lors de l'évaluation du matching déterministe."""


class InvalidListingError(DomainError):
    """Données d'annonce non conformes aux règles du domaine."""


class InvalidAlertError(DomainError):
    """Configuration d'alerte invalide ou incohérente."""


# --- Erreurs de Fournisseurs / Adaptateurs Externes ---


class ProviderError(BotRadarError):
    """Erreur lors de l'interaction avec un service tiers (API, Scraping, LLM)."""


class MarketPriceProviderError(ProviderError):
    """Erreur lors de l'interrogation d'un comparateur de prix externe."""


# --- Erreurs de Persistance / Repositories ---


class RepositoryError(BotRadarError):
    """Erreur survenue dans la couche d'accès aux données."""


class EntityNotFoundError(RepositoryError):
    """L'entité demandée n'existe pas en base de données."""


class DuplicateEntityError(RepositoryError):
    """L'entité existe déjà (conflit d'unicité)."""

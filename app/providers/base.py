"""Protocole et modèles de données de base pour les fournisseurs de prix de marché."""

import uuid
from collections.abc import Iterable
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any, Literal, Protocol

import httpx

from app.core.constants import MatchConfidence
from app.utils.isbn import is_valid_isbn, isbn_from_identifier, normalize_isbn

requested_components: ContextVar[frozenset[str]] = ContextVar(
    "market_components", default=frozenset({"current", "history"}),
)


@dataclass
class MarketOffer:
    """Offre d'occasion normalisée provenant d'un fournisseur."""

    provider_name: str
    item_price_cents: int
    shipping_price_cents: int | None
    delivered_price_cents: int
    source_url: str
    currency: str = "EUR"
    item_condition: str | None = None
    confidence: MatchConfidence = MatchConfidence.HIGH
    is_available: bool = True
    is_historical: bool = False
    is_comparable: bool = True
    price_includes_shipping: bool = False
    raw_payload: dict[str, Any] | None = None
    observed_at: datetime | None = None
    observation_id: uuid.UUID | None = None


@dataclass(frozen=True)
class CollectionAttempt:
    provider: str
    operation: str = "current"
    status: Literal["success", "partial", "failed", "skipped"] = "success"
    reason: str | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None
    duration_seconds: float = 0.0
    stage: str | None = None
    response_content_type: str | None = None
    response_bytes: int | None = None
    engines_requested: int | None = None
    engines_received: int | None = None
    poll_count: int | None = None
    lookup_seconds: float | None = None


def failed_offers(
    provider: str, *, operation: str = "current", response: httpx.Response | None = None,
    error: Exception | None = None, reason: str | None = None, stage: str | None = None,
) -> "MarketOffers":
    status = response.status_code if response is not None else None
    if response is None and isinstance(error, httpx.HTTPStatusError):
        response = error.response
        status = response.status_code
    if reason is None:
        reason = (
            "rate_limited" if status == 429 else "http_status" if status is not None
            else "timeout" if isinstance(error, (TimeoutError, httpx.TimeoutException))
            else "connection" if isinstance(error, httpx.RequestError)
            else "invalid_response" if isinstance(error, (ValueError, TypeError, KeyError))
            else "internal_error"
        )
    retry_after = None
    if response is not None:
        raw_retry = response.headers.get("Retry-After")
        if raw_retry:
            try:
                retry_after = float(raw_retry)
            except ValueError:
                with suppress(ValueError, TypeError, OverflowError):
                    retry_at = parsedate_to_datetime(raw_retry)
                    retry_after = (retry_at - datetime.now(UTC)).total_seconds()
            if retry_after is not None:
                retry_after = max(0.0, retry_after) if isfinite(retry_after) else None
    return MarketOffers(failed_providers=(provider,), attempts=(CollectionAttempt(
        provider, operation, "failed", reason, status, retry_after, stage=stage,
        response_content_type=(response.headers.get("content-type")
                               if response is not None else None),
        response_bytes=len(response.content) if response is not None else None,
    ),))


def deferred_offers(
    provider: str, *, reason: str, retry_at: datetime | None = None,
    operation: str = "current", emitted_requests: int = 0,
) -> "MarketOffers":
    retry_after = None
    if retry_at is not None:
        retry_after = max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
    return MarketOffers(
        attempts=(CollectionAttempt(
            provider, operation, "skipped", reason, None, retry_after,
        ),),
        emitted_requests=emitted_requests,
    )


class MarketOffers(list[MarketOffer]):
    """Offres collectées et sources en échec ; une liste vide n'est pas un diagnostic."""

    def __init__(
        self,
        offers: Iterable[MarketOffer] = (),
        *,
        failed_providers: tuple[str, ...] = (),
        historical_checked: bool = False,
        attempts: tuple[CollectionAttempt, ...] = (),
        emitted_requests: int = 0,
    ) -> None:
        super().__init__(offers)
        self.attempts = attempts
        self.failed_providers = failed_providers
        self.historical_checked = historical_checked
        self.emitted_requests = emitted_requests


def normalize_search_isbn(raw_isbn: str | None) -> str | None:
    """Retourne un ISBN interrogeable, ou ``None`` s'il est absent ou invalide.

    Les fournisseurs recherchant par ISBN ne savent renvoyer que l'ouvrage codé
    dans ce code : un ISBN mal saisi (faute de frappe, préfixe recomposé) ferait
    coter le mauvais livre sous le bon identifiant. La clé de contrôle est donc
    vérifiée avant toute requête réseau.
    """
    candidate = normalize_isbn(raw_isbn)
    if candidate and is_valid_isbn(candidate):
        return isbn_from_identifier(candidate)
    return None


def _positive_cents(value: Any) -> int | None:
    """Convertit une valeur de payload en centimes strictement positifs, sinon ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        cents = int(float(value))
    except (TypeError, ValueError):
        return None
    return cents if cents > 0 else None


def extract_best_historical_low(offers: list[MarketOffer]) -> int | None:
    """Retourne le plus bas historique certifié sur 1 an parmi les offres fournisseurs.

    Les payloads bruts peuvent exposer ``historical_low_1y_cents`` issu des agrégateurs
    (ex : Chasse-aux-livres). Une valeur absente, non numérique ou nulle est ignorée :
    elle ne doit jamais devenir une cote ni faire échouer le rafraîchissement du livre.
    """
    hist_lows: list[int] = [
        cents
        for offer in offers
        if offer.raw_payload
        for cents in [_positive_cents(offer.raw_payload.get("historical_low_1y_cents"))]
        if cents is not None
    ]
    return min(hist_lows) if hist_lows else None


class BookMarketProvider(Protocol):
    """Interface abstraite que chaque fournisseur de marché doit respecter."""

    name: str

    async def get_offers(
        self,
        *,
        isbn13: str | None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
    ) -> list[MarketOffer]:
        """Interroge le fournisseur et retourne une liste d'offres normalisées."""
        ...

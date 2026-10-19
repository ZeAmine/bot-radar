"""Admission temporelle pure : le premier catalogue d'une alerte reste silencieux."""

from datetime import UTC, datetime
from typing import Any, Protocol


class AlertFreshness(Protocol):
    listing_baseline: dict[str, Any] | None
    created_at: datetime


class ListingFreshness(Protocol):
    source_name: str
    external_id: str
    received_at: datetime
    published_at: datetime | None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def listing_is_new_for_leboncoin(alert: AlertFreshness, listing: ListingFreshness) -> bool:
    """Une annonce Leboncoin est nouvelle si elle est publiée après la création de l'alerte.

    La date de première publication est présente sur les pages observées ; une
    annonce sans date reste silencieuse plutôt que de devenir « nouvelle ». Le
    catalogue étant collecté en continu, aucune empreinte de premier scan n'est
    stockée : la date de publication suffit et ne peut pas désynchroniser les sources.
    """
    if listing.published_at is None:
        return False
    return _utc(listing.published_at) > _utc(alert.created_at)


def listing_is_new_for_alert(alert: AlertFreshness, listing: ListingFreshness) -> bool:
    """Une date absente ne devient jamais une date de publication inventée."""
    if listing.source_name == "leboncoin":
        return listing_is_new_for_leboncoin(alert, listing)
    baseline = alert.listing_baseline
    if not isinstance(baseline, dict) or listing.source_name != "vinted":
        return True
    if baseline.get("state") != "ready":
        return False
    if listing.external_id in baseline.get("external_ids", []):
        return False
    try:
        started = _utc(datetime.fromisoformat(baseline["started_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    if _utc(listing.received_at) <= started:
        return False
    return (listing.published_at is None
            or _utc(listing.published_at) > _utc(alert.created_at))

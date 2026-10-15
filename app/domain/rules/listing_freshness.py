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


def listing_is_new_for_alert(alert: AlertFreshness, listing: ListingFreshness) -> bool:
    """Une date absente ne devient jamais une date de publication inventée."""
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

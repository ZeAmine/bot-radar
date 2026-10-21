"""Reprise d'un livre dont la collecte a été refusée avant toute requête HTTP.

Un refus prononcé par le régulateur CAL — quota d'admission, fenêtre de
concurrence, circuit ouvert, stockage indisponible — n'est pas une panne du
fournisseur : aucune requête n'est partie, aucune réponse n'a été reçue. Le
compter comme un échec ferait escalader le recul du livre jusqu'à sept jours
pour un incident qui ne le concerne pas.
"""

from datetime import UTC, datetime, timedelta

from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import CollectionAttempt
from app.providers.cal_governor import (
    CAL_ADMINISTRATIVE_RETRY_SECONDS,
    CAL_PROBE_TTL_SECONDS,
    CAL_STORAGE_RETRY_SECONDS,
    administrative_deferral,
)

ADMINISTRATIVE_STATUS = "deferred"


def administrative_attempts(attempts: tuple[CollectionAttempt, ...]) -> list[CollectionAttempt]:
    return [a for a in attempts if a.status == "skipped" and administrative_deferral(a.reason)]


def administrative_deferral_only(attempts: tuple[CollectionAttempt, ...]) -> bool:
    """Vrai si la collecte n'a produit que des refus administratifs, sans requête."""
    if not attempts:
        return False
    return len(administrative_attempts(attempts)) == len(attempts)


def administrative_retry_at(
    attempts: tuple[CollectionAttempt, ...], now: datetime,
) -> datetime:
    requested: list[float] = []
    for attempt in administrative_attempts(attempts):
        if attempt.reason in {"storage_unavailable", "concurrency"}:
            requested.append(CAL_STORAGE_RETRY_SECONDS)
        elif attempt.reason == "probe_in_flight":
            requested.append(CAL_PROBE_TTL_SECONDS)
        elif attempt.retry_after_seconds is not None:
            requested.append(attempt.retry_after_seconds)
        else:
            requested.append(CAL_ADMINISTRATIVE_RETRY_SECONDS)
    return now + timedelta(seconds=max(requested, default=CAL_ADMINISTRATIVE_RETRY_SECONDS))


def record_administrative_deferral(
    cache: BookMarketCache, attempts: tuple[CollectionAttempt, ...], now: datetime,
) -> datetime:
    """Garde une reprise datée sans inventer tentative, échec ni succès."""
    retry_at = administrative_retry_at(attempts, now)
    state = dict(cache.refresh_state or {})
    reasons = sorted({str(a.reason) for a in administrative_attempts(attempts)})
    previous = state.get("administrative")
    count = int(previous.get("count", 0)) + 1 if isinstance(previous, dict) else 1
    if isinstance(previous, dict) and isinstance(previous.get("first_at"), str):
        first_at = previous["first_at"]
    else:
        first_at = now.astimezone(UTC).isoformat()
    state["administrative"] = {
        "count": count,
        "first_at": first_at,
        "last_at": now.astimezone(UTC).isoformat(),
        "reasons": reasons,
        "retry_at": retry_at.astimezone(UTC).isoformat(),
    }
    cache.refresh_state = state
    cache.collection_status = ADMINISTRATIVE_STATUS
    return retry_at

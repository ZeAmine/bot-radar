import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.db.models.book_market_cache import BookMarketCache

_reservation_lock: asyncio.Lock | None = None
_reservation_loop: asyncio.AbstractEventLoop | None = None


def reservation_lock() -> asyncio.Lock:
    global _reservation_lock, _reservation_loop
    loop = asyncio.get_running_loop()
    if _reservation_lock is None or _reservation_loop is not loop:
        _reservation_lock, _reservation_loop = asyncio.Lock(), loop
    return _reservation_lock

Component = Literal["current", "history"]
REFRESH_INTERVAL = timedelta(days=7)
RETRY_DELAYS = (timedelta(hours=6), timedelta(days=1), timedelta(days=7))


def aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def next_refresh_slot(moment: datetime) -> datetime:
    slot = moment.replace(minute=15, second=0, microsecond=0)
    while slot < moment:
        slot += timedelta(minutes=30)
    return slot


def component_success_at(
    cache: BookMarketCache | None, component: Component,
) -> datetime | None:
    if cache is None:
        return None
    if component == "current":
        return cache.last_successful_collection_at
    return cache.historical_checked_at


def component_due(cache: BookMarketCache | None, component: Component, now: datetime) -> datetime:
    if cache is None:
        return now
    state = (cache.refresh_state or {}).get(component)
    if isinstance(state, dict) and isinstance(state.get("due_at"), str):
        return datetime.fromisoformat(state["due_at"])
    successful = component_success_at(cache, component)
    due = aware(successful) + REFRESH_INTERVAL if successful else now
    if cache.last_attempt_at and cache.collection_status in {"failed", "partial", "skipped"}:
        due = max(due, aware(cache.last_attempt_at) + RETRY_DELAYS[0])
    return due


def collection_due(cache: BookMarketCache | None, now: datetime) -> bool:
    return any(component_due(cache, component, now) <= now for component in ("current", "history"))


def record_component(cache: BookMarketCache, component: Component, *,
                     succeeded: bool, now: datetime) -> None:
    state = dict(cache.refresh_state or {})
    previous = state.get(component)
    failures = int(previous.get("failures", 0)) if isinstance(previous, dict) else 0
    failures = 0 if succeeded else min(failures + 1, len(RETRY_DELAYS))
    delay = REFRESH_INTERVAL if succeeded else RETRY_DELAYS[failures - 1]
    state[component] = {"due_at": (now + delay).isoformat(), "failures": failures}
    cache.refresh_state = state
    cache.next_refresh_at = None

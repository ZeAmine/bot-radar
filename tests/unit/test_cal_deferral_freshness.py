"""Effet réel d'un report CAL sur la fraîcheur d'un livre.

Ces tests traversent `persist_market_result`, qui appelle la vraie
`_stamp_collection` et la vraie `record_component` : ils disent ce que le
pipeline applicatif fait d'une collecte reportée par le régulateur, et non ce
qu'un montage de test voudrait qu'il fasse.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import CollectionAttempt, MarketOffers
from app.services.book_market_pricing import BookPricingResult, persist_market_result
from app.services.market_refresh_policy import (
    REFRESH_INTERVAL,
    RETRY_DELAYS,
    collection_due,
    component_due,
    record_component,
)


async def _persist(cache: BookMarketCache, attempts: tuple) -> BookMarketCache:
    result = BookPricingResult.from_offers(MarketOffers([], attempts=attempts), None, None)
    repo = MagicMock()
    repo.save_snapshot = AsyncMock()
    repo.refresh_annual_references = AsyncMock()
    repo.get_cache_by_book_id = AsyncMock(return_value=cache)
    repo.update_or_create_cache = AsyncMock(return_value=cache)
    with patch(
        "app.services.book_market_pricing.MarketPriceRepository", return_value=repo,
    ), patch(
        "app.services.book_market_pricing._refresh_series_totals", AsyncMock(),
    ), patch(
        "app.services.book_market_pricing.AsyncSessionLocal", MagicMock(),
    ):
        return await persist_market_result(AsyncMock(), cache.book_id, result)


def _deferred(reason: str = "pass_budget") -> tuple:
    return (CollectionAttempt("chasse_aux_livres", "current", "skipped", reason),)


@pytest.mark.asyncio
async def test_a_never_queried_book_deferred_by_the_governor_is_not_marked_served():
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)

    written = await _persist(cache, _deferred())

    assert written.collection_status == "skipped"
    assert written.last_successful_collection_at is None
    assert written.historical_checked_at is None
    assert written.next_refresh_at is None


@pytest.mark.asyncio
async def test_a_never_queried_book_deferred_by_the_governor_keeps_its_turn():
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)
    written = await _persist(cache, _deferred())

    moment = datetime.now(UTC)
    assert component_due(written, "current", moment) <= moment + RETRY_DELAYS[0]
    assert component_due(written, "current", moment) < moment + REFRESH_INTERVAL


@pytest.mark.asyncio
async def test_a_deferred_collection_records_an_attempt_date():
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)

    written = await _persist(cache, _deferred())

    assert written.last_attempt_at is not None


def test_a_deferral_counts_as_a_failure_and_backs_off():
    cache = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    delays = []
    for _ in range(3):
        record_component(cache, "current", succeeded=False, now=moment)
        due = component_due(cache, "current", moment)
        delays.append(due - moment)
        moment = due

    assert delays[0] == RETRY_DELAYS[0]
    assert delays[1] == RETRY_DELAYS[1]
    assert delays[2] == RETRY_DELAYS[2]


def test_the_backoff_of_a_deferral_is_bounded():
    cache = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    for _ in range(6):
        record_component(cache, "current", succeeded=False, now=moment)
        moment = component_due(cache, "current", moment)

    assert cache.refresh_state["current"]["failures"] == len(RETRY_DELAYS)
    assert moment <= datetime(2026, 12, 31, tzinfo=UTC)


def test_a_book_deferred_once_returns_before_a_book_already_served():
    served = BookMarketCache(book_id=uuid.uuid4())
    deferred = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    record_component(served, "current", succeeded=True, now=moment)
    served.last_successful_collection_at = moment
    record_component(deferred, "current", succeeded=False, now=moment)

    assert component_due(deferred, "current", moment) < component_due(
        served, "current", moment,
    )


def test_a_deferred_book_stays_due_for_the_next_pass():
    cache = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    record_component(cache, "current", succeeded=False, now=moment)
    record_component(cache, "history", succeeded=False, now=moment)

    assert collection_due(cache, moment + RETRY_DELAYS[0]) is True
    assert collection_due(cache, moment + timedelta(minutes=30)) is False


@pytest.mark.asyncio
async def test_a_deferred_provider_does_not_hide_another_provider_answer():
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)
    attempts = (
        CollectionAttempt("chasse_aux_livres", "current", "skipped", "pass_budget"),
        CollectionAttempt("moment", "current", "success"),
    )

    written = await _persist(cache, attempts)

    assert written.collection_status == "empty"
    assert written.last_successful_collection_at is not None


@pytest.mark.asyncio
async def test_eight_books_are_served_in_turn_under_a_two_book_governor_cap():
    books = {index: BookMarketCache(book_id=uuid.uuid4()) for index in range(1, 9)}
    clock = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)
    for cache in books.values():
        cache.next_refresh_at = clock

    served: list[int] = []
    for _ in range(6):
        due = [
            index for index in sorted(books)
            if component_due(books[index], "current", clock) <= clock
        ]
        admitted = due[:2]
        served.extend(admitted)
        for index in due:
            attempt = (
                CollectionAttempt("chasse_aux_livres", "current", "success")
                if index in admitted
                else CollectionAttempt("chasse_aux_livres", "current", "skipped", "pass_budget")
            )
            await _persist(books[index], (attempt,))
            books[index].next_refresh_at = None
        clock += RETRY_DELAYS[0]

    assert served == list(range(1, 9))


@pytest.mark.asyncio
async def test_a_served_book_does_not_come_back_before_a_deferred_one():
    served = BookMarketCache(book_id=uuid.uuid4())
    deferred = BookMarketCache(book_id=uuid.uuid4())
    clock = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)
    served.next_refresh_at = clock
    deferred.next_refresh_at = clock

    await _persist(served, (CollectionAttempt("chasse_aux_livres", "current", "success"),))
    await _persist(deferred, _deferred())

    assert component_due(deferred, "current", clock) < component_due(
        served, "current", clock,
    )

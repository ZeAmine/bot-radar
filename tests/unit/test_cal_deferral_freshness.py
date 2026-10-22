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
    ), patch(
        "app.db.repositories.book_repo.BookRepository.plan_price_refreshes", AsyncMock(),
    ), patch(
        "app.services.book_market_pricing._reserve_next_refresh", AsyncMock(),
    ):
        return await persist_market_result(AsyncMock(), cache.book_id, result)


def _administrative_deferral(reason: str = "pass_budget") -> tuple:
    return (CollectionAttempt("chasse_aux_livres", "current", "skipped", reason),)


@pytest.mark.asyncio
async def test_a_never_queried_book_deferred_by_the_governor_is_not_marked_served():
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)

    written = await _persist(cache, _administrative_deferral())

    assert written.collection_status == "deferred"
    assert written.last_successful_collection_at is None
    assert written.historical_checked_at is None
    assert written.next_refresh_at is not None
    assert written.next_refresh_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_a_never_queried_book_deferred_by_the_governor_keeps_its_turn():
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)
    written = await _persist(cache, _administrative_deferral())

    moment = datetime.now(UTC)
    assert component_due(written, "current", moment) <= moment + RETRY_DELAYS[0]
    assert component_due(written, "current", moment) < moment + REFRESH_INTERVAL


@pytest.mark.asyncio
async def test_a_deferred_collection_invents_no_network_attempt():
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)

    written = await _persist(cache, _administrative_deferral())

    assert written.last_attempt_at is None
    assert (written.refresh_state or {}).get("current") is None
    assert (written.refresh_state or {})["administrative"]["count"] == 1


def test_a_component_failure_backs_off_six_hours_then_one_day_then_seven():
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


def test_the_component_backoff_is_bounded_to_three_steps():
    cache = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    for _ in range(6):
        record_component(cache, "current", succeeded=False, now=moment)
        moment = component_due(cache, "current", moment)

    assert cache.refresh_state["current"]["failures"] == len(RETRY_DELAYS)
    assert moment <= datetime(2026, 12, 31, tzinfo=UTC)


def test_a_failed_component_returns_before_a_served_one():
    served = BookMarketCache(book_id=uuid.uuid4())
    failed = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    record_component(served, "current", succeeded=True, now=moment)
    served.last_successful_collection_at = moment
    record_component(failed, "current", succeeded=False, now=moment)

    assert component_due(failed, "current", moment) < component_due(
        served, "current", moment,
    )


def test_a_failed_component_stays_due_for_the_next_pass():
    cache = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    record_component(cache, "current", succeeded=False, now=moment)
    record_component(cache, "history", succeeded=False, now=moment)

    assert collection_due(cache, moment + RETRY_DELAYS[0]) is True
    assert collection_due(cache, moment + timedelta(minutes=30)) is False


@pytest.mark.asyncio
async def test_an_administrative_deferral_does_not_hide_another_provider_answer():
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
async def test_a_served_book_does_not_come_back_before_a_deferred_one():
    served = BookMarketCache(book_id=uuid.uuid4())
    deferred = BookMarketCache(book_id=uuid.uuid4())
    clock = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)
    served.next_refresh_at = clock
    deferred.next_refresh_at = clock

    await _persist(served, (CollectionAttempt("chasse_aux_livres", "current", "success"),))
    await _persist(deferred, _administrative_deferral())

    assert component_due(deferred, "current", clock) < component_due(
        served, "current", clock,
    )


def test_deferred_status_is_outside_the_retry_backoff_of_the_shared_policy():
    """Comportement réel de `component_due`, sans modification de la politique.

    La politique applique son recul de six heures aux statuts `failed`,
    `partial` et `skipped` — pas à `deferred`, qui n'y figure pas. Un livre
    reporté avant toute requête ne subit donc pas le recul d'un échec.
    """
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    deferred = BookMarketCache(book_id=uuid.uuid4())
    deferred.last_attempt_at = moment
    deferred.collection_status = "deferred"
    skipped = BookMarketCache(book_id=uuid.uuid4())
    skipped.last_attempt_at = moment
    skipped.collection_status = "skipped"

    assert component_due(deferred, "current", moment) == moment
    assert component_due(skipped, "current", moment) == moment + RETRY_DELAYS[0]

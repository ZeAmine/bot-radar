from datetime import UTC, datetime, timedelta
from itertools import product

import pytest

from app.core.constants import ComparisonStatus
from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
from app.services.book_market_pricing import BookPricingResult, _stamp_collection
from app.services.market_refresh_policy import (
    REFRESH_INTERVAL,
    RETRY_DELAYS,
    collection_due,
    component_due,
    record_component,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=8)
CURRENT_STATUSES = ("success", "partial", "failed", "skipped")


def _result(
    *,
    attempts: tuple[CollectionAttempt, ...] = (),
    failed_providers: tuple[str, ...] = (),
    price: int | None = None,
    price_confirmed: bool = False,
    historical_checked: bool = False,
    collected_at: datetime = NOW,
) -> BookPricingResult:
    return BookPricingResult(
        offers=[],
        lowest_delivered_price_cents=price,
        best_provider_name="source" if price is not None else None,
        best_source_url=None,
        confidence=None,
        comparison_status=(
            ComparisonStatus.AVAILABLE if price is not None else ComparisonStatus.UNAVAILABLE
        ),
        cache_duration_hours=168 if price is not None else 6,
        historical_low_1y_cents=None,
        failed_providers=failed_providers,
        attempts=attempts,
        historical_checked=historical_checked,
        price_confirmed=price_confirmed,
        collected_at=collected_at,
    )


def _expected_complete(
    attempts: tuple[CollectionAttempt, ...],
    failed_providers: tuple[str, ...],
    price: int | None,
    price_confirmed: bool,
) -> bool:
    current = [a for a in attempts if a.operation == "current" and a.status != "skipped"]
    if attempts:
        return bool(current) and all(a.status == "success" for a in current)
    return not failed_providers and (price is None or price_confirmed)


def _expected_usable(
    attempts: tuple[CollectionAttempt, ...],
    failed_providers: tuple[str, ...],
    price: int | None,
    price_confirmed: bool,
) -> bool:
    if not attempts:
        return not failed_providers and (price is None or price_confirmed)
    current = [a for a in attempts if a.operation == "current" and a.status != "skipped"]
    if not current or any(a.status == "failed" for a in current):
        return False
    if price is not None:
        return price_confirmed or all(a.status == "success" for a in current)
    return all(a.status == "success" for a in current)


def _current_status_sets():
    for count in range(4):
        yield from product(CURRENT_STATUSES, repeat=count)


def _attempts(
    statuses: tuple[str, ...], history_status: str | None = None,
) -> tuple[CollectionAttempt, ...]:
    current = tuple(CollectionAttempt(f"source-{index}", "current", status)
                    for index, status in enumerate(statuses))
    history = (
        (CollectionAttempt("history-source", "history", history_status),)
        if history_status else ()
    )
    return current + history


def _offer(provider: str, cents: int) -> MarketOffer:
    return MarketOffer(
        provider_name=provider,
        item_price_cents=cents,
        shipping_price_cents=None,
        delivered_price_cents=cents,
        source_url=f"https://{provider}.example/{cents}",
        item_condition="Très bon état",
        price_includes_shipping=True,
        raw_payload={"best_merchant": provider},
    )


def test_truth_table_matches_declared_contract() -> None:
    for statuses in _current_status_sets():
        for history_status, price, price_confirmed, failed_providers, historical_checked in product(
            (None, "success", "partial", "failed", "skipped"),
            (None, 1000),
            (False, True),
            ((), ("failed-source",)),
            (False, True),
        ):
            attempts = _attempts(statuses, history_status)
            result = _result(
                attempts=attempts,
                failed_providers=failed_providers,
                price=price,
                price_confirmed=price_confirmed,
                historical_checked=historical_checked,
            )
            complete = _expected_complete(
                attempts, failed_providers, price, price_confirmed,
            )
            usable = _expected_usable(
                attempts, failed_providers, price, price_confirmed,
            )

            assert result.current_collection_complete is complete, (statuses, history_status)
            assert result.current_collection_succeeded is complete, (statuses, history_status)
            assert result.current_collection_usable is usable, (
                statuses, history_status, price, failed_providers,
            )
            if complete:
                assert usable, (statuses, history_status, price, failed_providers)
            if usable and not complete:
                assert price is not None, (statuses, history_status, failed_providers)
                assert price_confirmed, (statuses, history_status, failed_providers)


def test_usable_requires_no_failed_current_attempt() -> None:
    for statuses in _current_status_sets():
        attempts = _attempts(statuses, "success")
        current_failed = any(
            attempt.operation == "current" and attempt.status == "failed"
            for attempt in attempts
        )
        result = _result(attempts=attempts, price=1000)
        if current_failed:
            assert result.current_collection_usable is False, statuses


def test_usable_requires_a_confirmed_price_or_all_current_success() -> None:
    cal = _offer("chasse_aux_livres", 1500)
    healthy = _offer("momox", 1000)
    offers = MarketOffers(
        [cal, healthy],
        failed_providers=("chasse_aux_livres",),
        attempts=(
            CollectionAttempt("chasse_aux_livres", "current", "partial", "incomplete_offers"),
            CollectionAttempt("momox", "current"),
        ),
    )

    result = BookPricingResult.from_offers(offers, cal, None)

    assert result.current_collection_complete is False
    assert result.current_collection_usable is True
    assert result.lowest_delivered_price_cents == 1000
    assert result.best_provider_name == "momox"


def test_complete_is_strictly_narrower_than_usable() -> None:
    cal = _offer("chasse_aux_livres", 1500)
    healthy = _offer("momox", 1000)
    partial_with_price = BookPricingResult.from_offers(
        MarketOffers(
            [cal, healthy],
            failed_providers=("chasse_aux_livres",),
            attempts=(
                CollectionAttempt("chasse_aux_livres", "current", "partial"),
                CollectionAttempt("momox", "current"),
            ),
        ),
        cal,
        None,
    )
    success_without_price = BookPricingResult.from_offers(
        MarketOffers([], attempts=(CollectionAttempt("momox", "current"),)),
        None,
        None,
    )
    partial_alone = BookPricingResult.from_offers(
        MarketOffers(
            [cal],
            failed_providers=("chasse_aux_livres",),
            attempts=(CollectionAttempt("chasse_aux_livres", "current", "partial"),),
        ),
        cal,
        None,
    )

    assert partial_with_price.current_collection_usable is True
    assert partial_with_price.current_collection_complete is False
    assert success_without_price.current_collection_usable is True
    assert success_without_price.current_collection_complete is True
    assert partial_alone.current_collection_usable is False
    assert partial_alone.current_collection_complete is False


@pytest.mark.parametrize(("statuses", "price", "price_confirmed", "failed_providers", "expected"), [
    ((), None, False, (), True),
    ((), None, False, ("failed-source",), False),
    (("success",), None, False, (), True),
    (("success",), 1000, False, (), True),
    (("success", "partial"), 1000, False, ("failed-source",), False),
    (("success", "partial"), 1000, True, ("failed-source",), True),
    (("success", "partial"), None, False, (), False),
    (("partial",), 1000, False, ("failed-source",), False),
    (("partial",), 1000, True, ("failed-source",), True),
    (("partial",), None, False, ("failed-source",), False),
    (("success", "failed"), 1000, False, (), False),
    (("skipped",), 1000, False, (), False),
])
def test_stamp_advances_only_for_usable_results(
    statuses, price, price_confirmed, failed_providers, expected,
) -> None:
    attempts = tuple(
        CollectionAttempt(f"source-{index}", "current", status)
        for index, status in enumerate(statuses)
    )
    result = _result(
        attempts=attempts,
        failed_providers=failed_providers,
        price=price,
        price_confirmed=price_confirmed,
        collected_at=NOW,
    )
    cache = BookMarketCache(last_successful_collection_at=OLD)

    _stamp_collection(cache, result)

    assert cache.last_successful_collection_at == (NOW if expected else OLD)


def test_plain_offer_list_without_attempts_cannot_confirm_a_price() -> None:
    cal = _offer("chasse_aux_livres", 1500)

    result = BookPricingResult.from_offers([cal], cal, None)

    assert result.lowest_delivered_price_cents is None
    assert result.price_confirmed is False
    assert result.current_collection_usable is True


@pytest.mark.parametrize(
    ("statuses", "price", "price_confirmed", "failed_providers", "expected"),
    [
        ((), None, False, (), "empty_complete"),
        ((), None, False, ("failed-source",), "failed"),
        ((), 1000, False, (), "unproven_price"),
        ((), 1000, True, (), "confirmed_price"),
        (("success",), None, False, (), "empty_complete"),
        (("success",), 1000, False, (), "all_sources_succeeded"),
        (("partial",), 1000, False, ("failed-source",), "unproven_price"),
        (("partial",), 1000, True, ("failed-source",), "confirmed_price"),
        (("partial",), None, False, ("failed-source",), "partial_without_price"),
        (("success", "failed"), 1000, False, (), "failed"),
        (("skipped",), 1000, False, (), "no_current_attempt"),
    ],
)
def test_reason_names_the_blocking_or_accepting_condition(
    statuses, price, price_confirmed, failed_providers, expected,
) -> None:
    result = _result(
        attempts=_attempts(statuses),
        failed_providers=failed_providers,
        price=price,
        price_confirmed=price_confirmed,
    )

    assert result.current_collection_reason == expected


def test_skipped_current_attempt_does_not_confirm_a_price() -> None:
    offer = _offer("chasse_aux_livres", 1500)
    offers = MarketOffers(
        [offer], attempts=(CollectionAttempt("chasse_aux_livres", "current", "skipped"),),
    )

    result = BookPricingResult.from_offers(offers, offer, None)

    assert result.lowest_delivered_price_cents is None
    assert result.price_confirmed is False


def test_direct_partial_price_without_confirmation_is_not_usable() -> None:
    result = _result(
        attempts=(CollectionAttempt("chasse_aux_livres", "current", "partial"),),
        price=1500,
    )

    assert result.price_confirmed is False
    assert result.current_collection_usable is False
    assert result.current_collection_complete is False


def test_retry_schedule_is_six_hours_then_one_day_then_seven_days() -> None:
    cache = BookMarketCache()

    for index, expected in enumerate(RETRY_DELAYS):
        record_component(cache, "current", succeeded=False, now=NOW + timedelta(days=index))
        assert component_due(cache, "current", NOW + timedelta(days=index)) == (
            NOW + timedelta(days=index) + expected
        )
        assert cache.refresh_state["current"]["failures"] == index + 1

    record_component(cache, "current", succeeded=False, now=NOW + timedelta(days=4))
    assert cache.refresh_state["current"]["failures"] == len(RETRY_DELAYS)
    assert component_due(cache, "current", NOW + timedelta(days=4)) == (
        NOW + timedelta(days=4) + RETRY_DELAYS[-1]
    )

    record_component(cache, "current", succeeded=True, now=NOW + timedelta(days=5))
    assert cache.refresh_state["current"]["failures"] == 0
    assert component_due(cache, "current", NOW + timedelta(days=5)) == (
        NOW + timedelta(days=5) + REFRESH_INTERVAL
    )


def test_due_boundaries_cover_six_hours_and_seven_days() -> None:
    partial = _result(
        attempts=(CollectionAttempt("source", "current", "partial"),),
        failed_providers=("source",),
        collected_at=NOW,
    )
    retry_cache = BookMarketCache()
    _stamp_collection(retry_cache, partial)

    assert not collection_due(retry_cache, NOW + timedelta(hours=5, minutes=59, seconds=59))
    assert collection_due(retry_cache, NOW + timedelta(hours=6))
    assert collection_due(retry_cache, NOW + timedelta(hours=6, minutes=59, seconds=59))

    success = _result(
        attempts=(
            CollectionAttempt("source", "current"),
            CollectionAttempt("history-source", "history"),
        ),
        historical_checked=True,
        collected_at=NOW,
    )
    success_cache = BookMarketCache()
    _stamp_collection(success_cache, success)

    assert not collection_due(success_cache, NOW + timedelta(days=6, hours=23))
    assert not collection_due(success_cache, NOW + timedelta(days=6, hours=23, minutes=59,
                                                            seconds=59))
    assert collection_due(success_cache, NOW + timedelta(days=7))


def test_naive_cached_dates_are_normalized() -> None:
    naive = datetime(2026, 10, 1, 12, 0)
    cache = BookMarketCache(
        last_successful_collection_at=naive,
        last_attempt_at=naive,
        collection_status="partial",
    )

    assert component_due(cache, "current", NOW) == naive.replace(tzinfo=UTC) + REFRESH_INTERVAL
    assert collection_due(cache, NOW) is True

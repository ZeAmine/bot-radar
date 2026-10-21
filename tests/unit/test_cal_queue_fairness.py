"""Équité de la file de rafraîchissement face au plafond CAL.

Les files réelles sont celles du dépôt : `component_due` et `collection_due`
décident de l'échéance, et la passe ne traite que les livres dus, dans l'ordre
de leur créneau. Un livre écarté par le régulateur doit être reporté sans être
avantage ni pénalisé, et sans monopoliser les créneaux du lot.
"""

from datetime import UTC, datetime

from app.services.market_refresh_policy import (
    REFRESH_INTERVAL,
    RETRY_DELAYS,
    collection_due,
    component_due,
    record_component,
)

BATCH = 8
CAL_CAP = 2


class Cache:
    def __init__(self) -> None:
        self.last_attempt_at = None
        self.collection_status = "complete"
        self.last_successful_collection_at = None
        self.historical_checked_at = None
        self.refresh_state: dict = {}


def _pass(
    books: dict[int, Cache], now: datetime, cap: int = CAL_CAP,
) -> tuple[list[int], list[int]]:
    due = sorted(index for index, cache in books.items() if collection_due(cache, now))
    batch = due[:BATCH]
    admitted, deferred = batch[:cap], batch[cap:]
    for index in admitted:
        cache = books[index]
        record_component(cache, "current", succeeded=True, now=now)
        record_component(cache, "history", succeeded=True, now=now)
        cache.last_successful_collection_at = now
        cache.historical_checked_at = now
        cache.collection_status = "complete"
    for index in deferred:
        cache = books[index]
        cache.last_attempt_at = now
        cache.collection_status = "skipped"
        record_component(cache, "current", succeeded=False, now=now)
    return admitted, deferred


def test_eight_due_books_are_served_in_turn_across_consecutive_passes():
    books = {index: Cache() for index in range(1, BATCH + 1)}
    clock = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    served: list[int] = []
    for _ in range(6):
        admitted, _ = _pass(books, clock)
        served.extend(admitted)
        clock += RETRY_DELAYS[0]

    assert served == list(range(1, BATCH + 1))
    assert set(served) == set(books)


def test_a_deferred_book_is_not_pushed_back_by_a_served_one():
    books = {index: Cache() for index in range(1, BATCH + 1)}
    clock = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)
    _pass(books, clock)

    served_due = component_due(books[1], "current", clock)
    deferred_due = component_due(books[8], "current", clock)

    assert deferred_due < served_due
    assert served_due == clock + REFRESH_INTERVAL


def test_a_deferred_book_stays_at_the_head_of_the_queue_without_blocking_others():
    books = {index: Cache() for index in range(1, BATCH + 1)}
    clock = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)
    _pass(books, clock)

    next_pass = clock + RETRY_DELAYS[0]
    admitted, deferred = _pass(books, next_pass)

    assert admitted == [3, 4]
    assert deferred == [5, 6, 7, 8]
    assert 1 not in admitted
    assert 2 not in admitted


def test_a_deferred_book_does_not_invent_a_success_date():
    books = {1: Cache(), 2: Cache()}
    clock = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)
    _pass(books, clock)

    for index in (1, 2):
        cache = books[index]
        assert cache.last_successful_collection_at == clock
        assert cache.historical_checked_at == clock

    deferred = Cache()
    deferred.last_attempt_at = clock
    deferred.collection_status = "skipped"
    record_component(deferred, "current", succeeded=False, now=clock)
    assert deferred.last_successful_collection_at is None
    assert deferred.historical_checked_at is None


def test_a_deferred_book_records_an_attempt_without_claiming_a_collection():
    cache = Cache()
    clock = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)
    cache.last_attempt_at = clock
    cache.collection_status = "skipped"
    record_component(cache, "current", succeeded=False, now=clock)

    assert cache.last_attempt_at == clock
    assert cache.collection_status == "skipped"
    assert cache.refresh_state["current"]["failures"] == 1
    assert collection_due(cache, clock) is False
    assert collection_due(cache, clock + RETRY_DELAYS[0]) is True


def test_the_queue_drains_completely_before_any_book_returns():
    books = {index: Cache() for index in range(1, BATCH + 1)}
    clock = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    first_round: list[int] = []
    for _ in range(6):
        admitted, _ = _pass(books, clock)
        first_round.extend(admitted)
        clock += RETRY_DELAYS[0]

    assert first_round == list(range(1, BATCH + 1))


def test_a_deferred_cal_collection_is_not_a_failed_provider_for_other_sources():
    from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
    from app.services.book_market_pricing import BookPricingResult

    offer = MarketOffer(
        "momox", 1200, 300, 1500, "https://example.test/1",
        item_condition="Bon", price_includes_shipping=True,
    )
    offers = MarketOffers(
        [offer], historical_checked=True, attempts=(
            CollectionAttempt("chasse_aux_livres", "current", "skipped", "rate_limited"),
            CollectionAttempt("chasse_aux_livres", "history", "skipped", "rate_limited"),
            CollectionAttempt("momox", "current", "success"),
            CollectionAttempt("momox", "history", "success"),
        ),
    )

    result = BookPricingResult.from_offers(offers, offer, None)

    assert result.failed_providers == ()
    assert result.lowest_delivered_price_cents == 1500
    assert result.current_collection_usable is True
    assert result.historical_checked is True


def test_a_book_whose_only_source_is_deferred_is_not_claimed_as_collected():
    from app.providers.base import CollectionAttempt, MarketOffers
    from app.services.book_market_pricing import BookPricingResult

    offers = MarketOffers([], attempts=(
        CollectionAttempt("chasse_aux_livres", "current", "skipped", "pass_budget"),
    ))

    result = BookPricingResult.from_offers(offers, None, None)

    assert result.failed_providers == ()
    assert result.current_collection_usable is False
    assert result.historical_checked is False

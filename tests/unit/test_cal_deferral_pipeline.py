"""Reprise d'un livre reporté, à travers la vraie sélection et la vraie planification.

Ces tests n'appellent pas la sélection d'un lot par un découpage local : ils
passent par `BookRepository.get_books_needing_price_refresh` et
`plan_price_refreshes`, et par le régulateur réel contre Redis. Seuls la
requête SQL de chargement et la session sont substituées.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models.book_market_cache import BookMarketCache
from app.db.repositories.book_repo import BookRepository
from app.providers.base import CollectionAttempt, MarketOffers
from app.services.book_market_pricing import BookPricingResult, persist_market_result
from app.services.market_refresh_policy import component_due

pytest_plugins = ["tests.unit.cal_redis_fixture"]

CAPACITY = 8
BOOKS = 8
PASS_START = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)


def _book(cache: BookMarketCache) -> SimpleNamespace:
    return SimpleNamespace(id=cache.book_id, market_cache=cache)


def _repository(books: list[SimpleNamespace], _now: datetime | None = None) -> BookRepository:
    session = MagicMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    repository = BookRepository(session)
    repository.get_books_for_active_bd_alerts = AsyncMock(return_value=books)
    repository._refresh_annual = AsyncMock()
    return repository


async def _select(repository: BookRepository, now: datetime):
    return await repository.get_books_needing_price_refresh(limit=CAPACITY, now=now)


async def _plan(repository: BookRepository, now: datetime) -> None:
    await repository.plan_price_refreshes(now=now)


async def _persist(
    cache: BookMarketCache, attempts: tuple, now: datetime | None = None,
) -> BookMarketCache:
    result = replace(
        BookPricingResult.from_offers(MarketOffers([], attempts=attempts), None, None),
        collected_at=now or datetime.now(UTC),
    )
    repo = MagicMock()
    repo.save_snapshot = AsyncMock()
    repo.refresh_annual_references = AsyncMock()
    repo.alert_price_ceiling_cents = AsyncMock(return_value=None)
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


@pytest.mark.asyncio
async def test_a_deferred_book_keeps_a_slot_and_no_invented_failure():
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = PASS_START
    books = [_book(cache)]
    repository = _repository(books, PASS_START)

    assert await _select(repository, PASS_START) == books

    await _persist(cache, (
        CollectionAttempt("chasse_aux_livres", "current", "skipped", "pass_budget",
                          retry_after_seconds=1800.0),
    ))

    assert cache.last_attempt_at is None
    assert cache.last_successful_collection_at is None
    assert (cache.refresh_state or {}).get("current") is None
    assert cache.collection_status == "deferred"

    retry_at = cache.next_refresh_at
    assert retry_at is not None
    assert retry_at >= PASS_START + timedelta(minutes=30)

    await _plan(repository, PASS_START)
    assert cache.next_refresh_at == retry_at


@pytest.mark.asyncio
async def test_a_deferred_book_returns_at_its_retry_slot_without_looping():
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = PASS_START
    books = [_book(cache)]
    repository = _repository(books, PASS_START)

    await _persist(cache, (
        CollectionAttempt("chasse_aux_livres", "current", "skipped", "pass_budget",
                          retry_after_seconds=1800.0),
    ))
    cache.next_refresh_at = PASS_START + timedelta(minutes=30)
    await _plan(repository, PASS_START)

    assert await _select(repository, PASS_START + timedelta(minutes=5)) == []
    retry_at = cache.next_refresh_at
    assert retry_at is not None
    assert await _select(repository, retry_at) == books


@pytest.mark.asyncio
async def test_eight_due_books_are_admitted_in_four_passes(clean_cal_keys, cal_namespace):
    from app.providers.base import deferred_offers
    from app.providers.cal_governor import CalGovernor, CalSuspendedError

    class Clock:
        def __init__(self) -> None:
            self.now = PASS_START.timestamp()

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace, clock=clock)

    books = []
    for index in range(1, BOOKS + 1):
        cache = BookMarketCache(book_id=uuid.UUID(int=index))
        cache.next_refresh_at = PASS_START
        books.append(_book(cache))

    passes: list[tuple[datetime, list[int], list[str]]] = []
    moment = PASS_START
    for _ in range(4):
        clock.now = moment.timestamp()
        repository = _repository(books)
        await _plan(repository, moment)
        selected = await _select(repository, moment)

        admitted: list[int] = []
        refused: list[str] = []
        for book in selected:
            try:
                await governor.admit_book(None)
            except CalSuspendedError as exc:
                refused.append(exc.reason)
                await _persist(
                    book.market_cache,
                    deferred_offers(
                        "chasse_aux_livres", reason=exc.reason, retry_at=exc.retry_at,
                    ).attempts,
                    now=moment,
                )
            else:
                admitted.append(books.index(book) + 1)
                await _persist(book.market_cache, (
                    CollectionAttempt("chasse_aux_livres", "current", "success"),
                ), now=moment)
        passes.append((moment, admitted, refused))
        moment += timedelta(minutes=30)

    assert [admitted for _, admitted, _ in passes] == [[1, 2], [3, 4], [5, 6], [7, 8]]
    assert [slot.strftime("%H:%M") for slot, _, _ in passes] == [
        "12:15", "12:45", "13:15", "13:45",
    ]
    assert {refusal for _, _, refused in passes for refusal in refused} == {"pass_budget"}


@pytest.mark.asyncio
async def test_a_storage_deferral_schedules_a_bounded_retry():
    from app.providers.cal_governor import CAL_STORAGE_RETRY_SECONDS

    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = PASS_START
    moment = PASS_START

    await _persist(cache, (
        CollectionAttempt("chasse_aux_livres", "current", "skipped", "storage_unavailable"),
    ), now=moment)

    assert cache.next_refresh_at == moment + timedelta(seconds=CAL_STORAGE_RETRY_SECONDS)
    assert component_due(cache, "current", moment) <= moment + timedelta(minutes=10)
    assert component_due(cache, "current", moment) < moment + timedelta(hours=6)

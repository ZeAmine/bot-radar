"""Effet réel d'un report CAL et d'un échec sur la fraîcheur d'un livre.

Ces tests traversent `persist_market_result`, qui appelle la vraie
`_stamp_collection` et la vraie `record_component` : ils disent ce que le
pipeline applicatif fait d'une collecte reportée par le régulateur, et non ce
qu'un montage de test voudrait qu'il fasse.

La règle qui a remplacé l'échelle de relance à six heures, un jour, sept jours :
**un échec n'avance jamais une échéance**. Le livre reprend sa place dans la
file ordinaire au rythme de sa zone ; le compteur d'échecs sert à mesurer une
panne, jamais à calculer une date.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import CollectionAttempt, MarketOffers
from app.services.book_market_pricing import BookPricingResult, persist_market_result
from app.services.market_refresh_policy import (
    COLD_CADENCE,
    HISTORY_CADENCE,
    HOT_CADENCE,
    collection_due,
    component_due,
    record_component,
)


async def _persist(cache: BookMarketCache, attempts: tuple) -> BookMarketCache:
    result = BookPricingResult.from_offers(MarketOffers([], attempts=attempts), None, None)
    repo = MagicMock()
    repo.save_snapshot = AsyncMock()
    repo.refresh_annual_references = AsyncMock()
    repo.alert_price_ceiling_cents = AsyncMock(return_value=None)
    repo.get_cache_by_book_id = AsyncMock(return_value=cache)
    repo.update_or_create_cache = AsyncMock(return_value=cache)
    repo.alert_price_ceiling_cents = AsyncMock(return_value=None)
    with patch(
        "app.services.book_market_pricing.MarketPriceRepository", return_value=repo,
    ), patch(
        "app.services.book_market_pricing._refresh_series_totals", AsyncMock(),
    ), patch(
        "app.services.book_market_pricing.AsyncSessionLocal", MagicMock(),
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
    """Un report n'invente ni relevé ni échéance : le livre reste dû."""
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)
    written = await _persist(cache, _administrative_deferral())

    moment = datetime.now(UTC)
    assert component_due(written, "current", moment) <= moment


@pytest.mark.asyncio
async def test_a_deferred_collection_invents_no_network_attempt():
    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)

    written = await _persist(cache, _administrative_deferral())

    assert written.last_attempt_at is None
    assert (written.refresh_state or {}).get("current") is None
    assert (written.refresh_state or {})["administrative"]["count"] == 1


def test_a_failure_waits_for_the_next_rhythm_instead_of_backing_off():
    """Un échec replace le livre au rythme suivant, sans barreau intermédiaire."""
    cache = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    record_component(cache, "current", succeeded=False, now=moment)

    assert component_due(cache, "current", moment) == moment + COLD_CADENCE
    assert cache.refresh_state["current"]["failures"] == 1


def test_repeated_failures_do_not_shorten_the_next_attempt():
    """Le délai d'un échec est le même au premier et au cinquième : plus d'escalade."""
    cache = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    delays = []
    for _ in range(5):
        record_component(cache, "current", succeeded=False, now=moment)
        due = component_due(cache, "current", moment)
        delays.append(due - moment)
        moment = due

    assert set(delays) == {COLD_CADENCE}
    assert cache.refresh_state["current"]["failures"] == 5


def test_a_failed_hot_book_comes_back_at_the_hot_rhythm():
    """Le rythme d'une relance est celui de la zone, pas celui de la panne."""
    cache = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    record_component(cache, "current", succeeded=True, now=moment,
                     price_cents=1000, ceiling_cents=1000)
    record_component(cache, "current", succeeded=False, now=moment)

    assert cache.refresh_state["current"]["zone"] == "hot"
    assert component_due(cache, "current", moment) == moment + HOT_CADENCE


def test_a_failed_component_leaves_the_pass_and_returns_at_its_rhythm():
    cache = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    record_component(cache, "current", succeeded=False, now=moment)
    record_component(cache, "history", succeeded=False, now=moment)

    assert collection_due(cache, moment + timedelta(minutes=30)) is False
    assert collection_due(cache, moment + COLD_CADENCE) is True


def test_history_failures_wait_a_month():
    """L'historique ne repart pas à chaque passe : son rythme est mensuel."""
    cache = BookMarketCache(book_id=uuid.uuid4())
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)

    record_component(cache, "history", succeeded=False, now=moment)

    assert component_due(cache, "history", moment) == moment + HISTORY_CADENCE


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


def test_a_deferred_book_keeps_its_administrative_retry_date():
    """Le report administratif reste daté par le régulateur, hors du modèle de rythme."""
    moment = datetime(2026, 10, 21, 12, 15, tzinfo=UTC)
    deferred = BookMarketCache(book_id=uuid.uuid4())
    deferred.collection_status = "deferred"

    # Le report n'écrit aucune composante : le livre reste dû, sans fausse fraîcheur.
    assert (deferred.refresh_state or {}).get("current") is None
    assert component_due(deferred, "current", moment) == moment

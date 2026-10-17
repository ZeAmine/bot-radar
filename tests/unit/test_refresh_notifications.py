from unittest.mock import AsyncMock, patch

import pytest

from app.providers.base import CollectionAttempt
from app.services import market_health


@pytest.fixture
def notifier(monkeypatch):
    monkeypatch.setattr(market_health, "_incident_states", {})
    monkeypatch.setattr(market_health, "_incident_loaded", False)
    monkeypatch.setattr(market_health, "_notification_deadlines", {})
    client = AsyncMock()
    client.get.return_value = None
    client.set.return_value = True
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    return AsyncMock(return_value=True)


def result(*, incremental=False, occurrence="pass-1", attempts=()):
    return market_health.RefreshPassResult(
        total=3,
        stats={
            "processed": 3,
            "refreshed": 0,
            "available": 0,
            "unavailable": 0,
            "degraded": 3,
            "errors": 0,
        },
        incremental=incremental,
        occurrence=occurrence,
        attempts=list(attempts),
    )


@pytest.mark.asyncio
async def test_complete_pass_consolidates_failures_and_freshness_once(notifier):
    report = result(attempts=[CollectionAttempt("momox", status="failed", reason="timeout"),
                              CollectionAttempt("gibert", status="skipped")])
    report.never_collected = 2
    report.stale_current = 1
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(report)
        await market_health.notify_refresh_result(report)
    assert notifier.await_count == 1
    text = notifier.call_args.args[0]
    assert "Actualisées : 0" in text
    assert "momox" in text
    assert "Jamais collectés : 2" in text
    assert "Tentatives fournisseur non effectuées : 1" in text
    assert "Offres non actualisées" not in text


@pytest.mark.asyncio
async def test_incremental_incident_changes_and_recovery(notifier):
    failed = CollectionAttempt("momox", status="failed", reason="timeout")
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(result(incremental=True, attempts=[failed]))
        await market_health.notify_refresh_result(result(incremental=True, attempts=[failed]))
        await market_health.notify_refresh_result(result(incremental=True, attempts=[]))
        await market_health.notify_refresh_result(
            result(incremental=True, attempts=[CollectionAttempt("momox", status="skipped")])
        )
        assert notifier.await_count == 1
        await market_health.notify_refresh_result(
            result(
                incremental=True,
                attempts=[CollectionAttempt("momox")],
            )
        )
    assert notifier.await_count == 2
    assert "Rétablissement" in notifier.call_args.args[0]


@pytest.mark.asyncio
async def test_failed_notification_can_be_retried(notifier):
    notifier.side_effect = [False, True]
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(result())
        await market_health.notify_refresh_result(result())
    assert notifier.await_count == 2


@pytest.mark.asyncio
async def test_new_incident_aggravation_and_recovery_with_redis_down(notifier, monkeypatch):
    client = AsyncMock()
    client.get.side_effect = ConnectionError
    client.set.side_effect = ConnectionError
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    partial = [
        CollectionAttempt("momox", status="failed", reason="timeout"),
        CollectionAttempt("momox"),
        CollectionAttempt("momox"),
    ]
    worse = [CollectionAttempt("momox", status="failed", reason="timeout")] * 3
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(result(incremental=True, attempts=partial))
        await market_health.notify_refresh_result(result(incremental=True, attempts=worse))
        assert "Aggravation" in notifier.call_args.args[0]
        await market_health.notify_refresh_result(result(incremental=True, attempts=worse))
        assert notifier.await_count == 2
        await market_health.notify_refresh_result(
            result(
                incremental=True,
                attempts=[CollectionAttempt("momox")],
            )
        )
    assert notifier.await_count == 3


@pytest.mark.asyncio
async def test_empty_complete_pass_still_reports(notifier):
    report = result()
    report.total = 0
    report.stats = dict.fromkeys(report.stats, 0)
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(report)
    assert notifier.await_count == 1
    assert "Traités : 0/0" in notifier.call_args.args[0]


@pytest.mark.asyncio
async def test_selection_exception_is_reported_and_still_raised(notifier):
    from app.tasks import refresh_book_prices

    with (
        patch.object(market_health, "notify_admins", notifier),
        patch.object(
            refresh_book_prices,
            "_select_books",
            AsyncMock(side_effect=RuntimeError("synthetic")),
        ),
        patch.object(refresh_book_prices, "read_refresh_health", AsyncMock()),
        pytest.raises(
            RuntimeError,
            match="synthetic",
        ),
    ):
        await refresh_book_prices.refresh_book_prices_task(
            registry=AsyncMock(), session=AsyncMock()
        )
    assert notifier.await_count == 1
    assert "Interruption technique" in notifier.call_args.args[0]


@pytest.mark.asyncio
async def test_normal_deadline_does_not_create_an_incident(notifier):
    report = result()
    report.interrupted = True
    report.stats["degraded"] = 0
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(report)
    text = notifier.call_args.args[0]
    assert "Plafond de durée atteint" in text
    assert "incident" not in text
    assert "⚠️" not in text


def test_current_offer_counts_as_refreshed_when_history_fails():
    from app.db.models.book_market_cache import BookMarketCache
    from app.providers.base import MarketOffer, MarketOffers
    from app.services.book_market_pricing import BookPricingResult
    from app.tasks.refresh_book_prices import _book_result

    offer = MarketOffer(
        provider_name="source",
        item_price_cents=2059,
        shipping_price_cents=0,
        delivered_price_cents=2059,
        source_url="https://example.invalid",
    )
    offers = MarketOffers(
        [offer],
        failed_providers=("source",),
        attempts=(
            CollectionAttempt("source"),
            CollectionAttempt("source", "history", "failed", "timeout"),
        ),
    )
    result = BookPricingResult.from_offers(offers, offer, None)
    outcome = _book_result(BookMarketCache(collection_status="partial"), result)
    assert outcome.refreshed
    assert outcome.verdict == "degraded"
    assert outcome.attempts[0].status == "success"
    assert outcome.attempts[1].operation == "history"


def test_reused_cache_does_not_count_as_a_new_observation():
    from app.db.models.book_market_cache import BookMarketCache
    from app.domain.enums import ComparisonStatus
    from app.tasks.refresh_book_prices import _book_result

    cached = BookMarketCache(
        collection_status="complete", comparison_status=ComparisonStatus.AVAILABLE
    )
    assert not _book_result(cached, None).refreshed

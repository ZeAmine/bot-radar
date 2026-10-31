"""Une passe ne parle que pour un fait actionnable ; le reste est silence.

Le bilan de passe à quatre compteurs a disparu, avec ses modes « incrémental » et
« complet » : aucun de ses nombres ne permettait de décider quoi que ce soit. Ce
qui reste immédiat, c'est le fait — une source durablement en échec, un
rétablissement, une passe interrompue par une exception —, et rien d'autre.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.providers.base import CollectionAttempt
from app.services import market_health


def _failed(reason: str = "timeout") -> CollectionAttempt:
    return CollectionAttempt("momox", status="failed", reason=reason)


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


def result(*, occurrence="pass-1", attempts=()):
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
        occurrence=occurrence,
        attempts=list(attempts),
    )


@pytest.mark.asyncio
async def test_a_source_incident_is_named_once_and_carries_no_counter(notifier):
    """Contrat : le message nomme la source et s'arrête là — aucun compteur de passe."""
    report = result(attempts=[_failed(), CollectionAttempt("gibert", status="skipped")])
    report.never_collected = 2
    report.stale_current = 1
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(report)
        await market_health.notify_refresh_result(report)
    assert notifier.await_count == 1
    text = notifier.call_args.args[0]
    assert "momox" in text
    for counter in ("Actualisées", "Traités", "Jamais collectés", "Non parcourus",
                    "Restants à traiter", "incrémental", "complet"):
        assert counter not in text


@pytest.mark.asyncio
async def test_an_incident_stays_silent_while_it_lasts_and_reports_its_recovery(notifier):
    """Contrat : un incident déjà annoncé ne se répète pas ; son rétablissement se dit."""
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(result(attempts=[_failed()]))
        await market_health.notify_refresh_result(result(attempts=[_failed()]))
        await market_health.notify_refresh_result(result(attempts=[]))
        await market_health.notify_refresh_result(
            result(attempts=[CollectionAttempt("momox", status="skipped")])
        )
        assert notifier.await_count == 1
        await market_health.notify_refresh_result(
            result(attempts=[CollectionAttempt("momox")])
        )
    assert notifier.await_count == 2
    assert "Rétablissement" in notifier.call_args.args[0]


@pytest.mark.asyncio
async def test_failed_notification_can_be_retried(notifier):
    """Contrat : un incident non remis n'est pas perdu, la tentative suivante le renvoie."""
    notifier.side_effect = [False, True]
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(result(attempts=[_failed()]))
        await market_health.notify_refresh_result(result(attempts=[_failed()]))
    assert notifier.await_count == 2


@pytest.mark.asyncio
async def test_new_incident_aggravation_and_recovery_with_redis_down(notifier, monkeypatch):
    client = AsyncMock()
    client.get.side_effect = ConnectionError
    client.set.side_effect = ConnectionError
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    partial = [
        _failed(),
        CollectionAttempt("momox"),
        CollectionAttempt("momox"),
    ]
    worse = [_failed()] * 3
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(result(attempts=partial))
        await market_health.notify_refresh_result(result(attempts=worse))
        assert "Aggravation" in notifier.call_args.args[0]
        await market_health.notify_refresh_result(result(attempts=worse))
        assert notifier.await_count == 2
        await market_health.notify_refresh_result(
            result(attempts=[CollectionAttempt("momox")])
        )
    assert notifier.await_count == 3


@pytest.mark.asyncio
async def test_a_pass_without_a_single_fact_says_nothing(notifier):
    """Contrat : zéro livre traité et rien à signaler, c'est une passe qui se tait."""
    report = result()
    report.total = 0
    report.stats = dict.fromkeys(report.stats, 0)
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(report)
    assert notifier.await_count == 0


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
    # Contrat : l'interruption par exception est un fait, et c'est la seule ligne.
    assert notifier.call_args.args[0] == (
        "⚠️ Passe de rafraîchissement interrompue avant la fin."
    )


@pytest.mark.asyncio
async def test_a_pass_cut_by_the_ceiling_is_the_normal_case_and_is_silent(notifier):
    """Contrat : une passe coupée par le plafond de durée ne dit plus rien du tout.

    Le message partait deux fois par jour pour décrire un fonctionnement prévu ;
    la durée d'une passe n'est pas un fait de marché.
    """
    report = result()
    report.interrupted = True
    report.stats["degraded"] = 0
    with patch.object(market_health, "notify_admins", notifier):
        await market_health.notify_refresh_result(report)
    assert notifier.await_count == 0


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

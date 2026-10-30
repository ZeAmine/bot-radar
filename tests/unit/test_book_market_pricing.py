"""Tests unitaires du point de cotation unique des livres.

Ces tests verrouillent les deux défauts qui faisaient partir une alerte sans prix :

- la recherche par ISBN ne retombait sur le titre que si on le fournissait — et la
  création d'alerte ne le fournissait pas, donc une recherche ISBN vide n'écrivait
  aucune cote ;
- le plus bas constaté sur 1 an n'était lu que sur la meilleure offre, ce qui le
  faisait disparaître dès qu'un autre marchand remontait moins cher.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
from app.services import book_market_pricing as pricing
from app.services.book_market_pricing import (
    BookPricingResult,
    _stamp_collection,
    fetch_offers_for_book,
    persist_market_result,
    price_book,
    price_book_now,
    pricing_stat,
    schedule_book_pricing,
)
from app.services.market_price_service import _current_collection_is_empty
from app.services.market_refresh_policy import component_due


@pytest.fixture(autouse=True)
def synthetic_reservations(monkeypatch):
    monkeypatch.setattr(pricing.BookRepository, "plan_price_refreshes", AsyncMock())
    monkeypatch.setattr(pricing, "_reserve_next_refresh", AsyncMock())


ISBN = "9782917501245"
TITRE = "Les Dépressifs anonymes"


def _book(isbn13: str | None = ISBN) -> Book:
    return Book(
        id=uuid.uuid4(),
        title=TITRE,
        normalized_title="les depressifs anonymes",
        isbn13=isbn13,
        author="Riad Sattouf",
    )


def _offer(
    provider: str,
    delivered_cents: int,
    *,
    historical_low: int | None = None,
    merchant: str | None = None,
    condition: str = "Très bon état",
    is_available: bool = True,
) -> MarketOffer:
    return MarketOffer(
        provider_name=provider,
        item_price_cents=delivered_cents,
        shipping_price_cents=None,
        delivered_price_cents=delivered_cents,
        source_url=f"https://{provider}.example/offre/{delivered_cents}",
        price_includes_shipping=True,
        item_condition=condition,
        confidence=MatchConfidence.HIGH,
        is_available=is_available,
        is_comparable=True,
        raw_payload={
            "best_merchant": merchant or provider,
            "historical_low_1y_cents": historical_low,
        },
    )


def _registry(offers: list[MarketOffer]) -> MagicMock:
    registry = MagicMock()
    registry.fetch_all_offers = AsyncMock(return_value=offers)
    return registry


def _current_cache(now: datetime) -> BookMarketCache:
    old = now - timedelta(days=2)
    return BookMarketCache(
        book_id=uuid.uuid4(),
        lowest_delivered_price_cents=1000,
        best_provider_name="momox",
        best_source_url="https://momox.example/1000",
        confidence=MatchConfidence.HIGH,
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=old,
        expires_at=now + timedelta(hours=2),
        last_successful_collection_at=old,
        last_attempt_at=old,
        collection_status="complete",
        current_offers=[{
            "provider_name": "momox", "delivered_price_cents": 1000,
            "source_url": "https://momox.example/1000", "item_condition": "Très bon état",
        }],
        failed_provider_names=[],
        refresh_state={"current": {
            "due_at": (old + timedelta(days=7)).isoformat(), "failures": 0,
        }},
    )


@pytest.mark.asyncio
async def test_fetch_always_pairs_isbn_with_title() -> None:
    """Le titre accompagne l'ISBN : un repli par titre doit rester possible.

    C'est la régression qui laissait une alerte sans cote : la création passait
    `title=""`, et le fournisseur ne pouvait alors chercher l'ouvrage que par son code.
    """
    registry = _registry([])

    await fetch_offers_for_book(_book(), registry=registry)

    kwargs = registry.fetch_all_offers.call_args.kwargs
    assert kwargs["isbn13"] == ISBN
    assert kwargs["title"] == TITRE


@pytest.mark.asyncio
async def test_fetch_drops_an_invalid_isbn_but_keeps_the_title() -> None:
    """Un ISBN dont la clé de contrôle est fausse ne part jamais chez un fournisseur."""
    registry = _registry([])

    await fetch_offers_for_book(_book("123"), registry=registry)

    kwargs = registry.fetch_all_offers.call_args.kwargs
    assert kwargs["isbn13"] is None
    assert kwargs["title"] == TITRE


@pytest.mark.asyncio
async def test_price_book_keeps_the_lowest_historical_across_merchants() -> None:
    """Le plus bas 1 an est lu sur toutes les offres, pas sur la seule meilleure.

    Le meilleur prix du jour venait ici d'un connecteur direct, qui ne porte pas
    d'historique ; c'est l'agrégateur, plus cher aujourd'hui, qui le certifiait.
    """
    cheapest = _offer("momox", 1000)
    aggregator = _offer("chasse_aux_livres", 1200, historical_low=800)
    written = BookMarketCache(
        id=uuid.uuid4(),
        book_id=uuid.uuid4(),
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    market_repo = MagicMock()
    market_repo.refresh_annual_references = AsyncMock()
    market_repo.save_snapshot = AsyncMock()
    market_repo.get_cache_by_book_id = AsyncMock(return_value=None)
    market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
    market_repo.update_or_create_cache = AsyncMock(return_value=written)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: market_repo
        )
        cache = await price_book(
            AsyncMock(),
            _book(),
            registry=_registry(MarketOffers(
                [cheapest, aggregator],
                attempts=(
                    CollectionAttempt("momox", "current"),
                    CollectionAttempt("chasse_aux_livres", "current"),
                ),
            )),
        )

    kwargs = market_repo.update_or_create_cache.call_args.kwargs
    assert kwargs["lowest_delivered_price_cents"] == 1000
    assert kwargs["comparison_status"] == ComparisonStatus.AVAILABLE
    assert kwargs["historical_low_1y_cents"] == 800
    assert cache is written


@pytest.mark.asyncio
async def test_price_book_with_a_real_registry_uses_partial_cal_and_a_healthy_source() -> None:
    from app.providers.registry import ProviderRegistry

    class PartialCal:
        name = "chasse_aux_livres"

        async def get_offers(self, **_kwargs):
            return MarketOffers(
                [_offer(self.name, 1500)],
                failed_providers=(self.name,),
                attempts=(CollectionAttempt(
                    self.name, "current", "partial", "incomplete_offers",
                ),),
            )

    class HealthySource:
        name = "momox"

        async def get_offers(self, **_kwargs):
            return MarketOffers(
                [_offer(self.name, 1000)],
                attempts=(CollectionAttempt(self.name, "current"),),
            )

    registry = ProviderRegistry(providers=[PartialCal(), HealthySource()])
    written = BookMarketCache(
        id=uuid.uuid4(),
        book_id=uuid.uuid4(),
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    market_repo = MagicMock()
    market_repo.refresh_annual_references = AsyncMock()
    market_repo.save_snapshot = AsyncMock()
    market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
    market_repo.update_or_create_cache = AsyncMock(return_value=written)
    results: list[BookPricingResult] = []

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: market_repo
        )
        cache = await price_book(
            AsyncMock(), _book(), registry=registry, result_sink=results,
        )

    result = results[0]
    assert result.current_collection_complete is False
    assert result.current_collection_usable is True
    assert result.lowest_delivered_price_cents == 1000
    assert cache is written
    assert written.last_successful_collection_at == result.collected_at
    assert written.refresh_state["current"]["failures"] == 0
    assert market_repo.update_or_create_cache.call_args.kwargs[
        "lowest_delivered_price_cents"
    ] == 1000


@pytest.mark.asyncio
async def test_price_book_does_not_advance_for_partial_cal_and_empty_healthy_source() -> None:
    from app.providers.registry import ProviderRegistry

    class PartialCal:
        name = "chasse_aux_livres"

        async def get_offers(self, **_kwargs):
            return MarketOffers(
                [_offer(self.name, 1500)],
                failed_providers=(self.name,),
                attempts=(CollectionAttempt(
                    self.name, "current", "partial", "incomplete_offers",
                ),),
            )

    class HealthyEmpty:
        name = "momox"

        async def get_offers(self, **_kwargs):
            return MarketOffers(
                [],
                attempts=(CollectionAttempt(self.name, "current"),),
            )

    old = datetime.now(UTC) - timedelta(days=8)
    registry = ProviderRegistry(providers=[PartialCal(), HealthyEmpty()])
    written = BookMarketCache(
        id=uuid.uuid4(),
        book_id=uuid.uuid4(),
        last_successful_collection_at=old,
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    market_repo = MagicMock()
    market_repo.refresh_annual_references = AsyncMock()
    market_repo.save_snapshot = AsyncMock()
    market_repo.get_cache_by_book_id = AsyncMock(return_value=None)
    market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
    market_repo.update_or_create_cache = AsyncMock(return_value=written)
    results: list[BookPricingResult] = []

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: market_repo
        )
        cache = await price_book(
            AsyncMock(), _book(), registry=registry, result_sink=results,
        )

    result = results[0]
    assert result.current_collection_complete is False
    assert result.current_collection_usable is False
    assert result.lowest_delivered_price_cents is None
    assert cache is written
    assert written.last_successful_collection_at == old
    assert written.refresh_state["current"]["failures"] == 1
    assert component_due(written, "current", result.collected_at) == (
        result.collected_at + timedelta(hours=6)
    )


@pytest.mark.asyncio
async def test_price_book_saves_a_silent_book_as_unavailable() -> None:
    """Aucun marchand ne répond : la cote est marquée indisponible, pas laissée vide."""
    market_repo = MagicMock()
    market_repo.refresh_annual_references = AsyncMock()
    market_repo.save_snapshot = AsyncMock()
    market_repo.get_cache_by_book_id = AsyncMock(return_value=None)
    market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
    market_repo.update_or_create_cache = AsyncMock(
        return_value=BookMarketCache(
            id=uuid.uuid4(),
            book_id=uuid.uuid4(),
            comparison_status=ComparisonStatus.UNAVAILABLE,
        )
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: market_repo
        )
        await price_book(AsyncMock(), _book(), registry=_registry([]))

    kwargs = market_repo.update_or_create_cache.call_args.kwargs
    assert kwargs["comparison_status"] == ComparisonStatus.UNAVAILABLE
    assert kwargs["lowest_delivered_price_cents"] is None
    assert kwargs["historical_low_1y_cents"] is None


@pytest.mark.asyncio
async def test_price_book_keeps_history_without_stock() -> None:
    """Plus rien en stock mais un historique certifié : la cote vaut le plus bas 1 an."""
    exhausted = _offer("chasse_aux_livres", 0, historical_low=900, is_available=False)
    result = BookPricingResult.from_offers(
        offers=[exhausted],
        best_offer=None,
        historical_low_cents=900,
    )

    assert result.comparison_status == ComparisonStatus.UNAVAILABLE
    assert result.historical_low_1y_cents == 900
    assert result.has_data is True


@pytest.mark.asyncio
async def test_persist_keeps_a_fresh_price_when_every_provider_is_silent() -> None:
    """Un silence réseau ne doit pas priver l'utilisateur d'un prix encore frais."""
    book = _book()
    expires = datetime.now(UTC) + timedelta(hours=2)
    existing = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book.id,
        lowest_delivered_price_cents=470,
        expires_at=expires,
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    market_repo = MagicMock()
    market_repo.refresh_annual_references = AsyncMock()
    market_repo.save_snapshot = AsyncMock()
    market_repo.get_cache_by_book_id = AsyncMock(return_value=existing)
    market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
    market_repo.update_or_create_cache = AsyncMock()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: market_repo
        )
        cache = await persist_market_result(
            AsyncMock(),
            book.id,
            BookPricingResult.from_offers(offers=[], best_offer=None, historical_low_cents=None),
        )

    market_repo.update_or_create_cache.assert_not_called()
    assert cache is existing
    assert cache.expires_at == expires


@pytest.mark.asyncio
async def test_price_book_now_swallows_a_provider_failure() -> None:
    """Un marchand muet ne doit pas faire échouer l'écran qui a demandé la cote."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.db.session.AsyncSessionLocal", MagicMock())
        mp.setattr(
            "app.services.book_market_pricing.BookRepository",
            MagicMock(return_value=MagicMock(get_by_id=AsyncMock(side_effect=RuntimeError("pg")))),
        )
        assert await price_book_now(uuid.uuid4(), reason="market_button") is None


@pytest.mark.asyncio
async def test_schedule_prices_a_book_once_at_a_time() -> None:
    """Deux annonces de la même BD ne déclenchent qu'une salve chez les marchands."""
    book_id = uuid.uuid4()
    started: list[object] = []

    def _capture(coro, name=None):
        started.append((coro, name))
        return None

    async def _drain() -> None:
        pending, started[:] = list(started), []
        for coro, _name in pending:
            await coro

    price_now = AsyncMock(return_value=None)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.book_market_pricing.safe_create_task", _capture)
        mp.setattr("app.services.book_market_pricing.price_book_now", price_now)

        schedule_book_pricing(book_id, reason="alert_created")
        schedule_book_pricing(book_id, reason="alert_sent")
        assert [name for _coro, name in started] == [f"market_price_{book_id}"]
        assert book_id in pricing._pricing_in_flight

        await _drain()
        # Une seule cotation réellement partie pour les deux demandes.
        price_now.assert_awaited_once_with(book_id, reason="alert_created")
        assert book_id not in pricing._pricing_in_flight

        # La garde ne porte que sur le passage en cours : le livre redevient éligible.
        schedule_book_pricing(book_id, reason="alert_created")
        assert [name for _coro, name in started] == [f"market_price_{book_id}"]
        await _drain()

    assert price_now.await_count == 2
    assert book_id not in pricing._pricing_in_flight


async def test_fetch_never_dates_an_undated_historical_point():
    historical = _offer("chasse_aux_livres", 545, is_available=False)
    historical.is_historical = True
    current = _offer("ebay", 1103)
    offers = await fetch_offers_for_book(_book(), registry=_registry([historical, current]))
    assert offers[0].observed_at is None
    assert offers[1].observed_at is not None


@pytest.mark.asyncio
async def test_a_successful_check_advances_the_date_even_when_the_price_is_unchanged():
    """Une vérification réussie compte même si le montant reste identique."""
    book = _book()
    previous = datetime.now(UTC) - timedelta(days=1)
    collected_at = previous + timedelta(hours=12)
    existing = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book.id,
        lowest_delivered_price_cents=470,
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=previous,
        last_successful_collection_at=previous,
        last_attempt_at=previous,
    )
    repo = MagicMock()
    repo.save_snapshot = AsyncMock()
    repo.refresh_annual_references = AsyncMock()
    repo.update_or_create_cache = AsyncMock(return_value=existing)
    repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])

    offer = _offer("synthetic", 470)
    offers = MarketOffers(
        [offer], attempts=(CollectionAttempt("synthetic", "current"),),
    )
    result = BookPricingResult.from_offers(
        offers=offers, best_offer=offer, historical_low_cents=None
    )
    result = result.__class__(**{**result.__dict__, "collected_at": collected_at})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: repo
        )
        cache = await persist_market_result(AsyncMock(), book.id, result)

    assert cache.last_successful_collection_at == collected_at
    assert cache.last_attempt_at == collected_at
    assert cache.lowest_delivered_price_cents == 470


@pytest.mark.asyncio
async def test_a_successful_check_with_a_new_price_advances_the_date():
    """Un prix modifié et un prix identique datent tous deux la vérification réussie."""
    book = _book()
    previous = datetime.now(UTC) - timedelta(days=1)
    collected_at = previous + timedelta(hours=12)
    updated = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book.id,
        lowest_delivered_price_cents=600,
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=previous,
        last_successful_collection_at=previous,
        last_attempt_at=previous,
    )
    repo = MagicMock()
    repo.save_snapshot = AsyncMock()
    repo.refresh_annual_references = AsyncMock()
    repo.update_or_create_cache = AsyncMock(return_value=updated)
    repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])

    offer = _offer("synthetic", 600)
    offers = MarketOffers(
        [offer], attempts=(CollectionAttempt("synthetic", "current"),),
    )
    result = BookPricingResult.from_offers(
        offers=offers, best_offer=offer, historical_low_cents=None
    )
    result = result.__class__(**{**result.__dict__, "collected_at": collected_at})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: repo
        )
        cache = await persist_market_result(AsyncMock(), book.id, result)

    assert cache.last_successful_collection_at == collected_at
    assert cache.lowest_delivered_price_cents == 600


@pytest.mark.asyncio
async def test_a_failed_attempt_after_a_success_keeps_the_successful_date():
    """Un échec ne remet pas le compteur de fraîcheur à zéro."""
    book = _book()
    previous = datetime.now(UTC) - timedelta(days=1)
    collected_at = previous + timedelta(hours=12)
    existing = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book.id,
        lowest_delivered_price_cents=470,
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=previous,
        last_successful_collection_at=previous,
        last_attempt_at=previous,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    repo = MagicMock()
    repo.save_snapshot = AsyncMock()
    repo.refresh_annual_references = AsyncMock()
    repo.get_cache_by_book_id = AsyncMock(return_value=existing)
    repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])

    result = BookPricingResult(
        offers=[],
        lowest_delivered_price_cents=None,
        best_provider_name=None,
        best_source_url=None,
        confidence=None,
        comparison_status=ComparisonStatus.UNAVAILABLE,
        cache_duration_hours=24,
        historical_low_1y_cents=None,
        failed_providers=("synthetic",),
        collected_at=collected_at,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: repo
        )
        cache = await persist_market_result(AsyncMock(), book.id, result)

    assert cache.last_successful_collection_at == previous
    assert cache.last_attempt_at == collected_at
    assert cache.lowest_delivered_price_cents == 470


@pytest.mark.parametrize('internal_retries', [0, 2])
async def test_creation_retries_empty_response_once_without_stacking_provider_budgets(
    monkeypatch, internal_retries,
):
    from app.providers.base import CollectionAttempt, MarketOffers
    from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
    from app.providers.registry import ProviderRegistry

    empty = MarketOffers(failed_providers=('chasse_aux_livres',), attempts=(
        CollectionAttempt('chasse_aux_livres', 'current', 'failed', 'empty_response', 200,
                          stage='search_results'),
    ))
    success = MarketOffers([_offer('chasse_aux_livres', 1200)])
    provider = ChasseAuxLivresProvider(max_retries=internal_retries)
    fetch = AsyncMock(side_effect=[empty, success])
    monkeypatch.setattr(provider, 'get_offers', fetch)
    sleeper = AsyncMock()
    monkeypatch.setattr(pricing.asyncio, 'sleep', sleeper)
    registry = ProviderRegistry(providers=[provider])
    result = await fetch_offers_for_book(_book(), registry, retry_transient=True)
    if internal_retries:
        assert result == []
        assert fetch.await_count == 1
        sleeper.assert_not_awaited()
    else:
        assert result[0].delivered_price_cents == 1200
        assert fetch.await_count == 2
        sleeper.assert_awaited_once_with(1.5)


async def test_retrying_another_provider_keeps_the_unretried_cal_failure(monkeypatch):
    from app.providers.base import CollectionAttempt, MarketOffers
    from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
    from app.providers.registry import ProviderRegistry

    cal = ChasseAuxLivresProvider(max_retries=2)
    cal_fetch = AsyncMock(return_value=MarketOffers(
        failed_providers=(cal.name,), attempts=(
            CollectionAttempt(cal.name, 'current', 'failed', 'empty_response', 200),
        ),
    ))
    monkeypatch.setattr(cal, 'get_offers', cal_fetch)

    class Other:
        name = 'momox'
        get_offers = AsyncMock(side_effect=[
            MarketOffers(failed_providers=('momox',), attempts=(
                CollectionAttempt('momox', 'current', 'failed', 'timeout'),
            )),
            MarketOffers([_offer('momox', 1100)]),
        ])

    other = Other()
    registry = ProviderRegistry(providers=[cal, other])
    result = await fetch_offers_for_book(_book(), registry, retry_transient=True)
    assert 'chasse_aux_livres' in result.failed_providers
    assert any(a.provider == cal.name and a.status == 'failed' for a in result.attempts)
    assert cal_fetch.await_count == 1
    assert other.get_offers.await_count == 2


@pytest.mark.parametrize(('status', 'with_offer', 'succeeded', 'expected_price'), [
    ('success', True, True, 600),
    ('success', False, True, None),
    ('partial', True, False, None),
    ('partial', False, False, None),
    ('failed', False, False, None),
    ('failed', True, False, None),
])
def test_current_result_distinguishes_complete_empty_partial_and_technical_failure(
    status, with_offer, succeeded, expected_price,
):
    from app.providers.base import CollectionAttempt, MarketOffers

    offer = _offer('synthetic', 600)
    offers = MarketOffers([offer] if with_offer else [],
        failed_providers=() if status == 'success' else ('synthetic',),
        attempts=(CollectionAttempt('synthetic', status=status),))
    result = BookPricingResult.from_offers(offers, offer if with_offer else None, None)
    assert result.current_collection_succeeded is succeeded
    assert result.lowest_delivered_price_cents == expected_price


def test_partial_current_source_with_a_confirmed_price_is_usable() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
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
    result = BookPricingResult.from_offers(offers, healthy, None)
    cache = BookMarketCache(last_successful_collection_at=old)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert result.current_collection_complete is False
    assert result.current_collection_succeeded is False
    assert result.current_collection_usable is True
    assert result.price_confirmed is True
    assert cache.last_successful_collection_at == now
    assert cache.refresh_state["current"] == {
        "due_at": (now + timedelta(days=7)).isoformat(),
        "failures": 0,
    }
    assert cache.collection_status == "partial"
    assert component_due(cache, "current", now) == now + timedelta(days=7)


def test_partial_current_source_without_a_confirmed_price_stays_retryable() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    offers = MarketOffers(
        [],
        failed_providers=("chasse_aux_livres",),
        attempts=(
            CollectionAttempt("chasse_aux_livres", "current", "partial", "incomplete_offers"),
            CollectionAttempt("momox", "current"),
        ),
    )
    result = BookPricingResult.from_offers(offers, None, None)
    cache = BookMarketCache(last_successful_collection_at=old)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert result.current_collection_complete is False
    assert result.current_collection_usable is False
    assert cache.last_successful_collection_at == old
    assert cache.refresh_state["current"] == {
        "due_at": (now + timedelta(hours=6)).isoformat(),
        "failures": 1,
    }
    assert cache.collection_status == "partial"
    assert component_due(cache, "current", now) == now + timedelta(hours=6)


def test_confirmed_current_price_wins_over_a_retained_partial_cal_offer() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    retained = {
        "provider_name": "chasse_aux_livres", "item_price_cents": 800,
        "shipping_price_cents": None, "delivered_price_cents": 800,
        "source_url": "https://chasse.example/800", "item_condition": "Très bon état",
    }
    cache = BookMarketCache(
        book_id=uuid.uuid4(), current_offers=[retained],
        lowest_delivered_price_cents=1000, best_provider_name="momox",
        best_source_url="https://momox.example/1000",
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=old, expires_at=now + timedelta(hours=2),
        last_successful_collection_at=old, last_attempt_at=old,
    )
    cal = _offer("chasse_aux_livres", 1500)
    healthy = _offer("momox", 1000)
    offers = MarketOffers(
        [cal, healthy], failed_providers=("chasse_aux_livres",),
        attempts=(
            CollectionAttempt("chasse_aux_livres", "current", "partial", "incomplete_offers"),
            CollectionAttempt("momox", "current"),
        ),
    )
    result = BookPricingResult.from_offers(offers, healthy, None)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert result.price_confirmed is True
    assert cache.lowest_delivered_price_cents == 1000
    assert cache.best_provider_name == "momox"
    assert [offer["provider_name"] for offer in cache.current_offers] == ["momox"]
    assert cache.last_successful_collection_at == now


def test_partial_cal_alone_does_not_replace_the_last_known_price() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    retained = {
        "provider_name": "chasse_aux_livres", "item_price_cents": 800,
        "shipping_price_cents": None, "delivered_price_cents": 800,
        "source_url": "https://chasse.example/800", "item_condition": "Très bon état",
    }
    cache = BookMarketCache(
        book_id=uuid.uuid4(), current_offers=[retained],
        lowest_delivered_price_cents=800, best_provider_name="chasse_aux_livres",
        best_source_url="https://chasse.example/800",
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=old, expires_at=now + timedelta(hours=2),
        last_successful_collection_at=old, last_attempt_at=old,
    )
    cal = _offer("chasse_aux_livres", 1500)
    offers = MarketOffers(
        [cal], failed_providers=("chasse_aux_livres",),
        attempts=(
            CollectionAttempt("chasse_aux_livres", "current", "partial", "incomplete_offers"),
        ),
    )
    result = BookPricingResult.from_offers(offers, cal, None)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert result.lowest_delivered_price_cents is None
    assert cache.lowest_delivered_price_cents == 800
    assert cache.current_offers == [retained]
    assert cache.last_successful_collection_at == old
    assert component_due(cache, "current", now) == now + timedelta(hours=6)


def test_fully_successful_collection_replaces_the_known_offers_normally() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    cache = BookMarketCache(
        book_id=uuid.uuid4(), current_offers=[{
            "provider_name": "chasse_aux_livres", "delivered_price_cents": 800,
            "source_url": "https://chasse.example/800", "item_condition": "Très bon état",
        }],
        lowest_delivered_price_cents=1000, best_provider_name="momox",
        best_source_url="https://momox.example/offre/1000",
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=old, expires_at=now + timedelta(hours=2),
        last_successful_collection_at=old, last_attempt_at=old,
    )
    healthy = _offer("momox", 1000)
    offers = MarketOffers([healthy], attempts=(CollectionAttempt("momox", "current"),))
    result = BookPricingResult.from_offers(offers, healthy, None)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert cache.lowest_delivered_price_cents == 1000
    assert [offer["provider_name"] for offer in cache.current_offers] == ["momox"]
    assert cache.collection_status == "complete"
    assert cache.last_successful_collection_at == now


def test_failed_collection_without_a_price_keeps_the_last_known_offers() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    retained = {
        "provider_name": "chasse_aux_livres", "delivered_price_cents": 800,
        "source_url": "https://chasse.example/800", "item_condition": "Très bon état",
    }
    cache = BookMarketCache(
        book_id=uuid.uuid4(), current_offers=[retained],
        lowest_delivered_price_cents=800, best_provider_name="chasse_aux_livres",
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=old, expires_at=now + timedelta(hours=2),
        last_successful_collection_at=old, last_attempt_at=old,
    )
    result = BookPricingResult(
        offers=[], lowest_delivered_price_cents=None, best_provider_name=None,
        best_source_url=None, confidence=None, comparison_status=ComparisonStatus.UNAVAILABLE,
        cache_duration_hours=6, historical_low_1y_cents=None,
        failed_providers=("synthetic",), collected_at=now,
    )

    _stamp_collection(cache, result)

    assert cache.current_offers == [retained]
    assert cache.lowest_delivered_price_cents == 800
    assert cache.last_successful_collection_at == old
    assert cache.last_attempt_at == now
    assert cache.collection_status == "failed"


@pytest.mark.parametrize("with_point", [True, False])
def test_history_only_success_preserves_current_state(with_point: bool) -> None:
    now = datetime.now(UTC)
    cache = _current_cache(now)
    current_offers = list(cache.current_offers or [])
    current_due = component_due(cache, "current", now)
    checked_at = cache.checked_at
    expires_at = cache.expires_at
    point = replace(
        _offer("chasse_aux_livres", 900), is_historical=True, is_available=False,
        item_condition="Très bon état +", observed_at=now - timedelta(days=3),
    )
    offers = MarketOffers(
        [point] if with_point else [], historical_checked=True,
        attempts=(
            CollectionAttempt("chasse_aux_livres", "current", "skipped"),
            CollectionAttempt("chasse_aux_livres", "history"),
        ),
    )
    result = BookPricingResult.from_offers(
        offers, None, 900 if with_point else None,
    )

    _stamp_collection(cache, replace(result, collected_at=now))

    assert cache.lowest_delivered_price_cents == 1000
    assert cache.current_offers == current_offers
    assert cache.collection_status == "complete"
    assert cache.last_successful_collection_at == now - timedelta(days=2)
    assert cache.last_attempt_at == now - timedelta(days=2)
    assert cache.checked_at == checked_at
    assert cache.expires_at == expires_at
    assert cache.historical_checked_at == now
    assert component_due(cache, "current", now) == current_due
    assert pricing_stat(cache) == "available"
    assert _current_collection_is_empty(cache) is False
    if with_point:
        assert cache.historical_low_1y_cents == 900
        assert cache.source_history is not None
        assert cache.source_history["price_cents"] == 900
    else:
        assert cache.historical_low_1y_cents is None
        assert cache.source_history is None


def test_history_only_failure_preserves_current_state() -> None:
    now = datetime.now(UTC)
    cache = _current_cache(now)
    current_offers = list(cache.current_offers or [])
    current_due = component_due(cache, "current", now)
    old_checked = cache.historical_checked_at
    offers = MarketOffers(
        failed_providers=("chasse_aux_livres",),
        attempts=(
            CollectionAttempt("chasse_aux_livres", "current", "skipped"),
            CollectionAttempt("chasse_aux_livres", "history", "failed", "timeout"),
        ),
    )
    result = BookPricingResult.from_offers(offers, None, None)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert cache.lowest_delivered_price_cents == 1000
    assert cache.current_offers == current_offers
    assert cache.collection_status == "complete"
    assert cache.last_successful_collection_at == now - timedelta(days=2)
    assert cache.last_attempt_at == now - timedelta(days=2)
    assert cache.historical_checked_at == old_checked
    assert component_due(cache, "current", now) == current_due
    assert cache.refresh_state["history"]["failures"] == 1
    assert pricing_stat(cache) == "available"
    assert _current_collection_is_empty(cache) is False


def test_a_retried_current_does_not_back_off_the_history_it_never_asked() -> None:
    """La reprise d'une collecte ne redemande que `current`.

    Les tentatives fusionnées ne portent alors aucune composante `history` : ce
    silence n'est pas un échec de l'historique, qui doit rester dû pour la passe
    suivante au lieu de reculer de six heures.
    """
    now = datetime.now(UTC)
    cache = _current_cache(now)
    interesting = _offer("momox", 1000)
    offers = MarketOffers(
        [interesting],
        attempts=(
            CollectionAttempt("chasse_aux_livres", "current"),
            CollectionAttempt("momox", "current"),
        ),
    )
    result = BookPricingResult.from_offers(offers, interesting, None)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert "history" not in (cache.refresh_state or {})
    assert cache.historical_checked_at is None
    assert component_due(cache, "history", now) <= now


def test_combined_current_and_history_updates_both_components() -> None:
    now = datetime.now(UTC)
    cache = _current_cache(now)
    healthy = _offer("momox", 1000)
    point = replace(
        _offer("chasse_aux_livres", 900), is_historical=True, is_available=False,
        item_condition="Très bon état +", observed_at=now - timedelta(days=3),
    )
    offers = MarketOffers(
        [healthy, point], historical_checked=True,
        attempts=(
            CollectionAttempt("momox", "current"),
            CollectionAttempt("chasse_aux_livres", "history"),
        ),
    )
    result = BookPricingResult.from_offers(offers, healthy, 900)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert cache.lowest_delivered_price_cents == 1000
    assert [offer["provider_name"] for offer in cache.current_offers] == ["momox"]
    assert cache.collection_status == "complete"
    assert cache.last_successful_collection_at == now
    assert cache.last_attempt_at == now
    assert cache.historical_checked_at == now
    assert cache.historical_low_1y_cents == 900
    assert cache.source_history is not None
    assert cache.source_history["price_cents"] == 900


def test_partial_current_source_alone_stays_retryable() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    cal = _offer("chasse_aux_livres", 1500)
    offers = MarketOffers(
        [cal],
        failed_providers=("chasse_aux_livres",),
        attempts=(
            CollectionAttempt("chasse_aux_livres", "current", "partial", "incomplete_offers"),
        ),
    )
    result = BookPricingResult.from_offers(offers, None, None)
    cache = BookMarketCache(last_successful_collection_at=old)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert result.current_collection_complete is False
    assert result.current_collection_usable is False
    assert cache.last_successful_collection_at == old
    assert cache.refresh_state["current"]["failures"] == 1
    assert component_due(cache, "current", now) == now + timedelta(hours=6)


def test_a_price_derived_from_a_partial_cal_offer_is_not_usable() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    cal = _offer("chasse_aux_livres", 1500)
    offers = MarketOffers(
        [cal],
        failed_providers=("chasse_aux_livres",),
        attempts=(
            CollectionAttempt("chasse_aux_livres", "current", "partial", "incomplete_offers"),
        ),
    )
    result = BookPricingResult.from_offers(offers, cal, None)
    cache = BookMarketCache(last_successful_collection_at=old)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert result.lowest_delivered_price_cents is None
    assert result.best_provider_name is None
    assert result.price_confirmed is False
    assert result.current_collection_usable is False
    assert cache.last_successful_collection_at == old
    assert cache.refresh_state["current"]["failures"] == 1
    assert component_due(cache, "current", now) == now + timedelta(hours=6)


def test_empty_current_collection_is_usable_only_when_every_source_succeeded() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    offers = MarketOffers(
        [],
        attempts=(CollectionAttempt("momox", "current"),),
    )
    result = BookPricingResult.from_offers(offers, None, None)
    cache = BookMarketCache(last_successful_collection_at=old)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert result.current_collection_complete is True
    assert result.current_collection_succeeded is True
    assert result.current_collection_usable is True
    assert cache.last_successful_collection_at == now
    assert cache.refresh_state["current"]["failures"] == 0
    assert component_due(cache, "current", now) == now + timedelta(days=7)


def test_failed_current_source_keeps_a_confirmed_price_retryable() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    healthy = _offer("momox", 1000)
    offers = MarketOffers(
        [healthy],
        failed_providers=("gibert",),
        attempts=(
            CollectionAttempt("momox", "current"),
            CollectionAttempt("gibert", "current", "failed", "timeout"),
        ),
    )
    result = BookPricingResult.from_offers(offers, healthy, None)
    cache = BookMarketCache(last_successful_collection_at=old)

    _stamp_collection(cache, replace(result, collected_at=now))

    assert result.current_collection_complete is False
    assert result.current_collection_usable is False
    assert cache.last_successful_collection_at == old
    assert cache.refresh_state["current"]["failures"] == 1
    assert component_due(cache, "current", now) == now + timedelta(hours=6)

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.core.constants import ComparisonStatus
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
from app.services.book_market_pricing import BookPricingResult, _stamp_collection
from app.services.market_price_service import (
    MarketPriceService,
    reference_price,
    series_reference_total,
)
from app.services.market_refresh_policy import (
    collection_due,
    component_due,
    component_success_at,
)
from app.services.tome_extractor import TomeMatchResult


def source_result(price: int = 921) -> BookPricingResult:
    now = datetime.now(UTC)
    offers = MarketOffers([
        MarketOffer("chasse_aux_livres", price, None, price,
                    "https://www.chasse-aux-livres.fr/prix/275607933X/9782756079332",
                    item_condition="Très bon état +", price_includes_shipping=True,
                    is_historical=True, is_available=False, observed_at=now - timedelta(days=2)),
    ], historical_checked=True, attempts=(CollectionAttempt("chasse_aux_livres", "history"),
                                          CollectionAttempt("chasse_aux_livres", "current")))
    return BookPricingResult.from_offers(offers, None, price)


def test_source_minimum_replaces_no_local_projection():
    cache = BookMarketCache(annual_low_cents=100, historical_low_1y_cents=200)
    assert reference_price(cache) == (None, False)
    result = source_result()
    _stamp_collection(cache, result)
    assert reference_price(cache) == (921, True)
    assert cache.historical_low_1y_cents == 921
    assert cache.historical_low_at == result.offers[0].observed_at
    assert cache.source_history["days"] == 365
    assert cache.source_history["shipping_included"] is True


def test_partial_and_failed_collections_preserve_values_and_component_freshness():
    cache = BookMarketCache(current_offers=[{
        "provider_name": "retained", "delivered_price_cents": 1200,
    }])
    initial = source_result()
    _stamp_collection(cache, initial)
    history = dict(cache.source_history)
    historical_checked = cache.historical_checked_at
    current_checked = cache.last_successful_collection_at
    failure = BookPricingResult.from_offers(MarketOffers(failed_providers=("chasse_aux_livres",),
        attempts=(CollectionAttempt("chasse_aux_livres", "history", "failed"),
                  CollectionAttempt("chasse_aux_livres", "current", "skipped"))), None, None)
    _stamp_collection(cache, failure)
    assert cache.source_history == history
    assert cache.historical_checked_at == historical_checked
    assert cache.last_successful_collection_at == current_checked
    assert component_due(cache, "history", failure.collected_at) == (
        failure.collected_at + timedelta(hours=6)
    )
    assert component_due(cache, "current", failure.collected_at) == (
        initial.collected_at + timedelta(days=7)
    )
    assert not collection_due(cache, failure.collected_at + timedelta(hours=5))


def test_freshness_becomes_due_after_seven_days():
    cache = BookMarketCache()
    result = source_result()
    _stamp_collection(cache, result)
    assert not collection_due(cache, result.collected_at + timedelta(days=6, hours=23))
    assert collection_due(cache, result.collected_at + timedelta(days=7))


def test_verified_empty_history_is_dated_without_source_history():
    checked = datetime.now(UTC) - timedelta(days=2)
    offers = MarketOffers(
        [],
        historical_checked=True,
        attempts=(CollectionAttempt("chasse_aux_livres", "history"),),
    )
    result = BookPricingResult.from_offers(offers, None, None)
    cache = BookMarketCache(last_successful_collection_at=checked)

    _stamp_collection(cache, replace(result, collected_at=checked))

    assert result.historical_checked is True
    assert cache.historical_checked_at == checked
    assert cache.source_history is None
    assert cache.historical_low_1y_cents is None
    assert component_success_at(cache, "current") == checked
    assert component_success_at(cache, "history") == checked
    assert component_due(cache, "history", checked + timedelta(days=6)) == (
        checked + timedelta(days=7)
    )
    assert not collection_due(cache, checked + timedelta(days=6))
    assert collection_due(cache, checked + timedelta(days=7))


@pytest.mark.parametrize("title", [
    "Jean Doux et le mystère de la disquette molle", "L’Homme-chevreuil — Sept ans de vie sauvage",
    "Hoka Hey !", "Perpendiculaire au soleil", "Beneath the Trees Where Nobody Sees",
])
def test_creation_and_panel_use_the_same_source_minimum_without_diagnostics(title):
    cache = BookMarketCache()
    _stamp_collection(cache, source_result())
    book = Book(id=uuid.uuid4(), title=title, normalized_title=title, market_cache=cache)
    creation = MarketPriceService.get_comparison_summary(None, book, for_creation=True)
    panel = MarketPriceService.format_market_details_card(None, book)
    for text in (creation, panel):
        assert "Prix bas constaté :</b> 9,21 €" in text
        for forbidden in ("tentative", "collecte échouée", "Actualisation impossible",
                          "relevé(s)", "non vérifiable", "Dernière vérif."):
            assert forbidden not in text
    assert "Dernière MAJ :" in panel
    assert "Prochaine MAJ :" in panel


def test_explicit_second_volume_is_not_replaced_by_the_only_first_volume():
    first = Book(id=uuid.uuid4(), title="Absolute Martian Manhunter Vol. 1",
                 normalized_title="absolute martian manhunter 1", market_cache=None)
    card = MarketPriceService.format_market_details_card(
        None, None, [first], TomeMatchResult(tome_number=2),
    )
    assert "Tome 2" in card
    assert "Vol. 1" not in card


def test_series_lists_every_volume_and_never_uses_a_partial_total():
    cache = BookMarketCache(comparison_status=ComparisonStatus.UNAVAILABLE)
    _stamp_collection(cache, source_result())
    first = Book(id=uuid.uuid4(), title="Absolute Martian Manhunter Vol. 1",
                 normalized_title="absolute 1", market_cache=cache)
    second = Book(id=uuid.uuid4(), title="Absolute Martian Manhunter Vol. 2",
                  normalized_title="absolute 2", market_cache=None)
    card = MarketPriceService.format_market_details_card(None, first, [first, second])
    assert "Vol. 1" in card
    assert "Vol. 2" in card
    assert "Non renseigné" in card
    assert series_reference_total([first, second]) is None
    assert "Total de la série" not in card


def test_actual_absolute_titles_resolve_the_announced_second_volume():
    from app.db.repositories.book_repo import resolve_loaded_books

    books = [Book(title="Absolute Martian Manhunter Vol. 1", isbn13="9781799505204"),
             Book(title="Absolute Martian Manhunter, Vol. 2", isbn13="9781799510871")]
    selected, series, tome = resolve_loaded_books(
        alert_name="Absolute Martian Manhunter",
        listing_title="Absolute martian manhunter TP volume 2",
        listing_description=None, books=books,
    )
    assert selected is books[1]
    assert tome.tome_number == 2
    assert len(series) == 2


@pytest.mark.asyncio
async def test_overlapping_scheduled_passes_do_not_double_the_concurrency(monkeypatch):
    import asyncio

    from app.tasks import refresh_book_prices as refresh

    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(**kwargs):
        calls.append(kwargs)
        entered.set()
        await release.wait()
        return {"processed": 8}

    monkeypatch.setattr(refresh, "_refresh_book_prices_pass", run)
    first = asyncio.create_task(refresh.refresh_book_prices_task(job_id="full"))
    await entered.wait()
    second = await refresh.refresh_book_prices_task(job_id="incremental")
    assert second["processed"] == 0
    assert len(calls) == 1
    release.set()
    assert (await first)["processed"] == 8


def test_two_editions_of_the_same_volume_are_not_selected_arbitrarily():
    from app.db.repositories.book_repo import resolve_loaded_books

    books = [Book(title="Akira Tome 1", isbn13=code)
             for code in ("9782344012406", "9782723427371")]
    selected, series, tome = resolve_loaded_books(
        alert_name="Akira", listing_title="Akira Tome 1", listing_description=None, books=books,
    )
    assert selected is None
    assert len(series) == 2
    assert tome.tome_number == 1
    assert series_reference_total(books) is None


@pytest.mark.asyncio
async def test_history_retry_does_not_collect_fresh_current_offers():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.providers.base import requested_components
    from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
    from app.providers.registry import ProviderRegistry
    from app.services.book_market_pricing import fetch_offers_for_book

    scopes = []

    class Source(ChasseAuxLivresProvider):
        async def get_offers(self, **_kwargs):
            scopes.append(requested_components.get())
            return MarketOffers(historical_checked=True, attempts=(
                CollectionAttempt(self.name, "current", "skipped"),
                CollectionAttempt(self.name, "history"),
            ))

    direct = SimpleNamespace(name="direct", get_offers=AsyncMock(return_value=[]))
    book = Book(title="Hoka Hey !", isbn13="9782810202294", market_cache=BookMarketCache(
        last_successful_collection_at=datetime.now(UTC), collection_status="complete",
    ))
    await fetch_offers_for_book(book, registry=ProviderRegistry(providers=[Source(), direct]))
    assert scopes == [frozenset({"history"})]
    direct.get_offers.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_retry_preserves_the_history_obtained_in_the_first_attempt():
    from app.providers.base import requested_components
    from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
    from app.providers.registry import ProviderRegistry
    from app.services.book_market_pricing import fetch_offers_for_book

    scopes = []
    point = source_result().offers[0]
    current = MarketOffer("chasse_aux_livres", 1000, 200, 1200, "https://example.com/offer",
                          item_condition="Très bon", price_includes_shipping=True)

    class Source(ChasseAuxLivresProvider):
        async def get_offers(self, **_kwargs):
            scopes.append(requested_components.get())
            if len(scopes) == 1:
                return MarketOffers([point], historical_checked=True,
                    failed_providers=(self.name,), attempts=(
                        CollectionAttempt(self.name, "current", "failed", "timeout"),
                        CollectionAttempt(self.name, "history"),
                    ))
            return MarketOffers([current], attempts=(
                CollectionAttempt(self.name, "current"),
                CollectionAttempt(self.name, "history", "skipped"),
            ))

    book = Book(title="Jean Doux", isbn13="9782756079332", market_cache=None)
    offers = await fetch_offers_for_book(book, registry=ProviderRegistry(providers=[Source()]),
                                         retry_transient=True)
    assert scopes == [frozenset({"current", "history"}), frozenset({"current"})]
    assert point in offers
    assert current in offers
    assert offers.historical_checked
    assert offers.failed_providers == ()
    assert [a.status for a in offers.attempts if a.operation == "history"] == ["success"]


def test_confirmed_healthy_offer_wins_over_a_retained_failed_merchant():
    from dataclasses import asdict

    retained = MarketOffer("failed", 800, 100, 900, "https://example.com/retained",
                           item_condition="Très bon")
    newer = MarketOffer("healthy", 1000, 100, 1100, "https://example.com/new",
                        item_condition="Très bon")
    old_date = datetime.now(UTC)-timedelta(days=8)
    cache = BookMarketCache(lowest_delivered_price_cents=1100,
                            best_provider_name="healthy",
                            best_source_url=newer.source_url,
                            last_successful_collection_at=old_date,
                            current_offers=[{k: v for k, v in asdict(retained).items()
                                             if k not in {"observed_at", "observation_id"}}])
    result = BookPricingResult.from_offers(MarketOffers([newer], failed_providers=("failed",),
        attempts=(CollectionAttempt("failed", "current", "failed"),
                  CollectionAttempt("healthy", "current"))), newer, None)
    _stamp_collection(cache, result)
    assert cache.lowest_delivered_price_cents == 1100
    assert cache.best_provider_name == "healthy"
    assert cache.best_source_url == newer.source_url
    assert [offer["provider_name"] for offer in cache.current_offers] == ["healthy"]
    assert cache.last_successful_collection_at == old_date

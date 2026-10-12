"""Tests unitaires du point de cotation unique des livres.

Ces tests verrouillent les deux défauts qui faisaient partir une alerte sans prix :

- la recherche par ISBN ne retombait sur le titre que si on le fournissait — et la
  création d'alerte ne le fournissait pas, donc une recherche ISBN vide n'écrivait
  aucune cote ;
- le plus bas constaté sur 1 an n'était lu que sur la meilleure offre, ce qui le
  faisait disparaître dès qu'un autre marchand remontait moins cher.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import MarketOffer
from app.services import book_market_pricing as pricing
from app.services.book_market_pricing import (
    BookPricingResult,
    fetch_offers_for_book,
    persist_market_result,
    price_book,
    price_book_now,
    schedule_book_pricing,
)

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
            AsyncMock(), _book(), registry=_registry([cheapest, aggregator])
        )

    kwargs = market_repo.update_or_create_cache.call_args.kwargs
    assert kwargs["lowest_delivered_price_cents"] == 1000
    assert kwargs["comparison_status"] == ComparisonStatus.AVAILABLE
    assert kwargs["historical_low_1y_cents"] == 800
    assert cache is written


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

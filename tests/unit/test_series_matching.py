"""Tests unitaires pour la comparaison de prix sur séries multi-tomes et lots."""

from datetime import UTC, datetime, timedelta
import pytest

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.listing import Listing
from app.services.market_price_service import MarketPriceService
from app.services.tome_extractor import TomeMatchResult


def _create_mock_book(title: str, price_cents: int) -> Book:
    """Helper pour créer un livre factice avec un cache de prix actif."""
    book = Book(
        title=title,
        normalized_title=title.lower(),
        language="fr",
    )
    cache = BookMarketCache(
        lowest_delivered_price_cents=price_cents,
        best_provider_name="chasse_aux_livres",
        best_source_url="https://example.com/chasse",
        comparison_status=ComparisonStatus.AVAILABLE,
        confidence=MatchConfidence.HIGH,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    book.market_cache = cache
    return book


def test_series_matching_specific_tome_summary():
    """Vérifie le libellé pour un tome unitaire détecté."""
    book_t2 = _create_mock_book("RIP — Tome 2 (Maurice)", 1850)
    listing = Listing(
        source_name="vinted",
        external_id="123",
        url="https://vinted.fr",
        title="BD RIP Tome 2 Maurice",
        normalized_title="bd rip tome 2 maurice",
        category_slug="bd_comics",
        item_price_cents=1000,
        estimated_total_cents=1200,
    )
    tome_res = TomeMatchResult(is_lot=False, tome_number=2, confidence="high")

    summary = MarketPriceService.get_comparison_summary(
        listing=listing,
        book=book_t2,
        tome_result=tome_res,
    )
    assert summary is not None
    assert "📊 Prix du marché (T2) : 18,50 €" in summary
    assert "-6,50 €" in summary


def test_series_matching_lot_summary():
    """Vérifie le calcul et le libellé pour un lot de plusieurs tomes."""
    books = [
        _create_mock_book("RIP — Tome 1", 1600),
        _create_mock_book("RIP — Tome 2", 1850),
        _create_mock_book("RIP — Tome 3", 2000),
    ]
    # Total marché = 16.00 + 18.50 + 20.00 = 54.50 €
    listing = Listing(
        source_name="vinted",
        external_id="456",
        url="https://vinted.fr",
        title="Lot 3 BD RIP tomes 1 à 3",
        normalized_title="lot 3 bd rip tomes 1 a 3",
        category_slug="bd_comics",
        item_price_cents=2200,
        estimated_total_cents=2500,
    )
    tome_res = TomeMatchResult(is_lot=True, lot_tomes=[1, 2, 3], lot_count=3, confidence="high")

    summary = MarketPriceService.get_comparison_summary(
        listing=listing,
        book=None,
        books_in_series=books,
        tome_result=tome_res,
    )
    assert summary is not None
    assert "📦 Lot (3 tomes) : ~54,50 €" in summary
    # Gain : 25.00 - 54.50 = -29.50 €
    assert "-29,50 €" in summary


def test_series_matching_generic_range_summary():
    """Vérifie la fourchette de prix quand aucun tome n'est spécifié dans l'annonce."""
    books = [
        _create_mock_book("RIP — Tome 1", 1500),
        _create_mock_book("RIP — Tome 2", 1850),
        _create_mock_book("RIP — Tome 3", 2200),
    ]
    listing = Listing(
        source_name="vinted",
        external_id="789",
        url="https://vinted.fr",
        title="BD RIP très bon état",
        normalized_title="bd rip tres bon etat",
        category_slug="bd_comics",
        item_price_cents=1200,
        estimated_total_cents=1450,
    )
    tome_res = TomeMatchResult(is_lot=False, tome_number=None, confidence="none")

    summary = MarketPriceService.get_comparison_summary(
        listing=listing,
        book=None,
        books_in_series=books,
        tome_result=tome_res,
    )
    assert summary is not None
    assert "📊 Prix du marché : 15,00 € - 22,00 € (selon tome)" in summary
    assert "✅ Écart : <b>-0,50 € à -7,50 €</b>" in summary


def test_series_matching_lot_card():
    """Vérifie la génération du panneau détaillé pour un lot."""
    books = [
        _create_mock_book("RIP — Tome 1", 1600),
        _create_mock_book("RIP — Tome 2", 1850),
    ]
    listing = Listing(
        source_name="vinted",
        external_id="999",
        url="https://vinted.fr",
        title="Lot RIP T1 et T2",
        normalized_title="lot rip t1 et t2",
        category_slug="bd_comics",
        item_price_cents=2000,
        estimated_total_cents=2300,
    )
    tome_res = TomeMatchResult(is_lot=True, lot_tomes=[1, 2], lot_count=2, confidence="high")

    card = MarketPriceService.format_market_details_card(
        listing=listing,
        book=None,
        books_in_series=books,
        tome_result=tome_res,
    )
    assert "📊 <b>Prix du marché</b>" in card
    assert "💵 Lot (2 tomes) : ~34,50 €" in card
    assert "Cote par tome (Min sur 1 an) :" in card
    assert "• Tome 1 : 16,00 €" in card
    assert "• Tome 2 : 18,50 €" in card
    assert "Derniere MAJ :" in card

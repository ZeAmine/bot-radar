"""Tests unitaires pour le comparateur de marché d'occasion."""

import uuid
from datetime import UTC, datetime, timedelta

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.listing import Listing
from app.services.market_price_service import MarketPriceService


def create_book_with_cache(
    *,
    lowest_delivered_cents: int | None = 2200,
    status: ComparisonStatus = ComparisonStatus.AVAILABLE,
    expired: bool = False,
    historical_low_1y_cents: int | None = None,
) -> Book:
    book = Book(
        id=uuid.uuid4(),
        title="Blacksad — Intégrale",
        normalized_title="blacksad integrale",
        isbn13="9782205078054",
    )
    now = datetime.now(UTC)
    expires_at = now - timedelta(hours=1) if expired else now + timedelta(hours=24)

    cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book.id,
        lowest_delivered_price_cents=lowest_delivered_cents,
        best_provider_name="ebay",
        best_source_url="https://www.ebay.fr/itm/123",
        confidence=MatchConfidence.HIGH,
        checked_at=now,
        expires_at=expires_at,
        comparison_status=status,
        historical_low_1y_cents=historical_low_1y_cents,
        historical_low_at=now if historical_low_1y_cents is not None else None,
    )
    book.market_cache = cache
    return book


def create_listing(estimated_total_cents: int | None) -> Listing:
    return Listing(
        id=uuid.uuid4(),
        source_name="test",
        external_id="123",
        url="https://www.example.com",
        title="Blacksad — Intégrale",
        normalized_title="blacksad integrale",
        category_slug="bd_comics",
        item_price_cents=900,
        estimated_total_cents=estimated_total_cents,
        raw_payload={},
    )


def test_comparator_vinted_cheaper_than_market():
    # Vinted total : 13,69 € (1369 cts), Marché : 22,00 € (2200 cts) -> Vinted -8,31 €
    book = create_book_with_cache(lowest_delivered_cents=2200)
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "📉 Occasion : 22,00 €" in summary
    assert "✅ Écart : -8,31 €" in summary


def test_comparator_vinted_very_good_deal_fire_badge():
    # Économie importante (>= 10 €) -> Badge ✅
    book = create_book_with_cache(lowest_delivered_cents=3000)
    listing = create_listing(estimated_total_cents=1500)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "📉 Occasion : 30,00 €" in summary
    assert "✅ Écart : -15,00 €" in summary


def test_comparator_vinted_more_expensive_than_market():
    # Vinted total : 25,00 € (2500 cts), Marché : 20,00 € (2000 cts) -> Vinted +5,00 €
    book = create_book_with_cache(lowest_delivered_cents=2000)
    listing = create_listing(estimated_total_cents=2500)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "📉 Occasion : 20,00 €" in summary
    assert "❌ Écart : +5,00 €" in summary


def test_comparator_small_difference_warning():
    # Écart très faible (50 centimes) -> Vinted plus cher donc ❌
    book = create_book_with_cache(lowest_delivered_cents=2000)
    listing = create_listing(estimated_total_cents=2050)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "❌ Écart : +0,50 €" in summary


def test_comparator_market_unavailable():
    book = create_book_with_cache(status=ComparisonStatus.UNAVAILABLE)
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary == "📉 Occasion : indisponible"


def test_comparator_market_cache_expired():
    book = create_book_with_cache(expired=True)
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary == "📉 Occasion : indisponible"


def test_comparator_no_book_or_cache():
    listing = create_listing(estimated_total_cents=1369)
    summary = MarketPriceService.get_comparison_summary(listing, book=None)
    assert summary == "📉 Occasion : indisponible"


def test_comparator_with_historical_low_1y():
    book = create_book_with_cache(lowest_delivered_cents=2200, historical_low_1y_cents=1850)
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "📉 Occasion : 18,50 €" in summary
    assert "✅ Écart : -4,81 €" in summary

    card = MarketPriceService.format_market_details_card(listing, book)
    assert "💵 Offres : 18,50 €" in card
    assert "✅ Écart : -4,81 €" in card
    assert "🆕 Vinted : 13,69 €" in card
    assert "Derniere MAJ :" in card


def test_comparator_historical_low_when_status_unavailable():
    # Même si l'offre en direct est indisponible aujourd'hui, le plus bas historique 1 an sert de référence
    book = create_book_with_cache(
        status=ComparisonStatus.UNAVAILABLE,
        lowest_delivered_cents=None,
        historical_low_1y_cents=1850,
    )
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "📉 Occasion : 18,50 €" in summary
    assert "✅ Écart : -4,81 €" in summary


def test_comparator_excludes_brand_new_and_inferior_conditions():
    """Vérifie l'exclusion des offres Neuf, Bon, Correct et le plafonnement aux 5 meilleures offres."""
    book = create_book_with_cache(lowest_delivered_cents=869)
    # Injecter des partenaires mélangés (Neuf, Bon, Correct, Très bon, Comme neuf)
    book.market_cache.raw_payload = {
        "partners": {
            "Rakuten": {"merchant": "Rakuten", "is_used": True, "condition": "Comme neuf", "total_cents": 869},
            "Fnac": {"merchant": "Fnac", "is_used": True, "condition": "Très bon", "total_cents": 899},
            "Momox": {"merchant": "Momox", "is_used": True, "condition": "Bon", "total_cents": 750},  # Devrait être exclu car Bon
            "Abebooks": {"merchant": "Abebooks", "is_used": True, "condition": "Correct", "total_cents": 700},  # Devrait être exclu car Correct
            "Recyclivre": {"merchant": "Recyclivre", "is_used": True, "condition": "État moyen", "total_cents": 600},  # Devrait être exclu car Moyen
            "Les Libraires": {"merchant": "Les Libraires", "is_used": False, "condition": "Neuf", "total_cents": 999},  # Exclu car Neuf
            "Bd Fugue": {"merchant": "Bd Fugue", "is_used": True, "condition": "Très bon état", "total_cents": 950},
            "Bal": {"merchant": "Bal", "is_used": True, "condition": "Tres bon", "total_cents": 980},
            "Amazon": {"merchant": "Amazon", "is_used": True, "condition": "Comme neuf sous blister", "total_cents": 1050},
            "Ebay": {"merchant": "Ebay", "is_used": True, "condition": "Très bon", "total_cents": 1200},  # 6e offre valide, devrait être tronquée
        }
    }
    listing = create_listing(estimated_total_cents=1209)

    card = MarketPriceService.format_market_details_card(listing, book)
    assert "📉 <b>Prix du marché</b>" in card
    assert "🆕 Vinted : 12,09 €" in card
    # Le lowest_delivered doit s'aligner sur la meilleure offre TRÈS BON/COMME NEUF (Rakuten 8,69 € et non Momox 7,50 € Bon)
    assert "💵 Offres : 8,69 €" in card
    assert "❌ Écart : +3,40 €" in card

    assert "<b>Comparatif par boutique :</b>" in card
    assert "• Rakuten : 8,69 € (Comme neuf)" in card
    assert "• Fnac : 8,99 € (Très bon)" in card
    assert "• Bd Fugue : 9,50 € (Très bon)" in card
    assert "• Bal : 9,80 € (Très bon)" in card
    assert "• Amazon : 10,50 € (Comme neuf)" in card

    # Vérifier l'exclusion absolue des états inférieurs et du Neuf
    assert "Momox" not in card
    assert "(Bon)" not in card
    assert "Abebooks" not in card
    assert "(Correct)" not in card
    assert "Recyclivre" not in card
    assert "Les Libraires" not in card
    assert "(Neuf)" not in card

    # Vérifier la limite stricte de 5 offres (Ebay qui est la 6e offre valide ne doit pas être affichée)
    assert "Ebay" not in card

    assert "Derniere MAJ :" in card



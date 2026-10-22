import re
from datetime import UTC, datetime, timedelta

from app.services.market_price_service import MarketPriceService
from tests.unit.test_market_comparator import create_book_with_cache


def test_market_panel_keeps_single_price_and_link_without_merchant_list():
    book = create_book_with_cache()
    book.market_cache.raw_payload = {
        "partners": {
            "Boutique": {
                "merchant": "Boutique",
                "is_used": True,
                "condition": "Très bon",
                "total_cents": 2200,
                "url": "https://example.com/book",
            }
        }
    }
    card = MarketPriceService.format_market_details_card(None, book)
    assert "<b>Dernière offre connue :</b> 22,00 €" in card
    assert "Dernières offres connues" not in card
    assert "Consulter l'offre" in card


def test_market_footer_only_displays_real_dates_without_component_labels():
    book = create_book_with_cache()
    now = datetime.now(UTC)
    book.market_cache.last_successful_collection_at = now - timedelta(days=2)
    book.market_cache.source_history = {"collected_at": (now - timedelta(days=1)).isoformat()}
    book.market_cache.next_refresh_at = now + timedelta(hours=1)
    footer = MarketPriceService._verification_footer(book, None)
    assert len(footer.splitlines()) == 2
    pattern = r"(Dernière|Prochaine) MAJ : \d{2}/\d{2}(?:/\d{4})? à \d{2}:\d{2}"
    assert all(re.fullmatch(pattern, line) for line in footer.splitlines())


def test_missing_dates_are_not_replaced_by_attempts_or_an_unrelated_job():
    book = create_book_with_cache()
    book.market_cache.last_successful_collection_at = None
    book.market_cache.source_history = None
    book.market_cache.last_attempt_at = datetime.now(UTC)
    book.market_cache.next_refresh_at = None
    footer = MarketPriceService._verification_footer(book, datetime.now(UTC) + timedelta(hours=1))
    assert footer == ""


def test_market_panel_without_prices_explains_absence_without_empty_price_fields():
    book = create_book_with_cache(lowest_delivered_cents=None, annual_low_cents=None)
    card = MarketPriceService.format_market_details_card(None, book)
    assert "Non renseigné" not in card
    assert "<b>Dernière offre connue :</b>" not in card
    assert "<b>Prix bas constaté :</b>" not in card
    assert "cote" in card.lower()


def test_invalid_history_date_keeps_the_real_current_date():
    book = create_book_with_cache()
    book.market_cache.last_successful_collection_at = datetime.now(UTC)
    book.market_cache.source_history = {"collected_at": "not-a-date"}
    footer = MarketPriceService._verification_footer(book, None)
    assert footer.startswith("Dernière MAJ : ")
    assert "offres" not in footer


def test_past_reservation_is_not_presented_as_a_future_update():
    book = create_book_with_cache()
    book.market_cache.next_refresh_at = datetime.now(UTC) - timedelta(hours=1)
    assert "Prochaine MAJ" not in MarketPriceService._verification_footer(book, None)

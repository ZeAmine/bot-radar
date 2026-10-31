"""Tests unitaires pour le comparateur de marché d'occasion."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.listing import Listing
from app.services.market_price_service import PENDING_COTE_LINE, MarketPriceService
from app.services.tome_extractor import TomeMatchResult
from tests.fakes_market_source import attach_source_history, source_history_values


def test_market_card_dates_the_last_successful_check_at_the_bottom():
    book = create_book_with_cache()
    successful = datetime(datetime.now(UTC).year, 10, 7, 15, 4, tzinfo=UTC)
    book.market_cache.collection_status = "partial"
    book.market_cache.last_successful_collection_at = successful
    book.market_cache.source_history["collected_at"] = successful.isoformat()
    book.market_cache.last_attempt_at = successful + timedelta(hours=1)
    book.market_cache.annual_low_observed_at = successful - timedelta(days=1)
    book.market_cache.annual_first_at = book.market_cache.annual_low_observed_at
    book.market_cache.annual_last_at = book.market_cache.annual_low_observed_at
    book.market_cache.failed_provider_names = ["secret_source"]
    card = MarketPriceService.format_market_details_card(None, book)
    assert "Actualisation" not in card
    assert "📚 <b>Blacksad" in card
    assert "<b>Prix bas :</b> 22,00 €" in card
    assert "(06/10" not in card
    assert "relevé(s) conservé(s)" not in card
    # Contrat : une cote plus vieille que son rythme + 48 h ne s'affiche plus comme un
    # prix mais comme son âge daté — le montant retenu n'est donc plus écrit en clair.
    assert "<b>Dernière offre connue :</b> non vérifié depuis" in card
    assert "(relevé du 7 octobre" in card
    assert "<b>Dernière offre connue :</b> 22,00 €" not in card
    assert "Dernière MAJ : 07/10 à 17:04" in card
    assert card.count("07/10 à 17:04") == 1
    assert "Dernière tentative" not in card
    assert "Dernière mise à jour" not in card
    assert "secret_source" not in card


def test_failed_refresh_keeps_the_last_successful_check_and_retained_prices():
    book = create_book_with_cache()
    successful = datetime(datetime.now(UTC).year, 10, 7, 15, 4, tzinfo=UTC)
    book.market_cache.collection_status = "failed"
    book.market_cache.last_successful_collection_at = successful
    book.market_cache.source_history["collected_at"] = successful.isoformat()
    book.market_cache.last_attempt_at = successful + timedelta(days=1)
    card = MarketPriceService.format_market_details_card(None, book)
    assert "Actualisation" not in card
    assert "Dernière MAJ : 07/10 à 17:04" in card
    assert "08/10 à 17:04" not in card
    # Contrat : le prix retenu n'est jamais effacé ; périmé, il dit son âge au lieu
    # de son montant, et c'est la dernière offre connue qui reste nommée.
    assert "<b>Dernière offre connue :</b> non vérifié depuis" in card
    assert "<b>Dernière offre connue :</b> 22,00 €" not in card
    assert "Meilleure offre actuelle" not in card


def test_a_failed_collection_without_any_price_is_no_longer_in_progress():
    from app.services.market_health import classify_sent_cote_state
    from app.services.market_price_service import cote_absence_line

    book = create_book_with_cache(lowest_delivered_cents=None, annual_low_cents=None)
    book.market_cache.checked_at = None
    book.market_cache.last_attempt_at = datetime(2026, 10, 7, 15, 4, tzinfo=UTC)
    book.market_cache.collection_status = "failed"
    line = cote_absence_line(book)
    assert "Cote indisponible" in line
    assert "en cours" not in line
    assert classify_sent_cote_state(line) == "unavailable"
    card = MarketPriceService.format_market_details_card(None, book)
    assert "Actualisation" not in card
    assert "07/10 à 17:04" not in card
    assert "en cours" not in card
    assert "Dernière MAJ :" not in card


def create_book_with_cache(
    *,
    lowest_delivered_cents: int | None = 2200,
    status: ComparisonStatus = ComparisonStatus.AVAILABLE,
    expired: bool = False,
    annual_low_cents: int | None = 2200,
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
        annual_low_cents=annual_low_cents,
        annual_low_observed_at=now,
        annual_low_snapshot_id=uuid.uuid4(),
        annual_low_provider="synthetic",
        annual_first_at=now,
        annual_last_at=now,
        annual_count=1,
        annual_computed_at=now,
        annual_expires_at=now + timedelta(days=365),
    )
    attach_source_history(cache, annual_low_cents, now)
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
    book = create_book_with_cache(lowest_delivered_cents=2200, annual_low_cents=2200)
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "<b>Prix bas :</b> 22,00 €" in summary
    assert "Prix du marché" not in summary
    assert "<b>Écart estimé :</b> −8,31 €" in summary


def test_comparator_vinted_very_good_deal_fire_badge():
    book = create_book_with_cache(lowest_delivered_cents=3000, annual_low_cents=3000)
    listing = create_listing(estimated_total_cents=1500)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "<b>Prix bas :</b> 30,00 €" in summary
    assert "Prix du marché" not in summary
    assert "<b>Écart estimé :</b> −15,00 €" in summary


def test_comparator_vinted_more_expensive_than_market():
    book = create_book_with_cache(lowest_delivered_cents=2000, annual_low_cents=2000)
    listing = create_listing(estimated_total_cents=2500)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "<b>Prix bas :</b> 20,00 €" in summary
    assert "Prix du marché" not in summary
    assert "<b>Écart estimé :</b> +5,00 €" in summary


def test_comparator_small_difference_warning():
    book = create_book_with_cache(lowest_delivered_cents=2000, annual_low_cents=2000)
    listing = create_listing(estimated_total_cents=2050)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "<b>Prix bas :</b> 20,00 €" in summary
    assert "Prix du marché" not in summary
    assert "<b>Écart estimé :</b> +0,50 €" in summary


def test_gap_amount_is_signed_without_a_suffix_or_a_sign_at_zero():
    """Le montant signé suffit à exprimer l'écart, sans suffixe."""
    above = create_book_with_cache(lowest_delivered_cents=2000, annual_low_cents=2000)
    below = create_book_with_cache(lowest_delivered_cents=2200, annual_low_cents=2200)
    equal = create_book_with_cache(lowest_delivered_cents=2000, annual_low_cents=2000)

    summary_above = MarketPriceService.get_comparison_summary(
        create_listing(estimated_total_cents=2500), above
    )
    summary_below = MarketPriceService.get_comparison_summary(
        create_listing(estimated_total_cents=1800), below
    )
    summary_equal = MarketPriceService.get_comparison_summary(
        create_listing(estimated_total_cents=2000), equal
    )
    assert summary_above is not None
    assert summary_below is not None
    assert summary_equal is not None

    above_line = next(line for line in summary_above.splitlines() if "Écart estimé :" in line)
    below_line = next(line for line in summary_below.splitlines() if "Écart estimé :" in line)
    equal_line = next(line for line in summary_equal.splitlines() if "Écart estimé :" in line)

    assert above_line == "<b>Écart estimé :</b> +5,00 €"
    assert below_line == "<b>Écart estimé :</b> −4,00 €"
    assert not above_line.startswith(("✅", "⛔️"))
    assert not below_line.startswith(("✅", "⛔️"))

    assert equal_line == "<b>Écart estimé :</b> 0,00 €"
    assert "✅" not in equal_line
    assert "⛔️" not in equal_line


def test_comparator_market_unavailable():
    book = create_book_with_cache(status=ComparisonStatus.UNAVAILABLE, annual_low_cents=None)
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is None


def test_comparator_market_cache_expired():
    book = create_book_with_cache(expired=True, annual_low_cents=None)
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is None


def test_comparator_no_book_or_cache():
    listing = create_listing(estimated_total_cents=1369)
    summary = MarketPriceService.get_comparison_summary(listing, book=None)
    assert summary is None


def test_comparator_with_historical_low_1y():
    book = create_book_with_cache(lowest_delivered_cents=2200, annual_low_cents=1850)
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "Prix du marché" not in summary
    assert "<b>Prix bas :</b> 18,50 €" in summary
    assert "<b>Écart estimé :</b> −4,81 €" in summary

    card = MarketPriceService.format_market_details_card(listing, book)
    assert "🆕 Vinted" not in card
    assert "💵 Offres" not in card
    assert "Écart" not in card
    assert "Dernière MAJ :" in card


def test_comparator_no_historical_low_falls_back_to_live_market_price():
    """Vérifie que sans historique 1 an, le résumé se replie sur le prix du marché direct."""
    book = create_book_with_cache(lowest_delivered_cents=2200, annual_low_cents=None)
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "<b>Dernière offre connue :</b> 22,00 € (Ebay)" in summary
    assert "<b>Écart estimé :</b> −8,31 €" in summary


def test_comparator_market_details_card_excludes_amazon_de():
    """Vérifie que Amazon (De) est exclu de la carte de détails même en raw_payload."""
    book = create_book_with_cache(lowest_delivered_cents=2000)
    book.market_cache.raw_payload = {
        "partners": {
            "Amazon (De)": {
                "merchant": "Amazon (De)",
                "is_used": True,
                "condition": "Très bon",
                "total_cents": 1500,
            },
            "Rakuten": {
                "merchant": "Rakuten",
                "is_used": True,
                "condition": "Très bon",
                "total_cents": 2000,
            },
        }
    }
    listing = create_listing(estimated_total_cents=1800)

    card = MarketPriceService.format_market_details_card(listing, book)
    assert "Amazon (De)" not in card
    assert "<b>Dernière offre connue :</b> 20,00 €" in card
    from app.services.market_price_service import _extract_partners, _select_secondhand_partners

    partners = _select_secondhand_partners(_extract_partners(book.market_cache, None, None))
    assert set(partners) == {"Rakuten"}
    assert "🆕 Vinted" not in card
    assert "💵 Offres" not in card
    assert "Écart" not in card


def test_comparator_historical_low_when_status_unavailable():
    book = create_book_with_cache(
        status=ComparisonStatus.UNAVAILABLE,
        lowest_delivered_cents=None,
        annual_low_cents=1850,
    )
    listing = create_listing(estimated_total_cents=1369)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    assert "Prix du marché" not in summary
    assert "<b>Prix bas :</b> 18,50 €" in summary
    assert "<b>Écart estimé :</b> −4,81 €" in summary

    card = MarketPriceService.format_market_details_card(listing, book)
    assert "📊 <b>Prix du marché</b>" in card
    assert "<b>Prix bas :</b> 18,50 €" in card
    assert "🆕 Vinted" not in card
    assert "💵 Offres" not in card
    assert "Écart" not in card
    assert "Cote d'occasion basée sur le plus bas prix" not in card
    assert "nous avons pu constater" not in card


def test_comparator_single_book_series_fallback():
    book = create_book_with_cache(
        lowest_delivered_cents=None,
        annual_low_cents=1548,
    )
    listing = create_listing(estimated_total_cents=1200)

    card = MarketPriceService.format_market_details_card(
        listing=listing,
        book=None,
        books_in_series=[book],
    )
    assert "📊 <b>Prix du marché</b>" in card
    assert "<b>Prix bas :</b> 15,48 €" in card


def test_comparator_no_price_at_all_returns_unavailable():
    book = create_book_with_cache(
        lowest_delivered_cents=None,
        annual_low_cents=None,
    )
    listing = create_listing(estimated_total_cents=1200)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is None


def test_comparator_excludes_brand_new_and_inferior_conditions():
    """Vérifie l'exclusion des offres Neuf/inférieures et le plafonnement à 5 offres."""
    book = create_book_with_cache(lowest_delivered_cents=869)
    book.market_cache.raw_payload = {
        "partners": {
            "Rakuten": {
                "merchant": "Rakuten",
                "is_used": True,
                "condition": "Comme neuf",
                "total_cents": 869,
            },
            "Fnac": {
                "merchant": "Fnac",
                "is_used": True,
                "condition": "Très bon",
                "total_cents": 899,
            },
            "Momox": {"merchant": "Momox", "is_used": True, "condition": "Bon", "total_cents": 750},
            "Abebooks": {
                "merchant": "Abebooks",
                "is_used": True,
                "condition": "Correct",
                "total_cents": 700,
            },
            "Recyclivre": {
                "merchant": "Recyclivre",
                "is_used": True,
                "condition": "État moyen",
                "total_cents": 600,
            },
            "Les Libraires": {
                "merchant": "Les Libraires",
                "is_used": False,
                "condition": "Neuf",
                "total_cents": 999,
            },
            "Bd Fugue": {
                "merchant": "Bd Fugue",
                "is_used": True,
                "condition": "Très bon état",
                "total_cents": 950,
            },
            "Bal": {
                "merchant": "Bal",
                "is_used": True,
                "condition": "Tres bon",
                "total_cents": 980,
            },
            "Amazon": {
                "merchant": "Amazon",
                "is_used": True,
                "condition": "Comme neuf sous blister",
                "total_cents": 1050,
            },
            "Ebay": {
                "merchant": "Ebay",
                "is_used": True,
                "condition": "Très bon",
                "total_cents": 1200,
            },
        }
    }
    listing = create_listing(estimated_total_cents=1209)

    card = MarketPriceService.format_market_details_card(listing, book)
    assert "📊 <b>Prix du marché</b>" in card
    assert "🆕 Vinted" not in card
    assert "💵 Offres" not in card
    assert "Écart" not in card

    assert "<b>Dernière offre connue :</b> 8,69 €" in card
    assert "Dernières offres connues" not in card
    from app.services.market_price_service import _extract_partners, _select_secondhand_partners

    partners = _select_secondhand_partners(_extract_partners(book.market_cache, None, None))
    assert set(partners) == {"Rakuten", "Fnac", "Bd Fugue", "Bal", "Amazon", "Ebay"}
    assert all(merchant not in card for merchant in partners)


def test_comparator_market_details_card_shows_both_historical_low_and_snapshots_partners():
    """Vérifie l'affichage combiné de la cote 1 an et du comparatif par boutique."""
    from datetime import UTC, datetime

    from app.db.models.market_price_snapshot import MarketPriceSnapshot

    book = create_book_with_cache(
        lowest_delivered_cents=2104,
        annual_low_cents=548,
    )
    snap = MarketPriceSnapshot(
        book_id=book.id,
        provider_name="chasse_aux_livres",
        item_price_cents=2104,
        shipping_price_cents=0,
        delivered_price_cents=2104,
        source_url="https://example.com/indes",
        checked_at=datetime.now(UTC),
        raw_payload={
            "partners": {
                "Momox": {
                    "merchant": "Momox",
                    "is_used": True,
                    "condition": "Très bon",
                    "total_cents": 2104,
                    "url": "https://momox.fr/indes",
                },
                "Fnac": {
                    "merchant": "Fnac",
                    "is_used": True,
                    "condition": "Très bon",
                    "total_cents": 2715,
                },
            }
        },
    )
    book.snapshots = [snap]
    listing = create_listing(estimated_total_cents=1200)

    card = MarketPriceService.format_market_details_card(listing, book)

    assert "<b>Prix bas :</b> 5,48 €" in card
    assert "Dernières offres connues" not in card
    assert "<b>Dernière offre connue :</b> 21,04 €" in card
    assert '<a href="https://momox.fr/indes">Consulter l\'offre</a>' in card


def test_summary_and_card_share_the_same_reference_price():
    """Le résumé d'alerte et le panneau détaillé doivent citer la même référence.

    Régression : le panneau écrasait la cote 1 an par le prix du jour, si bien que
    les deux écrans affichaient deux références différentes pour le même livre.
    """
    book = create_book_with_cache(lowest_delivered_cents=2200, annual_low_cents=548)
    listing = create_listing(estimated_total_cents=1200)

    summary = MarketPriceService.get_comparison_summary(listing, book)
    card = MarketPriceService.format_market_details_card(listing, book)

    assert summary is not None
    assert "<b>Prix bas :</b> 5,48 €" in summary
    assert "<b>Prix bas :</b> 5,48 €" in card
    assert "<b>Dernière offre connue :</b> 22,00 €" in card


def test_summary_uses_vinted_total_including_shipping():
    """L'écart compare le total Vinted (port inclus), pas le prix de l'article seul."""
    book = create_book_with_cache(lowest_delivered_cents=2000, annual_low_cents=2000)
    listing = create_listing(estimated_total_cents=None)
    listing.item_price_cents = 1500

    summary = MarketPriceService.get_comparison_summary(listing, book)
    assert summary is not None
    # 1 500 + 75 (5 %) + 70 + 288 (port) = 1 933 cts : écart -0,67 € contre 2 000 cts.
    assert "<b>Écart estimé :</b> −0,67 €" in summary


def test_lot_summary_reports_partial_tome_matching():
    """Un lot dont un tome n'a pas de cote ne doit pas cumuler les tomes non appariées."""
    books = [
        create_book_with_cache(lowest_delivered_cents=1600, annual_low_cents=1600),
        create_book_with_cache(lowest_delivered_cents=1850, annual_low_cents=1850),
    ]
    books[0].title = "RIP — Tome 1"
    books[1].title = "RIP — Tome 2"
    tome3 = create_book_with_cache(lowest_delivered_cents=None, annual_low_cents=None)
    tome3.title = "RIP — Tome 3"
    books.append(tome3)

    listing = create_listing(estimated_total_cents=2500)
    tome_res = TomeMatchResult(is_lot=True, lot_tomes=[1, 2, 3], lot_count=3, confidence="high")

    summary = MarketPriceService.get_comparison_summary(
        listing=listing, book=None, books_in_series=books, tome_result=tome_res
    )
    assert summary is not None
    assert "Tome 1 : 16,00 €" in summary
    assert "Tome 2 : 18,50 €" in summary
    assert "Tome 3 : cote indisponible" in summary
    assert "34,50" not in summary
    assert "Écart" not in summary


def test_inferred_lot_label_says_the_composition_was_deduced():
    """Un lot composé par nous ne doit pas se lire comme un inventaire du vendeur."""
    books = [
        create_book_with_cache(lowest_delivered_cents=1600, annual_low_cents=1600),
        create_book_with_cache(lowest_delivered_cents=1850, annual_low_cents=1850),
    ]
    books[0].title = "RIP — Tome 1"
    books[1].title = "RIP — Tome 2"

    listing = create_listing(estimated_total_cents=2500)
    tome_res = TomeMatchResult(
        is_lot=True,
        lot_tomes=[1, 2],
        lot_count=2,
        confidence="high",
        composition_inferred=True,
    )

    summary = MarketPriceService.get_comparison_summary(
        listing=listing, book=None, books_in_series=books, tome_result=tome_res
    )
    assert summary is not None
    assert ("<b>Prix bas "
            "(Lot de 2 tomes, composition déduite) :</b> ~34,50 €") in summary


def test_inferred_lot_label_keeps_the_partial_coverage():
    """La couverture réelle reste annoncée, même quand la composition est déduite."""
    books = [
        create_book_with_cache(lowest_delivered_cents=1600, annual_low_cents=1600),
        create_book_with_cache(lowest_delivered_cents=None, annual_low_cents=None),
    ]
    books[0].title = "RIP — Tome 1"
    books[1].title = "RIP — Tome 2"

    listing = create_listing(estimated_total_cents=2500)
    tome_res = TomeMatchResult(
        is_lot=True,
        lot_tomes=[1, 2],
        lot_count=2,
        confidence="high",
        composition_inferred=True,
    )

    summary = MarketPriceService.get_comparison_summary(
        listing=listing, book=None, books_in_series=books, tome_result=tome_res
    )
    assert summary is not None
    assert "Tome 1 : 16,00 €" in summary
    assert "Tome 2 : cote indisponible" in summary
    assert "Écart" not in summary


def test_details_card_rejects_unsafe_offer_link():
    """Une URL non publique ou porteuse de HTML brut ne doit jamais être rendue cliquable."""
    book = create_book_with_cache(lowest_delivered_cents=2000, annual_low_cents=2000)
    book.market_cache.best_source_url = "javascript:alert(1)"
    listing = create_listing(estimated_total_cents=1200)

    card = MarketPriceService.format_market_details_card(listing, book)
    assert "Lien :" not in card


def test_details_card_escapes_offer_link():
    """L'URL de l'offre est échappée pour ne pas casser le HTML Telegram."""
    book = create_book_with_cache(lowest_delivered_cents=2000, annual_low_cents=2000)
    book.market_cache.best_source_url = "https://recyclivre.com/livre?a=1&b=2"
    listing = create_listing(estimated_total_cents=1200)

    card = MarketPriceService.format_market_details_card(listing, book)
    assert "https://recyclivre.com/livre?a=1&amp;b=2" in card
    assert "?a=1&b=2" not in card


def _tome_book(title: str, price_cents: int | None) -> Book:
    """Un tome de série avec (ou sans) cote en base."""
    book = Book(
        id=uuid.uuid4(),
        title=title,
        normalized_title=title.lower(),
        isbn13=None,
    )
    if price_cents is None:
        book.market_cache = None
        return book
    now = datetime.now(UTC)
    book.market_cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book.id,
        lowest_delivered_price_cents=price_cents,
        best_provider_name="test",
        best_source_url=None,
        confidence=MatchConfidence.HIGH,
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=now,
        expires_at=now + timedelta(hours=24),
        annual_low_cents=price_cents,
        **source_history_values(price_cents, now),
    )
    return book


def test_series_card_shows_every_volume_and_the_persisted_total():
    """E4 : la série liste chaque tome, signale les muets, et affiche le total persisté.

    Un tome sans cote ne disparaît plus : il compte dans la ligne « en cours de
    cote ». Le total affiché est celui de l'alerte, jamais recomposé ici.
    """
    books = [
        _tome_book("Blast — Tome 1", 500),
        _tome_book("Blast — Tome 2", 700),
        _tome_book("Blast — Tome 3", None),
    ]
    listing = create_listing(estimated_total_cents=1500)

    card = MarketPriceService.format_market_details_card(
        listing, None, books, None, None, series_total_cents=1200
    )

    assert "Tome 1 : 5,00 €" in card
    assert "Tome 2 : 7,00 €" in card
    assert "⏳ 1 tome(s) en cours de cote…" in card
    assert "Total de la série" not in card


def test_series_card_without_persisted_total_omits_the_total_line():
    """E4 : sans total en base, la ligne est absente — jamais un total recomposé."""
    books = [_tome_book("Blast — Tome 1", 500), _tome_book("Blast — Tome 2", 700)]
    listing = create_listing(estimated_total_cents=1500)

    card = MarketPriceService.format_market_details_card(listing, None, books)

    assert "Total de la série" not in card


def test_lot_card_shows_the_total_only_when_every_tome_is_priced():
    """E4 : le prix de lot n'existe que si tous ses tomes sont cotés.

    Un total partiel serait un cumul fictif : le vendeur vend le lot entier, pas
    la moitié que nous aurions su coter.
    """
    books = [_tome_book("Blast — Tome 1", 500), _tome_book("Blast — Tome 2", 700)]
    listing = create_listing(estimated_total_cents=1500)
    tome_result = TomeMatchResult(is_lot=True, lot_tomes=[1, 2], confidence="high")

    card = MarketPriceService.format_market_details_card(
        listing, None, books, tome_result, None
    )
    assert "<b>Total du lot :</b> ~12,00 €" in card

    # Un tome sans cote : pas de prix de lot, les cotes par tome restent.
    books[1].market_cache = None
    card = MarketPriceService.format_market_details_card(
        listing, None, books, tome_result, None
    )
    assert "Total du lot" not in card
    assert "Tome 1 : 5,00 €" in card


def test_lot_card_without_any_price_announces_the_pending_cotes():
    """E4 : un lot dont aucun tome n'est coté annonce l'attente, pas un panneau vide.

    L'attente est comptée par tome plutôt que résumée par la ligne générique : le
    lot dit combien de tomes il attend, sans cesser d'annoncer qu'il attend.
    """
    books = [_tome_book("Blast — Tome 1", None), _tome_book("Blast — Tome 2", None)]
    listing = create_listing(estimated_total_cents=1500)
    tome_result = TomeMatchResult(is_lot=True, lot_tomes=[1, 2], confidence="high")

    card = MarketPriceService.format_market_details_card(
        listing, None, books, tome_result, None
    )
    assert "⏳ 2 tome(s) en cours de cote…" in card
    assert "Total du lot" not in card


def _tome_book_with_settled_absence(title: str, *, checked_at: datetime | None = None) -> Book:
    """Un tome dont une passe de cotation a eu lieu, sans trouver la moindre offre."""
    book = _tome_book(title, None)
    book.market_cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book.id,
        lowest_delivered_price_cents=None,
        best_provider_name=None,
        best_source_url=None,
        confidence=None,
        comparison_status=ComparisonStatus.UNAVAILABLE,
        checked_at=datetime.now(UTC) if checked_at is None else checked_at,
        expires_at=None,
        annual_low_cents=None,
    )
    book.market_cache.collection_status = "empty"
    book.market_cache.last_successful_collection_at = book.market_cache.checked_at
    return book


def test_a_pass_without_offer_is_dated_and_no_longer_reads_as_pending():
    """Une passe qui a eu lieu a un résultat : l'absence d'offre, et sa date.

    « Cote d'occasion en cours de calcul… » restait affiché pour un livre que
    personne ne vend : l'attente était le seul mensonge disponible ici.
    """
    from app.services.market_price_service import NO_OFFER_LINE, cote_absence_line

    checked_at = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
    settled = _tome_book_with_settled_absence("Blast — Tome 3", checked_at=checked_at)

    # Aucun livre : rien à dire d'une cote qui n'a pas d'objet.
    assert cote_absence_line(None) is None
    # Jamais coté : l'attente est vraie, elle reste.
    assert cote_absence_line(_tome_book("Blast — Tome 2", None)) == PENDING_COTE_LINE

    line = cote_absence_line(settled)
    assert line is not None
    assert line.startswith(NO_OFFER_LINE)
    # La date est celle du relevé, lue à l'heure de Paris.
    assert "06/10" not in line

    # Un cache sans date de relevé n'atteste aucune passe : on ne conclut pas.
    never_checked = _tome_book_with_settled_absence("Blast — Tome 4")
    never_checked.market_cache.checked_at = None
    never_checked.market_cache.last_successful_collection_at = None
    never_checked.market_cache.collection_status = None
    assert cote_absence_line(never_checked) == PENDING_COTE_LINE


def test_series_card_counts_a_settled_absence_apart_from_the_pending_cotes():
    """Le panneau distingue ce qu'il attend de ce qu'il a déjà constaté.

    Les deux comptes portent sur les mêmes tomes sans prix, mais ne disent pas la
    même chose : l'un attend une cote, l'autre l'a déjà eue.
    """
    books = [
        _tome_book("Blast — Tome 1", 500),
        _tome_book("Blast — Tome 2", None),
        _tome_book_with_settled_absence("Blast — Tome 3"),
    ]
    listing = create_listing(estimated_total_cents=1500)

    card = MarketPriceService.format_market_details_card(
        listing, None, books, None, None, series_total_cents=1200
    )

    assert "⏳ 1 tome(s) en cours de cote…" in card
    assert "🔍 1 tome(s) sans aucune offre trouvée." in card
    # Une valeur persistée incohérente est masquée, jamais remplacée à la lecture.
    assert "Total de la série" not in card
    assert "~5,00" not in card


def test_the_panel_without_cote_says_which_silence_it_reports():
    """Le panneau date ce qu'il a constaté, et n'affirme rien qu'il n'a pas fait."""
    listing = create_listing(estimated_total_cents=1500)
    pending_book = _tome_book("Blast — Intégrale", None)
    settled = _tome_book_with_settled_absence("Blast — Intégrale")

    pending_card = MarketPriceService.format_market_details_card(listing, pending_book)
    settled_card = MarketPriceService.format_market_details_card(listing, settled)

    assert PENDING_COTE_LINE in pending_card
    assert "Aucune offre" not in pending_card
    assert "Aucune offre d'occasion trouvée" in settled_card
    assert "Dernière MAJ :" in settled_card
    assert PENDING_COTE_LINE not in settled_card


def test_the_panel_of_an_uncatalogued_series_says_it_and_lets_books_win():
    """Le panneau d'une œuvre que personne ne catalogue dit la réponse, pas l'attente.

    Le verdict vaut pour une alerte **sans livre** : dès qu'un livre existe, sa
    cote est plus récente que le verdict, et l'écran la montre. Un panneau qui
    préférerait le verdict à un livre afficherait une absence démentie par la base.
    """
    from app.services.market_price_service import NO_CATALOGUE_LINE

    listing = create_listing(estimated_total_cents=1500)

    card = MarketPriceService.format_market_details_card(
        listing, None, catalogue_exhausted=True
    )
    assert NO_CATALOGUE_LINE in card
    assert PENDING_COTE_LINE not in card

    # Sans le verdict, le même panneau annonce l'attente.
    waiting = MarketPriceService.format_market_details_card(listing, None)
    assert PENDING_COTE_LINE in waiting
    assert NO_CATALOGUE_LINE not in waiting

    # Un livre contredit le verdict : il est coté, donc catalogué.
    priced = MarketPriceService.format_market_details_card(
        listing, _tome_book("Blast — Tome 1", 500), catalogue_exhausted=True
    )
    assert NO_CATALOGUE_LINE not in priced
    assert "5,00 €" in priced


def test_single_book_card_carries_the_series_total():
    """E4 : le tome résolu affiche aussi le total de sa série — il en fait partie."""
    current = _tome_book("Blast — Tome 2", 700)
    books = [_tome_book("Blast — Tome 1", 500), current]
    listing = create_listing(estimated_total_cents=1500)

    card = MarketPriceService.format_market_details_card(
        listing, current, books, None, None, series_total_cents=1200
    )

    assert "Autres tomes de la série" in card
    assert "<b>Total de la série :</b> ~12,00 €" in card


def test_series_reference_total_ignores_an_integral_when_tomes_are_numbered():
    """E4 : l'intégrale ne double pas la série — le total somme ce qui est listé."""
    from app.services.market_price_service import series_reference_total

    books = [
        _tome_book("Blacksad — Intégrale", 900),
        _tome_book("Blacksad — Tome 1", 500),
        _tome_book("Blacksad — Tome 2", 700),
    ]
    assert series_reference_total(books) is None

    # Sans tome numéroté, tout compte : il n'y a rien à dédoublonner.
    unnumbered = [_tome_book("Blacksad — Intégrale", 900), _tome_book("Blacksad — Hors série", 300)]
    assert series_reference_total(unnumbered) is None

    # Aucune cote : pas de total — un zéro se lirait comme une série gratuite.
    assert series_reference_total([_tome_book("Blast — Tome 1", None)]) is None


def test_lot_card_keeps_a_failed_tome_without_a_refresh_warning():
    first = create_book_with_cache()
    second = create_book_with_cache()
    first.title, second.title = "Série Tome 1", "Série Tome 2"
    first.market_cache.collection_status = "failed"
    first.market_cache.last_attempt_at = datetime(2026, 10, 6, tzinfo=UTC)
    first.market_cache.checked_at = datetime(2026, 10, 6, tzinfo=UTC)
    second.market_cache.collection_status = "complete"
    second.market_cache.last_attempt_at = datetime(2026, 10, 7)
    card = MarketPriceService.format_market_details_card(
        None, None, [first, second], TomeMatchResult(is_lot=True, lot_tomes=[1, 2])
    )
    assert "Actualisation" not in card
    assert "07/10 à 02:00" not in card
    assert "• Tome 1 : 22,00 €" in card
    assert "• Tome 2 : 22,00 €" in card


def test_a_series_with_failed_unpriced_tomes_does_not_promise_pending_prices():
    book = create_book_with_cache(lowest_delivered_cents=None, annual_low_cents=None)
    book.title = "Série Tome 1"
    book.market_cache.collection_status = "failed"
    other = create_book_with_cache()
    other.title = "Série Tome 2"
    card = MarketPriceService.format_market_details_card(None, None, [book, other])
    assert "1 tome(s) sans cote disponible" in card
    assert "en cours de cote" not in card


def test_series_with_one_empty_tome_does_not_claim_the_whole_collection_is_empty():
    first = create_book_with_cache()
    first.title = "Série Tome 1"
    first.market_cache.collection_status = "complete"
    first.market_cache.last_attempt_at = datetime(2026, 10, 6, tzinfo=UTC)
    second = create_book_with_cache(lowest_delivered_cents=None, annual_low_cents=None)
    second.title = "Série Tome 2"
    second.market_cache.collection_status = "empty"
    second.market_cache.last_attempt_at = datetime(2026, 10, 7, tzinfo=UTC)
    second.market_cache.checked_at = datetime(2026, 10, 7, tzinfo=UTC)
    card = MarketPriceService.format_market_details_card(None, None, [first, second])
    assert "Actualisation" not in card
    assert "07/10 à 02:00" not in card
    assert "1 tome(s) sans aucune offre trouvée" in card
    assert "Tome 1 : 22,00 €" in card


def test_an_empty_current_offer_set_falls_back_to_the_last_known_offers():
    """Le lien conservé reste disponible sans réintroduire de liste de boutiques."""
    book = create_book_with_cache()
    book.market_cache.current_offers = []
    book.market_cache.collection_status = "failed"
    snapshot = MagicMock()
    snapshot.raw_payload = {
        "partners": {
            "Bd Fugue": {
                "merchant": "Bd Fugue",
                "is_used": True,
                "condition": "Comme neuf",
                "total_cents": 2499,
                "url": "https://bdfugue.example/offre",
            },
            "Rakuten": {
                "merchant": "Rakuten",
                "is_used": True,
                "condition": "Très bon",
                "total_cents": 2599,
            },
        }
    }

    card = MarketPriceService.format_market_details_card(
        None, book, latest_snapshot=snapshot
    )

    assert "Dernières offres connues" not in card
    assert "<b>Dernière offre connue :</b> 22,00 €" in card
    assert 'href="https://bdfugue.example/offre"' in card
    assert card.count("• ") == 0
    assert "Actualisation" not in card


def test_the_freshness_footer_closes_the_panel_after_the_offer_link():
    """La date de vérification vient après le lien, suivie de la passe confirmée."""
    from zoneinfo import ZoneInfo

    paris = ZoneInfo("Europe/Paris")
    year = datetime.now(UTC).year
    book = create_book_with_cache()
    book.market_cache.last_successful_collection_at = datetime(
        year, 10, 17, 7, 0, tzinfo=paris
    )
    book.market_cache.source_history["collected_at"] = (
        book.market_cache.last_successful_collection_at.isoformat()
    )
    next_check = datetime.now(UTC) + timedelta(hours=1)
    book.market_cache.next_refresh_at = next_check
    snapshot = MagicMock()
    snapshot.raw_payload = {
        "partners": {
            "Bd Fugue": {
                "merchant": "Bd Fugue",
                "is_used": True,
                "condition": "Comme neuf",
                "total_cents": 2499,
                "url": "https://bdfugue.example/offre",
            }
        }
    }

    card = MarketPriceService.format_market_details_card(
        None, book, latest_snapshot=snapshot, next_check_at=next_check
    )
    lines = card.splitlines()

    assert "Dernière MAJ : 17/10 à 07:00" in lines[-2]
    assert lines[-1].startswith("Prochaine MAJ :")
    assert card.index("Consulter l'offre") < card.index("Dernière MAJ")
    assert "Actualisation" not in card


def test_the_freshness_footer_distinguishes_never_from_unknown():
    """Deux absences différentes : aucune vérification réussie, ou date historique perdue."""
    never = Book(
        id=uuid.uuid4(),
        title="Jamais vérifié",
        normalized_title="jamais verifie",
    )
    never.market_cache = BookMarketCache(id=uuid.uuid4(), book_id=never.id)
    never_card = MarketPriceService.format_market_details_card(None, never)
    assert "Dernière MAJ :" not in never_card

    unknown = Book(
        id=uuid.uuid4(),
        title="Vérifié autrefois",
        normalized_title="verifie autrefois",
    )
    unknown.market_cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=unknown.id,
        checked_at=datetime(2026, 10, 6, 9, 30, tzinfo=UTC),
        historical_low_1y_cents=1200,
    )
    unknown_card = MarketPriceService.format_market_details_card(None, unknown)
    assert "Dernière MAJ :" not in unknown_card

    settled_without_date = Book(
        id=uuid.uuid4(),
        title="Collecte ancienne sans date",
        normalized_title="collecte ancienne sans date",
    )
    settled_without_date.market_cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=settled_without_date.id,
        collection_status="complete",
    )
    settled_card = MarketPriceService.format_market_details_card(None, settled_without_date)
    assert "Dernière MAJ :" not in settled_card


def test_the_freshness_footer_shows_the_year_when_it_is_ambiguous():
    """Une date d'une autre année porte son année ; celle de l'année courante non."""
    from zoneinfo import ZoneInfo

    paris = ZoneInfo("Europe/Paris")
    previous_year = datetime.now(UTC).year - 1
    book = create_book_with_cache()
    book.market_cache.last_successful_collection_at = datetime(
        previous_year, 12, 31, 7, 0, tzinfo=paris
    )

    book.market_cache.source_history["collected_at"] = (
        book.market_cache.last_successful_collection_at.isoformat()
    )
    card = MarketPriceService.format_market_details_card(None, book)

    assert f"Dernière MAJ : 31/12/{previous_year} à 07:00" in card
    assert "Prochaine MAJ :" not in card


def test_next_full_pass_ignores_incremental_jobs(monkeypatch):
    """Une tâche glissante ne garantit pas que ce livre sera traité : aucune ligne."""
    from types import SimpleNamespace

    from app.services import market_health
    from app.tasks.scheduler import scheduler as real_scheduler

    now = datetime.now(UTC)
    incremental = SimpleNamespace(
        id="refresh_prices_incremental", next_run_time=now + timedelta(minutes=15)
    )
    monkeypatch.setattr(real_scheduler, "get_jobs", lambda: [incremental])
    assert market_health.next_full_pass_at() is None

    full = SimpleNamespace(
        id="refresh_prices_07_00", next_run_time=now + timedelta(hours=8)
    )
    monkeypatch.setattr(real_scheduler, "get_jobs", lambda: [incremental, full])
    assert market_health.next_full_pass_at() == full.next_run_time


def test_next_check_is_only_announced_for_a_book_in_an_active_alert():
    """Une tâche globale ne suffit pas : le livre doit entrer dans son périmètre."""
    from types import SimpleNamespace

    from app.bot.handlers import market_panel
    from app.core.constants import AlertStatus

    when = datetime.now(UTC) + timedelta(hours=8)
    book = create_book_with_cache()
    book.market_cache.next_refresh_at = when

    def view_with(status, deleted_at=None):
        panel = SimpleNamespace(
            alert=SimpleNamespace(status=status, deleted_at=deleted_at)
        )
        return market_panel.PanelView(message=MagicMock(), panel=panel)

    assert market_panel._next_check_for(view_with(AlertStatus.ACTIVE), book) == when
    assert market_panel._next_check_for(view_with(AlertStatus.PAUSED), book) == when
    assert market_panel._next_check_for(view_with(AlertStatus.DELETED), book) is None
    assert market_panel._next_check_for(
        view_with(AlertStatus.ACTIVE, deleted_at=datetime.now(UTC)), book
    ) is None
    assert market_panel._next_check_for(
        market_panel.PanelView(message=MagicMock(), panel=SimpleNamespace(alert=None)), book
    ) is None

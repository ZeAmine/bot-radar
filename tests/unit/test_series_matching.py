"""Tests unitaires pour la comparaison de prix sur séries multi-tomes et lots."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.listing import Listing
from app.services.market_price_service import MarketPriceService
from app.services.tome_extractor import TomeExtractor, TomeMatchResult


def _create_mock_book(title: str, price_cents: int) -> Book:
    """Helper pour créer un livre factice avec un cache de prix actif."""
    book = Book(
        title=title,
        normalized_title=title.lower(),
        language="fr",
    )
    cache = BookMarketCache(
        lowest_delivered_price_cents=price_cents,
        historical_low_1y_cents=price_cents,
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
    assert "📉 Prix bas constaté (T2) : 18,50 €" in summary
    assert "Prix du marché" not in summary
    assert "✅ Écart : <b>-6,50 €</b>" in summary


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
    assert "📉 Prix bas constaté [1, 2, 3] : ~54,50 €" in summary
    assert "Prix du marché" not in summary
    # Gain : 25.00 - 54.50 = -29.50 €
    assert "✅ Écart : <b>-29,50 €</b>" in summary


def test_series_matching_generic_range_summary():
    """Vérifie le repli quand aucun tome n'est spécifié dans l'annonce."""
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
    assert "📉 Prix bas constaté :  —" in summary
    assert "⚖️ Écart :  —" in summary


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
    assert "💵 Lot" not in card
    assert "🆕 Vinted" not in card
    assert "Écart" not in card
    assert "Cote par tome (Min sur 1 an) :" in card
    assert "• Tome 1 : 16,00 €" in card
    assert "• Tome 2 : 18,50 €" in card
    assert "Derniere MAJ :" in card


def test_series_matching_chateau_des_animaux_lot_1_2_3():
    """Vérifie le cas réel de 'Le château des animaux 1.2.3' avec historique."""
    b1 = _create_mock_book("Le Château des animaux — Tome 1 (Miss Bengalore)", 1267)
    b1.market_cache.historical_low_1y_cents = 1267

    b2 = _create_mock_book("Le Château des animaux — Tome 2 (Les Marguerites d'hiver)", 1548)
    b2.market_cache.historical_low_1y_cents = 1548

    b3 = _create_mock_book("Le Château des animaux — Tome 3 (La Nuit des justes)", 2223)
    b3.market_cache.historical_low_1y_cents = 1415

    b4 = _create_mock_book("Le Château des animaux — Tome 4 (Le Sang du roi)", 1563)
    b4.market_cache.historical_low_1y_cents = 1563

    books = [b1, b2, b3, b4]

    # Extraction depuis le titre de l'utilisateur
    tome_res = TomeExtractor.extract(
        title="Le château des animaux,  1.2.3",
        series_key="Le château des animaux",
    )
    assert tome_res.is_lot is True
    assert tome_res.lot_tomes == [1, 2, 3]

    listing = Listing(
        source_name="vinted",
        external_id="chateau_123",
        url="https://vinted.fr",
        title="Le château des animaux,  1.2.3",
        normalized_title="le chateau des animaux 1 2 3",
        category_slug="bd_comics",
        item_price_cents=3300,
        estimated_total_cents=3718,
    )

    summary = MarketPriceService.get_comparison_summary(
        listing=listing,
        book=None,
        books_in_series=books,
        tome_result=tome_res,
    )
    assert summary is not None
    assert "Prix du marché" not in summary
    # Hist: 12.67 + 15.48 + 14.15 = 42.30 €
    assert "📉 Prix bas constaté [1, 2, 3] : ~42,30 €" in summary
    # Écart vs prix bas: 37.18 - 42.30 = -5.12 €
    assert "✅ Écart : <b>-5,12 €</b>" in summary


@pytest.mark.parametrize(
    ("lot_tomes", "item_total_cents", "expected_label", "expected_icon", "expected_diff"),
    [
        # Lot de 2 tomes consécutifs (T1 + T2 = 16.00 + 18.50 = 34.50 €)
        ([1, 2], 2500, "📉 Prix bas constaté [1, 2] : ~34,50 €", "✅", "-9,50 €"),
        # Lot de 2 tomes non-consécutifs (T1 + T3 = 16.00 + 20.00 = 36.00 €)
        ([1, 3], 3000, "📉 Prix bas constaté [1, 3] : ~36,00 €", "✅", "-6,00 €"),
        # Lot de 3 tomes (T1 + T2 + T3 = 54.50 €), prix Vinted supérieur (écart défavorable)
        ([1, 2, 3], 6000, "📉 Prix bas constaté [1, 2, 3] : ~54,50 €", "⛔️", "+5,50 €"),
        # Lot de 1 tome dans une liste de lot (T2 = 18.50 €)
        ([2], 1500, "📉 Prix bas constaté [2] : ~18,50 €", "✅", "-3,50 €"),
    ],
)
def test_series_matching_lot_variations_matrix(
    lot_tomes: list[int],
    item_total_cents: int,
    expected_label: str,
    expected_icon: str,
    expected_diff: str,
    ) -> None:
    """Matrice générale vérifiant la cotation cumulée selon différentes combinaisons de tomes."""
    books = [
        _create_mock_book("Série Test — Tome 1", 1600),
        _create_mock_book("Série Test — Tome 2", 1850),
        _create_mock_book("Série Test — Tome 3", 2000),
    ]
    listing = Listing(
        source_name="vinted",
        external_id="test_lot",
        url="https://vinted.fr",
        title=f"Lot tomes {lot_tomes}",
        normalized_title=f"lot tomes {lot_tomes}",
        category_slug="bd_comics",
        item_price_cents=item_total_cents - 200,
        estimated_total_cents=item_total_cents,
    )
    tome_res = TomeMatchResult(
        is_lot=True,
        lot_tomes=lot_tomes,
        lot_count=len(lot_tomes),
        confidence="high",
    )

    summary = MarketPriceService.get_comparison_summary(
        listing=listing,
        book=None,
        books_in_series=books,
        tome_result=tome_res,
    )
    assert summary is not None
    assert expected_label in summary
    assert f"{expected_icon} Écart : <b>{expected_diff}</b>" in summary


def test_series_matching_blacksad_generic():
    """Vérifie le rendu quand aucun tome n'est spécifié."""
    books = [
        _create_mock_book("Blacksad — Tome 1 (Quelque part entre les ombres)", 470),
        _create_mock_book("Blacksad — Tome 2 (Arctic-Nation)", 526),
        _create_mock_book("Blacksad — Tome 3 (Âme Rouge)", 584),
        _create_mock_book("Blacksad — Tome 4 (L'Enfer, le silence)", 670),
        _create_mock_book("Blacksad — Tome 5 (Amarillo)", 319),
        _create_mock_book("Blacksad — Tome 6 (Alors, tout tombe (Première partie))", 512),
        _create_mock_book("Blacksad — Tome 7 (Alors, tout tombe (Seconde partie))", 1049),
    ]
    listing = Listing(
        source_name="vinted",
        external_id="10084814764",
        url="https://vinted.fr/items/10084814764",
        title="Blacksad",
        normalized_title="blacksad",
        category_slug="bd_comics",
        item_price_cents=400,
        estimated_total_cents=778,
    )
    tome_res = TomeExtractor.extract(
        title=listing.title,
        description="Exemplaire Blacksad en état Très bon état.",
        series_key="Blacksad",
    )
    assert tome_res.tome_number is None

    summary = MarketPriceService.get_comparison_summary(
        listing=listing,
        book=None,
        books_in_series=books,
        tome_result=tome_res,
    )
    assert summary is not None
    assert "📉 Prix bas constaté :  —" in summary
    assert "⚖️ Écart :  —" in summary


def test_tome_extractor_subtitle_in_description():
    """Vérifie que la présence du sous-titre dans la description permet de détecter le tome."""
    tome_res = TomeExtractor.extract(
        title="BD Blacksad",
        description="Je vends le tome Quelque part entre les ombres en très bon état.",
        series_key="Blacksad",
    )
    assert tome_res.tome_number == 1
    assert tome_res.matched_subtitle == "quelque part entre les ombres"
    assert tome_res.confidence == "medium"


@pytest.mark.asyncio
async def test_handle_market_tome_select():
    """Vérifie la bascule interactive sur un tome précis dans le panneau de marché."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch
    from aiogram.types import CallbackQuery, Message
    from app.bot.handlers.callbacks import handle_market_tome_select

    listing_id = uuid.uuid4()
    book_id = uuid.uuid4()

    mock_listing = MagicMock()
    mock_listing.id = listing_id
    mock_listing.estimated_total_cents = 778
    mock_listing.item_price_cents = 400

    mock_book = _create_mock_book("Blacksad — Tome 1 (Quelque part entre les ombres)", 470)
    mock_book.id = book_id

    mock_callback = MagicMock(spec=CallbackQuery)
    mock_callback.data = f"market_tome:{listing_id}:{book_id}"
    mock_msg = MagicMock(spec=Message)
    mock_msg.edit_text = AsyncMock()
    mock_callback.message = mock_msg
    mock_callback.answer = AsyncMock()

    mock_session = AsyncMock()
    with (
        patch("app.bot.handlers.callbacks.ListingRepository") as mock_l_repo_cls,
        patch("app.bot.handlers.callbacks.BookRepository") as mock_b_repo_cls,
    ):
        mock_l_repo = MagicMock()
        mock_l_repo.get_by_id = AsyncMock(return_value=mock_listing)
        mock_l_repo_cls.return_value = mock_l_repo

        mock_b_repo = MagicMock()
        mock_b_repo.get_by_id = AsyncMock(return_value=mock_book)
        mock_b_repo_cls.return_value = mock_b_repo

        await handle_market_tome_select(mock_callback, mock_session)

        mock_msg.edit_text.assert_called_once()
        text = mock_msg.edit_text.call_args[1]["text"]
        assert "Tome 1 (Quelque part entre les ombres)" in text
        assert "4,70 €" in text
        keyboard = mock_msg.edit_text.call_args[1]["reply_markup"]
        assert any(btn.callback_data == f"market_details:{listing_id}" for row in keyboard.inline_keyboard for btn in row)



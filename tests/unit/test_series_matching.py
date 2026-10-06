import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.listing import Listing
from app.db.repositories.book_repo import resolve_loaded_books
from app.services.market_price_service import PENDING_COTE_LINE, MarketPriceService
from app.services.tome_extractor import TomeExtractor, TomeMatchResult


def _create_mock_book(title: str, price_cents: int | None) -> Book:
    """Helper pour créer un livre factice avec un cache de prix actif."""
    book = Book(
        id=uuid.uuid4(),
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
    assert "<b>Prix bas constaté (T2) :</b> 18,50 €" in summary
    assert "Prix du marché" not in summary
    assert "<b>Écart :</b> -6,50 € ✅" in summary


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
    assert "<b>Prix bas constaté (Lot) :</b> ~54,50 €" in summary
    assert "Prix du marché" not in summary
    # Gain : 25.00 - 54.50 = -29.50 €
    assert "<b>Écart :</b> -29,50 € ✅" in summary


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
    assert summary == "ℹ️ Série détectée : tome non identifié."
    assert "Écart" not in summary


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
    assert "Cote par tome (plus bas connu) :" in card
    assert "• Tome 1 : 16,00 €" in card
    assert "• Tome 2 : 18,50 €" in card
    assert "Dernière mise à jour :" in card


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
    assert "<b>Prix bas constaté (Lot) :</b> ~42,30 €" in summary
    # Écart vs prix bas: 37.18 - 42.30 = -5.12 €
    assert "<b>Écart :</b> -5,12 € ✅" in summary


@pytest.mark.parametrize(
    ("lot_tomes", "item_total_cents", "expected_label", "expected_icon", "expected_diff"),
    [
        # Lot de 2 tomes consécutifs (T1 + T2 = 16.00 + 18.50 = 34.50 €)
        ([1, 2], 2500, "<b>Prix bas constaté (Lot) :</b> ~34,50 €", "✅", "-9,50 €"),
        # Lot de 2 tomes non-consécutifs (T1 + T3 = 16.00 + 20.00 = 36.00 €)
        ([1, 3], 3000, "<b>Prix bas constaté (Lot) :</b> ~36,00 €", "✅", "-6,00 €"),
        # Lot de 3 tomes (T1 + T2 + T3 = 54.50 €), prix Vinted supérieur (écart défavorable)
        ([1, 2, 3], 6000, "<b>Prix bas constaté (Lot) :</b> ~54,50 €", "⛔️", "+5,50 €"),
        # Lot de 1 tome dans une liste de lot (T2 = 18.50 €)
        ([2], 1500, "<b>Prix bas constaté (Lot) :</b> ~18,50 €", "✅", "-3,50 €"),
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
    assert f"<b>Écart :</b> {expected_diff} {expected_icon}" in summary


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
    assert summary == "ℹ️ Série détectée : tome non identifié."
    assert "Écart" not in summary


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
    from unittest.mock import AsyncMock, MagicMock, patch

    from aiogram.types import CallbackQuery, Message

    from app.bot.handlers.callbacks import _encode_tome_ref, handle_market_tome_select

    listing_id = uuid.uuid4()
    book_id = uuid.uuid4()

    mock_listing = MagicMock()
    mock_listing.id = listing_id
    mock_listing.estimated_total_cents = 778
    mock_listing.item_price_cents = 400

    mock_book = _create_mock_book("Blacksad — Tome 1 (Quelque part entre les ombres)", 470)
    mock_book.id = book_id

    mock_callback = MagicMock(spec=CallbackQuery)
    mock_callback.data = _encode_tome_ref(listing_id, book_id)
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
        mock_l_repo.get_alert_for_listing = AsyncMock(return_value=None)
        mock_l_repo_cls.return_value = mock_l_repo

        mock_b_repo = MagicMock()
        mock_b_repo.get_by_id = AsyncMock(return_value=mock_book)
        mock_b_repo.get_all_books_for_alert = AsyncMock(return_value=[mock_book])
        mock_b_repo_cls.return_value = mock_b_repo

        await handle_market_tome_select(mock_callback, mock_session)

        mock_msg.edit_text.assert_called_once()
        text = mock_msg.edit_text.call_args[1]["text"]
        assert "📊 <b>Prix du marché</b>" in text
        assert "4,70 €" in text
        keyboard = mock_msg.edit_text.call_args[1]["reply_markup"]
        assert any(
            btn.callback_data == f"market_details:{listing_id}"
            for row in keyboard.inline_keyboard
            for btn in row
        )


@pytest.mark.asyncio
async def test_market_tome_view_keeps_the_tome_selection_when_series_has_several():
    """E5 : depuis un tome, les autres tomes restent sélectionnables — pas de cul-de-sac.

    Le panneau d'un tome résolu n'affichait que « Tous les tomes » : impossible de
    passer directement d'un tome à un autre sans revenir à la liste.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from aiogram.types import CallbackQuery, Message

    from app.bot.handlers.callbacks import _encode_tome_ref, handle_market_tome_select

    listing_id = uuid.uuid4()
    book_id = uuid.uuid4()
    other_book_id = uuid.uuid4()

    mock_listing = MagicMock()
    mock_listing.id = listing_id
    mock_listing.estimated_total_cents = 778
    mock_listing.item_price_cents = 400

    mock_book = _create_mock_book("Blacksad — Tome 1 (Quelque part entre les ombres)", 470)
    mock_book.id = book_id
    other_book = _create_mock_book("Blacksad — Tome 2 (Arctic-Nation)", 899)
    other_book.id = other_book_id

    mock_callback = MagicMock(spec=CallbackQuery)
    mock_callback.data = _encode_tome_ref(listing_id, book_id)
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
        mock_l_repo.get_alert_for_listing = AsyncMock(
            return_value=MagicMock(isbn=None, name="Blacksad", series_total_cents=None)
        )
        mock_l_repo_cls.return_value = mock_l_repo

        mock_b_repo = MagicMock()
        mock_b_repo.get_by_id = AsyncMock(return_value=mock_book)
        mock_b_repo.get_all_books_for_alert = AsyncMock(return_value=[mock_book, other_book])
        mock_b_repo_cls.return_value = mock_b_repo

        await handle_market_tome_select(mock_callback, mock_session)

        keyboard = mock_msg.edit_text.call_args[1]["reply_markup"]
        tome_buttons = [
            btn
            for row in keyboard.inline_keyboard
            for btn in row
            if (btn.callback_data or "").startswith("market_tome:")
        ]
        assert len(tome_buttons) == 2, "tous les tomes restent accessibles"
        assert any(
            btn.callback_data == f"market_details:{listing_id}"
            for row in keyboard.inline_keyboard
            for btn in row
        )


@pytest.mark.asyncio
async def test_series_panel_offers_tome_selection_even_when_the_tome_is_resolved():
    """E5 : le panneau d'un tome résolu propose quand même la sélection par tome.

    Avant E5, les boutons n'apparaissaissaient que quand le tome n'était pas
    identifié (`not book`) : un tome résolu fermait la série au lieu de l'ouvrir.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from aiogram.types import CallbackQuery, Message

    from app.bot.handlers.callbacks import handle_view_market_details

    listing_id = uuid.uuid4()

    mock_listing = MagicMock(id=listing_id, estimated_total_cents=1198, title="Blast Tome 2")
    tome1 = _create_mock_book("Blast — Tome 1 (Grasse Carcasse)", 509)
    tome2 = _create_mock_book("Blast — Tome 2 (L'Apocalypse selon Saint Jacky)", 899)
    books = [tome1, tome2]

    mock_msg = MagicMock(spec=Message)
    mock_msg.reply = AsyncMock()
    mock_msg.html_text = ""

    mock_callback = MagicMock(spec=CallbackQuery)
    mock_callback.data = f"market:{listing_id}"
    mock_callback.message = mock_msg
    mock_callback.answer = AsyncMock()

    mock_session = AsyncMock()
    with (
        patch("app.use_cases.view_market_details.ListingRepository") as mock_l_cls,
        patch("app.use_cases.view_market_details.BookRepository") as mock_b_cls,
        patch("app.use_cases.view_market_details.MarketPriceRepository") as mock_m_cls,
    ):
        mock_l = MagicMock()
        mock_l.get_by_id = AsyncMock(return_value=mock_listing)
        mock_l.get_alert_for_listing = AsyncMock(
            return_value=MagicMock(isbn=None, name="Blast", series_total_cents=None)
        )
        mock_l_cls.return_value = mock_l

        mock_b = MagicMock()
        # Le tome est résolu par le texte de l'annonce — la série reste ouverte.
        mock_b.resolve_book_for_listing = AsyncMock(return_value=(tome2, books, None))
        mock_b_cls.return_value = mock_b

        mock_m = MagicMock()
        mock_m.get_latest_snapshot = AsyncMock(return_value=None)
        mock_m_cls.return_value = mock_m

        await handle_view_market_details(mock_callback, mock_session)

    mock_msg.reply.assert_called_once()
    keyboard = mock_msg.reply.call_args[1]["reply_markup"]
    tome_buttons = [
        btn
        for row in keyboard.inline_keyboard
        for btn in row
        if (btn.callback_data or "").startswith("market_tome:")
    ]
    assert len(tome_buttons) == 2, "un tome résolu n'a pas fermé la série"


@pytest.mark.asyncio
async def test_handle_view_market_details_success():
    """Vérifie l'ouverture normale du panneau de marché lors du clic sur le bouton d'alerte."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from aiogram.types import CallbackQuery, Message

    from app.bot.handlers.callbacks import handle_view_market_details

    listing_id = uuid.uuid4()
    mock_listing = MagicMock(id=listing_id, estimated_total_cents=1200, title="Les Indes fourbes")
    mock_book = _create_mock_book("Les Indes fourbes", 548)
    mock_book.id = uuid.uuid4()

    mock_msg = MagicMock(spec=Message)
    mock_msg.reply = AsyncMock()
    mock_msg.html_text = ""

    mock_callback = MagicMock(spec=CallbackQuery)
    mock_callback.data = f"market:{listing_id}"
    mock_callback.message = mock_msg
    mock_callback.answer = AsyncMock()

    mock_session = AsyncMock()
    with (
        patch("app.use_cases.view_market_details.ListingRepository") as mock_l_cls,
        patch("app.use_cases.view_market_details.BookRepository") as mock_b_cls,
        patch("app.use_cases.view_market_details.MarketPriceRepository") as mock_m_cls,
    ):
        mock_l = MagicMock()
        mock_l.get_by_id = AsyncMock(return_value=mock_listing)
        mock_l.get_alert_for_listing = AsyncMock(
            return_value=MagicMock(isbn=None, name="Les Indes fourbes")
        )
        mock_l_cls.return_value = mock_l

        mock_b = MagicMock()
        mock_b.resolve_book_for_listing = AsyncMock(
            return_value=(mock_book, [mock_book], None)
        )
        mock_b_cls.return_value = mock_b

        mock_m = MagicMock()
        mock_m.get_latest_snapshot = AsyncMock(return_value=None)
        mock_m_cls.return_value = mock_m

        await handle_view_market_details(mock_callback, mock_session)

        mock_msg.reply.assert_called_once()
        text = mock_msg.reply.call_args[1]["text"]
        assert "📊 <b>Prix du marché</b>" in text
        assert "5,48 €" in text
        mock_callback.answer.assert_called_once()


@pytest.mark.asyncio
async def test_handle_view_market_details_series_buttons_fit_telegram_limit():
    """Le panneau d'une série doit parsser : un callback_data trop long fait rejeter le message.

    Telegram plafonne `callback_data` à 64 octets et rejette alors le message entier
    (BUTTON_DATA_INVALID). Depuis E5, les boutons par tome figurent sur **tout**
    panneau de série (résolu ou non) : la contrainte de longueur touche donc chaque
    vue, pas seulement la série muette.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from aiogram.types import CallbackQuery, Message

    from app.bot.handlers.callbacks import handle_view_market_details

    listing_id = uuid.uuid4()
    mock_listing = MagicMock(id=listing_id, estimated_total_cents=1198, title="Blast")
    tome1 = _create_mock_book("Blast — Tome 1 (Grasse Carcasse)", 509)
    tome2 = _create_mock_book("Blast — Tome 2 (L'Apocalypse selon Saint Jacky)", 899)
    books = [tome1, tome2]

    mock_msg = MagicMock(spec=Message)
    mock_msg.reply = AsyncMock()
    mock_msg.html_text = ""

    mock_callback = MagicMock(spec=CallbackQuery)
    mock_callback.data = f"market:{listing_id}"
    mock_callback.message = mock_msg
    mock_callback.answer = AsyncMock()

    mock_session = AsyncMock()
    with (
        patch("app.use_cases.view_market_details.ListingRepository") as mock_l_cls,
        patch("app.use_cases.view_market_details.BookRepository") as mock_b_cls,
        patch("app.use_cases.view_market_details.MarketPriceRepository") as mock_m_cls,
    ):
        mock_l = MagicMock()
        mock_l.get_by_id = AsyncMock(return_value=mock_listing)
        mock_l.get_alert_for_listing = AsyncMock(
            return_value=MagicMock(isbn=None, name="Blast", series_total_cents=None)
        )
        mock_l_cls.return_value = mock_l

        mock_b = MagicMock()
        # Tome non résolu : c'est ce cas qui produit les boutons par tome.
        mock_b.resolve_book_for_listing = AsyncMock(return_value=(None, books, None))
        mock_b_cls.return_value = mock_b

        mock_m = MagicMock()
        mock_m.get_latest_snapshot = AsyncMock(return_value=None)
        mock_m_cls.return_value = mock_m

        await handle_view_market_details(mock_callback, mock_session)

    mock_msg.reply.assert_called_once()
    keyboard = mock_msg.reply.call_args[1]["reply_markup"]
    buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    assert any((btn.callback_data or "").startswith("market_tome:") for btn in buttons)
    for btn in buttons:
        assert 1 <= len(btn.callback_data.encode()) <= 64, btn.callback_data


def test_market_panel_is_routed_for_both_entry_and_back_to_all_tomes():
    """« Tous les tomes » porte `market_details:` : le filtre doit le router.

    Le panneau n'était enregistré que pour `market:`. Le retour « Tous les tomes »
    ne désignait donc aucun handler et le bouton restait mort.
    """
    from unittest.mock import MagicMock

    from app.bot.handlers import callbacks

    def routed_for(data: str) -> list[str]:
        event = MagicMock()
        event.data = data
        return [
            handler.callback.__name__
            for handler in callbacks.router.callback_query.handlers
            for filter_obj in handler.filters
            if filter_obj.magic is not None and filter_obj.callback(event)
        ]

    assert "handle_view_market_details" in routed_for("market:abc")
    assert "handle_view_market_details" in routed_for("market_details:abc")


def test_market_tome_ref_round_trip():
    """La référence encodée se relit exactement : c'est elle qui désigne le tome cliqué."""
    from app.bot.handlers.callbacks import _decode_tome_ref, _encode_tome_ref

    listing_id, book_id = uuid.uuid4(), uuid.uuid4()
    ref = _encode_tome_ref(listing_id, book_id)

    assert len(ref.encode()) <= 64
    assert _decode_tome_ref(ref) == (listing_id, book_id)
    # Une donnée tronquée, forgée ou d'un autre panneau ne doit pas lever.
    assert _decode_tome_ref(None) is None
    assert _decode_tome_ref("market_details:x") is None
    assert _decode_tome_ref("market_tome:pas-du-base64:non-plus") is None


def test_series_card_distingue_une_absence_constatee_d_une_cote_en_cours():
    """Un tome dont la passe a conclu à l'absence d'offre ne compte plus dans l'attente.

    Les deux comptes portent sur les mêmes tomes sans prix, mais ne disent pas la
    même chose : l'un attend une cote, l'autre l'a déjà eue.
    """
    priced = _create_mock_book("Blast — Tome 1 (Grasse Carcasse)", 500)
    waiting = _create_mock_book("Blast — Tome 2 (L'Apocalypse selon Saint Jacky)", None)
    waiting.market_cache = None
    settled = _create_mock_book("Blast — Tome 3 (La Vallée des larmes)", None)
    settled.market_cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=settled.id,
        comparison_status=ComparisonStatus.UNAVAILABLE,
        checked_at=datetime(2026, 10, 6, 9, 30, tzinfo=UTC),
    )
    listing = Listing(
        source_name="vinted",
        external_id="blast",
        url="https://vinted.fr",
        title="Blast de Manu Larcenet",
        normalized_title="blast de manu larcenet",
        category_slug="bd_comics",
        item_price_cents=800,
        estimated_total_cents=1198,
    )

    card = MarketPriceService.format_market_details_card(
        listing=listing,
        book=None,
        books_in_series=[priced, waiting, settled],
        tome_result=None,
    )

    assert "⏳ 1 tome(s) en cours de cote…" in card
    assert "🔍 1 tome(s) sans aucune offre trouvée." in card
    # L'attente générique n'existe plus : chaque silence a son compte.
    assert PENDING_COTE_LINE not in card


def test_series_card_counts_every_unpriced_tome_when_none_is_priced():
    """Sans aucune cote en base, le panneau annonce une attente au lieu d'être vide."""
    books = [
        _create_mock_book("Blast — Tome 1 (Grasse Carcasse)", None),
        _create_mock_book("Blast — Tome 2 (L'Apocalypse selon Saint Jacky)", None),
    ]
    for book in books:
        book.market_cache = None
    listing = Listing(
        source_name="vinted",
        external_id="blast",
        url="https://vinted.fr",
        title="Blast de Manu Larcenet",
        normalized_title="blast de manu larcenet",
        category_slug="bd_comics",
        item_price_cents=800,
        estimated_total_cents=1198,
    )

    card = MarketPriceService.format_market_details_card(
        listing=listing,
        book=None,
        books_in_series=books,
        tome_result=None,
    )
    assert "Cotes par tome" in card
    assert "⏳ 2 tome(s) en cours de cote…" in card
    # Le total Vinted reste affiché : c'est la seule information fiable ici.
    assert "11,98 €" in card



def test_rip_tome_titles_cleaning_no_stray_parenthesis():
    """Vérifie que les titres de la série RIP ne produisent aucune parenthèse orpheline."""
    from app.services.market_price_service import clean_tome_display_title

    titles = [
        "RIP — Tome 1 (Derrick — Je ne survivrai pas à la mort)",
        "RIP — Tome 2 (Maurice — Les mouches ne mentent pas)",
        "RIP — Tome 3 (Ahmed — Pourrissant dans la neige)",
        "RIP — Tome 4 (Albert — Prière pour les vivants)",
        "RIP — Tome 5 (Fanette — Mauvaise graine)",
        "RIP — Tome 6 (Eugène — Toutes les bonnes choses ont une fin)",
    ]
    for idx, t in enumerate(titles, start=1):
        clean = clean_tome_display_title(t)
        assert clean == f"Tome {idx}"
        assert not clean.endswith(")")
        assert not clean.startswith("(")

        with_sub = clean_tome_display_title(t, include_subtitle=True)
        assert with_sub.count("(") == with_sub.count(")")
        assert not with_sub.endswith("))")


def test_fraction_volume_not_treated_as_lot():
    """Vérifie que 'vol 2/8' ou 'tome 1/5' est bien extrait en tome unitaire et non en lot."""
    # Cas exact de l'annonce utilisateur
    res = TomeExtractor.extract("Batman par Grant Morrison vol 2/8: RIP", series_key="rip")
    assert res.is_lot is False
    assert res.tome_number == 2

    # Autres variantes fractionnaires
    res2 = TomeExtractor.extract("Blacksad tome 1/5", series_key="blacksad")
    assert res2.is_lot is False
    assert res2.tome_number == 1

    res3 = TomeExtractor.extract("RIP 2/6", series_key="rip")
    assert res3.is_lot is False
    assert res3.tome_number == 2


def test_rip_secondary_subtitles_detection():
    """Vérifie la détection des tomes de RIP via les sous-titres secondaires."""
    r1 = TomeExtractor.extract("BD RIP Les mouches ne mentent pas", series_key="rip")
    assert r1.tome_number == 2

    r2 = TomeExtractor.extract("RIP Mauvaise graine", series_key="rip")
    assert r2.tome_number == 5

    r3 = TomeExtractor.extract("RIP Prière pour les vivants", series_key="rip")
    assert r3.tome_number == 4


def test_format_market_details_card_no_cote_indisponible_in_lot():
    """Vérifie qu'aucun libellé 'Cote indisponible' n'apparaît dans les cartes de lot."""
    b1 = _create_mock_book("RIP — Tome 1 (Derrick — La mort dans l'âme)", 1200)
    b2 = _create_mock_book("RIP — Tome 2 (Maurice — Les mouches ne mentent pas)", None)
    listing = Listing(
        source_name="vinted",
        external_id="rip_lot",
        url="https://vinted.fr",
        title="Lot RIP T1 et T2",
        normalized_title="lot rip t1 et t2",
        category_slug="bd_comics",
        item_price_cents=1500,
        estimated_total_cents=1800,
    )
    tome_res = TomeMatchResult(is_lot=True, lot_tomes=[1, 2], lot_count=2, confidence="high")

    card = MarketPriceService.format_market_details_card(
        listing=listing,
        book=None,
        books_in_series=[b1, b2],
        tome_result=tome_res,
    )
    assert "Cote indisponible" not in card
    assert "• Tome 1 : 12,00 €" in card
    assert "Tome 2" not in card  # pas de cote -> exclu du résumé propre



def test_market_details_card_series_tomes_formatting():
    """Vérifie que la carte de détails affiche strictement 'Prix du marché' en titre,
    exclut les entrées sans tome ou sans cote ('Blacksad, cote indisponible'),
    et met en gras le tome courant dans la liste des tomes."""
    root_book = _create_mock_book("Blacksad", None)
    b1 = _create_mock_book("Blacksad — Tome 1 (Quelque part entre les ombres)", 470)
    b2 = _create_mock_book("Blacksad — Tome 2 (Arctic-Nation)", 526)
    b3 = _create_mock_book("Blacksad — Tome 3 (Âme Rouge)", 584)
    b4 = _create_mock_book("Blacksad — Tome 4 (L'Enfer, le silence)", 670)
    b5 = _create_mock_book("Blacksad — Tome 5 (Amarillo)", 319)
    b6 = _create_mock_book("Blacksad — Tome 6 (Alors, tout tombe (Première partie))", 512)
    b7 = _create_mock_book("Blacksad — Tome 7 (Alors, tout tombe (Seconde partie))", 1049)

    books = [root_book, b1, b2, b3, b4, b5, b6, b7]
    listing = Listing(
        source_name="vinted",
        external_id="10084814764",
        url="https://vinted.fr/items/10084814764",
        title="Blacksad Tome 5 Amarillo",
        normalized_title="blacksad tome 5 amarillo",
        category_slug="bd_comics",
        item_price_cents=400,
        estimated_total_cents=778,
    )

    card = MarketPriceService.format_market_details_card(
        listing=listing,
        book=b5,
        books_in_series=books,
    )

    # Titre strictement 'Prix du marché'
    assert "📊 <b>Prix du marché</b>\n" in card
    assert "Amarillo" not in card.split("\n")[0]

    # Pas d'entrée parasite non numérotée sans cote
    assert "• Blacksad : Cote indisponible" not in card
    assert "Cote indisponible" not in card

    # Section Autres tomes de la série
    assert "<b>Autres tomes de la série :</b>" in card
    assert "• Tome 1 : 4,70 €" in card
    assert "• Tome 2 : 5,26 €" in card
    assert "• Tome 4 : 6,70 €" in card
    # Tome courant en gras
    assert "• <b>Tome 5 : 3,19 €</b>" in card
    assert "• Tome 6 : 5,12 €" in card
    assert "• Tome 7 : 10,49 €" in card


@pytest.mark.asyncio
async def test_resolve_book_for_listing_does_not_swallow_tome2_when_single_generic_book():
    """Vérifie que si une alerte de série ne possède qu'un livre générique (sans n° de tome),
    une annonce pour le Tome 2 renvoie book=None pour permettre la découverte BNF du Tome 2.
    """
    from unittest.mock import AsyncMock, MagicMock

    from app.db.repositories.book_repo import BookRepository

    mock_session = MagicMock()
    repo = BookRepository(mock_session)

    generic_book = Book(
        id=uuid.uuid4(),
        title="L'Arabe du futur",
        normalized_title="l arabe du futur",
        language="fr",
    )
    repo.get_all_books_for_alert = AsyncMock(return_value=[generic_book])

    resolved_book, books_in_series, tome_res = await repo.resolve_book_for_listing(
        alert_id=uuid.uuid4(),
        alert_name="L'Arabe du futur",
        listing_title="L’arabe du futur 2",
    )

    assert tome_res.tome_number == 2
    # Ne doit SURTOUT PAS renvoyer le livre générique comme étant le Tome 2 !
    assert resolved_book is None
    assert books_in_series == [generic_book]


@pytest.mark.asyncio
async def test_resolve_book_for_listing_matches_existing_tome_number():
    """Vérifie que si le tome recherché existe parmi les livres catalogués,
    il est immédiatement et correctement résolu.
    """
    from unittest.mock import AsyncMock, MagicMock

    from app.db.repositories.book_repo import BookRepository

    mock_session = MagicMock()
    repo = BookRepository(mock_session)

    b1 = Book(
        id=uuid.uuid4(),
        title="L'Arabe du futur 1",
        normalized_title="l arabe du futur 1",
        language="fr",
    )
    b2 = Book(
        id=uuid.uuid4(),
        title="L’arabe du futur 2",
        normalized_title="l arabe du futur 2",
        language="fr",
    )
    repo.get_all_books_for_alert = AsyncMock(return_value=[b1, b2])

    resolved_book, books_in_series, tome_res = await repo.resolve_book_for_listing(
        alert_id=uuid.uuid4(),
        alert_name="L'Arabe du futur",
        listing_title="L’arabe du futur 2",
    )

    assert tome_res.tome_number == 2
    assert resolved_book is not None
    assert resolved_book.id == b2.id


@pytest.mark.asyncio
async def test_resolve_book_for_listing_one_shot_returns_single_book():
    """Vérifie qu'un one-shot sans mention de tome continue d'être résolu sur l'unique livre."""
    from unittest.mock import AsyncMock, MagicMock

    from app.db.repositories.book_repo import BookRepository

    mock_session = MagicMock()
    repo = BookRepository(mock_session)

    one_shot = Book(
        id=uuid.uuid4(),
        title="Jimmy Corrigan",
        normalized_title="jimmy corrigan",
        language="fr",
    )
    repo.get_all_books_for_alert = AsyncMock(return_value=[one_shot])

    resolved_book, books_in_series, tome_res = await repo.resolve_book_for_listing(
        alert_id=uuid.uuid4(),
        alert_name="Jimmy Corrigan",
        listing_title="Jimmy Corrigan roman graphique",
    )

    assert tome_res.tome_number is None
    assert not tome_res.is_lot
    assert resolved_book is not None
    assert resolved_book.id == one_shot.id


# --- Lot dénombré : le vendeur donne un compte, pas des numéros ---

_CHATEAU_COTES = [
    ("Le Château des animaux — Tome 1 (Miss Bengalore)", 670),
    ("Le Château des animaux — Tome 2 (Les Marguerites d'hiver)", 922),
    ("Le Château des animaux — Tome 3 (La Nuit des justes)", 1167),
    ("Le Château des animaux — Tome 4 (Le Sang du roi)", 1318),
    ("Le château des animaux", 1775),
]
"""Les quatre tomes du Château des animaux aux cotes relevées en production,
plus la fiche sans numéro que porte aussi cette alerte."""


def _chateau_catalogue() -> list[Book]:
    return [_create_mock_book(title, price) for title, price in _CHATEAU_COTES]


def _chateau_lot_listing() -> Listing:
    """L'annonce du lot à 50,00 € + 6,08 € de frais, soit 56,08 € fdp."""
    return Listing(
        source_name="vinted",
        external_id="chateau",
        url="https://vinted.fr",
        title="Lot de 4 tomes Le Chateau des animaux",
        normalized_title="lot de 4 tomes le chateau des animaux",
        description="Exemplaires en très bon état.",
        category_slug="bd_comics",
        item_price_cents=5000,
        estimated_total_cents=5608,
    )


def test_counted_lot_resolves_to_the_whole_series_and_prices_it():
    """« Lot de 4 tomes » face à une série de 4 tomes : la cote est cumulative.

    C'est le cas que l'extracteur produisait déjà (`lot_count=4`) mais que la
    résolution jetait : sans numéros, le lot retombait sur « cote indisponible ».
    """
    books = _chateau_catalogue()
    listing = _chateau_lot_listing()

    resolved_book, series, tome_res = resolve_loaded_books(
        alert_name="Le Château des animaux",
        listing_title=listing.title,
        listing_description=listing.description,
        books=books,
    )

    assert resolved_book is None
    assert tome_res.is_lot is True
    assert tome_res.lot_count == 4
    assert tome_res.lot_tomes == [1, 2, 3, 4]
    # La composition vient de nous, pas du vendeur : le libellé doit le dire.
    assert tome_res.composition_inferred is True

    summary = MarketPriceService.get_comparison_summary(
        listing=listing,
        book=resolved_book,
        books_in_series=series,
        tome_result=tome_res,
    )
    assert summary is not None
    assert "<b>Prix bas constaté (Lot de 4 tomes, composition déduite) :</b> ~40,77 €" in summary
    assert "<b>Écart :</b> +15,31 € ⛔️" in summary


def test_counted_lot_is_refused_when_the_series_is_longer():
    """Une série plus longue que le compte annoncé ne laisse aucune composition."""
    books = _chateau_catalogue()
    books.append(_create_mock_book("Le Château des animaux — Tome 5 (Le Retour)", 1400))

    _, _, tome_res = resolve_loaded_books(
        alert_name="Le Château des animaux",
        listing_title="Lot de 4 tomes Le Chateau des animaux",
        listing_description=None,
        books=books,
    )

    assert tome_res.lot_count == 4
    assert tome_res.lot_tomes == []
    assert tome_res.composition_inferred is False


def test_counted_lot_is_refused_when_a_tome_has_two_editions():
    """Deux fiches pour le même tome : on ne sait pas laquelle est dans le lot."""
    books = _chateau_catalogue()
    books.append(_create_mock_book("Le Château des animaux Tome 2 (édition poche)", 800))

    _, _, tome_res = resolve_loaded_books(
        alert_name="Le Château des animaux",
        listing_title="Lot de 4 tomes Le Chateau des animaux",
        listing_description=None,
        books=books,
    )

    assert tome_res.lot_tomes == []
    assert tome_res.composition_inferred is False


def test_counted_lot_is_refused_without_a_single_numbered_tome():
    """Sans fiche numérotée, le compte ne compose rien."""
    _, _, tome_res = resolve_loaded_books(
        alert_name="Le Château des animaux",
        listing_title="Lot de 4 tomes Le Chateau des animaux",
        listing_description=None,
        books=[_create_mock_book("Le château des animaux", 1775)],
    )

    assert tome_res.lot_tomes == []
    assert tome_res.composition_inferred is False


def test_explicitly_numbered_lot_is_not_flagged_as_inferred():
    """Quand le vendeur nomme les tomes, la composition reste énoncée."""
    _, _, tome_res = resolve_loaded_books(
        alert_name="Le Château des animaux",
        listing_title="Le chateau des animaux tomes 1 à 4",
        listing_description=None,
        books=_chateau_catalogue(),
    )

    assert tome_res.lot_tomes == [1, 2, 3, 4]
    assert tome_res.composition_inferred is False





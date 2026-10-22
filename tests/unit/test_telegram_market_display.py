import uuid
from itertools import permutations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

from app.bot.handlers import market_panel
from app.bot.handlers.callbacks import _decode_tome_ref, _tome_selection_rows
from app.db.models.alert import Alert
from app.services.market_price_service import MarketPriceService, series_reference_total
from app.use_cases.view_market_details import MarketPanelData
from tests.unit.test_series_matching import _create_mock_book


def _series_books(title="Absolute Master"):
    first = _create_mock_book(f"{title} — Tome 1", 1149)
    second = _create_mock_book(f"{title} — Tome 2", 1669)
    edition = _create_mock_book(f"{title}, Vol. 2", 9900)
    root = _create_mock_book(title, None)
    for book, code in zip(
        (first, second, edition, root),
        ("9791026853794", "9791026855682", "9781799510871", None), strict=True,
    ):
        book.isbn13 = code
    return [first, second, edition, root]


@pytest.mark.parametrize("title", ["Absolute Master", "Absolute Martian Manhunter"])
def test_two_real_volumes_produce_two_buttons_independently_of_editions(title):
    books = _series_books(title)
    source_id = uuid.uuid4()
    original_total = series_reference_total(books)
    for ordered in permutations(books):
        rows = _tome_selection_rows(source_id, list(ordered))
        buttons = [button for row in rows for button in row]
        assert [button.text for button in buttons] == ["T1", "T2"]
        for button in buttons:
            decoded = _decode_tome_ref(button.callback_data)
            assert decoded is not None
            assert decoded[0] == source_id
            assert decoded[1] in {book.id for book in books}
            assert len(button.callback_data.encode()) <= 64
    assert series_reference_total(books) == original_total is None


def test_series_buttons_exclude_a_neighboring_series():
    books = _series_books()
    books.append(_create_mock_book("Absolute Master Universe — Tome 3", 1234))
    rows = _tome_selection_rows(uuid.uuid4(), books, series_title="Absolute Master")
    assert [b.text for row in rows for b in row] == ["T1", "T2"]


def test_series_lines_keep_only_number_and_price_without_assigning_an_edition():
    books = _series_books()
    lines = MarketPriceService._series_books_lines(books, books[0])
    assert lines == ["Tome 1 : 11,49 €", "Tome 2 : minimum annuel indisponible"]


@pytest.mark.parametrize("priced", [True, False])
def test_market_heading_reuses_the_verified_cal_url_and_isbn_is_copyable(priced):
    book = _series_books()[0]
    url = "https://www.chasse-aux-livres.fr/prix/9791026853794/absolute-master"
    if not priced:
        book.market_cache = None
    else:
        book.market_cache.source_history = {**book.market_cache.source_history, "source_url": url}
    card = MarketPriceService.format_market_details_card(None, book)
    assert "<b>ISBN :</b> <code>9791026853794</code>" in card
    if priced:
        assert f'<b><a href="{url}">Prix du marché</a></b>' in card
    else:
        assert "📊 <b>Prix du marché</b>" in card


@pytest.mark.parametrize("url", [
    "https://www.chasse-aux-livres.fr/prix/9791026855682/another-book",
    "https://www.chasse-aux-livres.fr.evil.example/prix/9791026853794/book",
    "javascript:alert(1)",
    None,
])
def test_market_heading_never_links_to_an_unverified_book(url):
    book = _series_books()[0]
    book.market_cache.source_history = {**book.market_cache.source_history, "source_url": url}
    book.market_cache.best_source_url = url
    card = MarketPriceService.format_market_details_card(None, book)
    assert "📊 <b>Prix du marché</b>" in card
    assert '>Prix du marché</a>' not in card


@pytest.mark.asyncio
async def test_tome_navigation_keeps_duplicate_editions_ambiguous_and_rejects_foreign_books():
    books = _series_books()
    message = MagicMock(spec=Message)
    message.chat = MagicMock(id=18)
    message.message_id = 29
    message.edit_text = AsyncMock()
    callback = MagicMock(message=message)
    callback.answer = AsyncMock()
    panel = MarketPanelData(None, books[0], books, None, None)
    view = market_panel.PanelView(message, panel, listing_id=uuid.uuid4(), selected_id=books[0].id)
    with patch.object(market_panel, "_panels", {(18, 29): view}):
        await market_panel.select_panel_tome(callback, books[1].id)
        text = message.edit_text.await_args.args[0]
        assert "plusieurs éditions" in text
        assert "<b>ISBN :</b>" not in text
        assert "99,00 €" not in text
        await market_panel.select_panel_tome(callback, books[0].id)
        text = message.edit_text.await_args.args[0]
        assert "<code>9791026853794</code>" in text
        previous = message.edit_text.await_count
        await market_panel.select_panel_tome(callback, uuid.uuid4())
        assert message.edit_text.await_count == previous
        assert callback.answer.await_args.args == ("Tome introuvable.",)
        market_panel.close_panel(message)
        await market_panel.render_panel(view)
        assert message.edit_text.await_count == previous


def test_confirmed_series_edition_is_used_instead_of_an_arbitrary_duplicate():
    books = _series_books()
    alert = Alert(name="Absolute Master", preparation={"series_volumes": [
        {"number": 2, "isbn13": books[1].isbn13, "confirmed": True},
    ]})
    panel = MarketPanelData(None, books[0], books, None, None, alert=alert)
    view = market_panel.PanelView(MagicMock(spec=Message), panel,
                                  listing_id=uuid.uuid4(), selected_id=books[2].id)
    text, keyboard = market_panel._content(view)
    assert "<code>9791026855682</code>" in text
    assert "<code>9781799510871</code>" not in text
    assert "Tome 2 : 16,69 €" in text
    assert [b.text for row in keyboard.inline_keyboard for b in row] == ["T1", "T2", "Fermer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("chosen_author", ["David Sala", "George Bess"])
async def test_frankenstein_homonyms_keep_their_author_and_isbn_through_creation(
    reverse, chosen_author,
):
    from app.bot.handlers.alerts_create_confirm import select_alert_creation_work
    from app.bot.handlers.alerts_helpers import build_creation_recap, draft_updates_from_search
    from app.services.book_metadata_service import BookMetadata, BookMetadataService
    from app.use_cases.book_search import result_from_report, result_from_resolution
    from tests.add_fakes import DraftState
    from tests.unit.test_book_metadata_service import _mock_bnf_typed
    from tests.unit.test_telegram_book_contract import _callback

    records = [
        BookMetadata(title="Frankenstein", author="David Sala", isbn13="9782205078054"),
        BookMetadata(title="Frankenstein", author="George Bess", isbn13="9782369903086"),
        BookMetadata(title="Frankenstein", author="David Sala", isbn13="9782369904151"),
    ]
    if reverse:
        records.reverse()
    service = BookMetadataService()
    with _mock_bnf_typed(service, records):
        report = await service.resolve_for_alert_report("Frankenstein")
    result = result_from_report(report, query_title="Frankenstein")
    updates = draft_updates_from_search(result, query_title="Frankenstein")
    works = updates["work_choices"]
    assert {work["author"] for work in works} == {"David Sala", "George Bess"}
    assert updates["isbn"] is None
    state = DraftState()
    state.data = {"name": "Frankenstein", **updates}
    _, keyboard = build_creation_recap(state.data)
    labels = [b.text for row in keyboard.inline_keyboard for b in row
              if (b.callback_data or "").startswith("al_create_work:")]
    assert any("Sala" in label for label in labels)
    assert any("Bess" in label for label in labels)
    chosen_index = next(i for i, work in enumerate(works) if work["author"] == chosen_author)
    chosen = next(meta for meta in records if meta.isbn13 == works[chosen_index]["isbn13"])
    from app.services.book_metadata_service import BookResolution

    confirmed = result_from_resolution(BookResolution(best=chosen))
    callback = _callback(f"al_create_work:{chosen_index}")
    with (
        patch("app.bot.handlers.alerts_create_confirm.BookSearchUseCase") as search,
        patch("app.bot.handlers.alerts_create_confirm._resolve_edition_choices",
              new=AsyncMock(return_value=[])),
        patch("app.bot.handlers.alerts_create_confirm.relaunch_keyword_search"),
    ):
        search.return_value.search_by_isbn = AsyncMock(return_value=confirmed)
        await select_alert_creation_work(callback, state)
    assert state.data["isbn"] == chosen.isbn13
    assert state.data["author"] == chosen_author
    text = callback.message.edit_text.await_args.args[0]
    assert f"<b>Auteur :</b> {chosen_author}" in text
    assert f"<code>{chosen.isbn13}</code>" in text


@pytest.mark.asyncio
async def test_tome_selection_rejects_a_linked_book_from_a_neighboring_series():
    books = _series_books()
    neighbor = _create_mock_book("Absolute Master Universe — Tome 2", 9900)
    books.append(neighbor)
    message = MagicMock(spec=Message)
    message.chat = MagicMock(id=31)
    message.message_id = 41
    message.edit_text = AsyncMock()
    callback = MagicMock(message=message, answer=AsyncMock())
    panel = MarketPanelData(None, books[0], books, None, None,
                           alert=Alert(name="Absolute Master"))
    view = market_panel.PanelView(message, panel, listing_id=uuid.uuid4())
    with patch.object(market_panel, "_panels", {(31, 41): view}):
        await market_panel.select_panel_tome(callback, neighbor.id)
    message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.args == ("Tome introuvable.",)


@pytest.mark.asyncio
async def test_expired_panel_callback_cannot_open_a_book_outside_the_alert():
    from app.bot.handlers.callbacks import _encode_tome_ref, handle_market_tome_select
    from tests.unit.test_telegram_book_contract import _callback

    books = _series_books()
    listing_id = uuid.uuid4()
    foreign = _create_mock_book("Another series Tome 1", 5500)
    callback = _callback(_encode_tome_ref(listing_id, foreign.id))
    callback.message.chat = MagicMock(id=52)
    callback.message.message_id = 62
    listing = MagicMock(id=listing_id)
    alert = Alert(id=uuid.uuid4(), name="Absolute Master")
    with (
        patch.object(market_panel, "_panels", {}),
        patch("app.bot.handlers.callbacks.ListingRepository") as listings,
        patch("app.bot.handlers.callbacks.BookRepository") as repository,
        patch("app.bot.handlers.callbacks.MarketPriceRepository") as prices,
    ):
        listings.return_value.get_by_id = AsyncMock(return_value=listing)
        listings.return_value.get_alert_for_listing = AsyncMock(return_value=alert)
        repository.return_value.get_all_books_for_alert = AsyncMock(return_value=books)
        repository.return_value.get_by_id = AsyncMock(return_value=foreign)
        prices.return_value.get_latest_snapshot = AsyncMock(return_value=None)
        await handle_market_tome_select(callback, MagicMock())
    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.args == ("Tome introuvable.",)


def test_cal_header_escapes_the_retained_url_without_changing_it():
    book = _series_books()[0]
    url = 'https://www.chasse-aux-livres.fr/prix/9791026853794/book?x=1&y="2"'
    book.market_cache.source_history = {**book.market_cache.source_history, "source_url": url}
    card = MarketPriceService.format_market_details_card(None, book)
    assert 'book?x=1&amp;y=&quot;2&quot;">Prix du marché</a>' in card


def test_confirmed_edition_never_reuses_the_other_editions_offers():
    books = _series_books()
    snapshot = MagicMock(raw_payload={"partners": {"Wrong edition": {
        "total_cents": 9900, "condition": "Très bon", "is_used": True,
    }}})
    card = MarketPriceService.format_market_details_card(
        None, books[2], books, latest_snapshot=snapshot,
        preferred_isbns={2: books[1].isbn13},
    )
    assert "<code>9791026855682</code>" in card
    assert "Wrong edition" not in card
    assert "99,00 €" not in card


@pytest.mark.asyncio
@pytest.mark.parametrize("preselected", [False, True])
async def test_open_panel_never_starts_on_a_neighboring_series(preselected):
    from tests.unit.test_market_panel_lifecycle import _panel_callback

    books = _series_books()
    neighbor = _create_mock_book("Absolute Master Universe — Tome 1", 9900)
    neighbor.isbn13 = "9782369903086"
    books = [neighbor, books[1]]
    alert = Alert(id=uuid.uuid4(), name="Absolute Master")
    panel = MarketPanelData(None, neighbor if preselected else None, books, None, None, alert=alert)
    callback = _panel_callback()
    with patch.object(market_panel, "_panels", {}):
        await market_panel.open_panel(callback, panel)
    text = callback.message.reply.await_args.kwargs["text"]
    assert "Universe" not in text
    assert "<code>9782369903086</code>" not in text
    keyboard = callback.message.reply.await_args.kwargs["reply_markup"]
    assert [b.text for row in keyboard.inline_keyboard for b in row] == ["T2", "Fermer"]


def test_market_heading_falls_back_to_the_saved_cal_offer_url():
    book = _series_books()[0]
    url = "https://www.chasse-aux-livres.fr/prix/9791026853794/book"
    book.market_cache.best_source_url = url
    book.market_cache.source_history = {**book.market_cache.source_history, "source_url": None}
    card = MarketPriceService.format_market_details_card(None, book)
    assert f'<a href="{url}">Prix du marché</a>' in card


@pytest.mark.parametrize("reverse", [False, True])
def test_an_edition_without_isbn_is_not_an_implicit_preference(reverse):
    books = _series_books()
    books[2].isbn13 = None
    if reverse:
        books.reverse()
    lines = MarketPriceService._series_books_lines(books, None)
    assert lines == ["Tome 1 : 11,49 €", "Tome 2 : minimum annuel indisponible"]
    ambiguous = next(book for book in books if "Vol. 2" in book.title)
    card = MarketPriceService.format_market_details_card(None, ambiguous, books)
    assert "plusieurs éditions" in card
    assert "99,00 €" not in card


@pytest.mark.asyncio
async def test_ambiguous_tome_selection_never_reuses_the_initial_tomes_link_or_footer():
    from app.services.tome_extractor import TomeMatchResult

    books = _series_books()
    first_url = "https://www.chasse-aux-livres.fr/prix/9791026853794/first-tome"
    books[0].market_cache.source_history = {
        **books[0].market_cache.source_history, "source_url": first_url,
    }
    message = MagicMock(spec=Message)
    message.chat = MagicMock(id=83)
    message.message_id = 93
    message.edit_text = AsyncMock()
    callback = MagicMock(message=message, answer=AsyncMock())
    panel = MarketPanelData(None, books[0], books, TomeMatchResult(tome_number=1), None)
    view = market_panel.PanelView(message, panel, listing_id=uuid.uuid4(), selected_id=books[0].id)
    with patch.object(market_panel, "_panels", {(83, 93): view}):
        await market_panel.select_panel_tome(callback, books[1].id)
    text = message.edit_text.await_args.args[0]
    assert "Tome 2 : plusieurs éditions" in text
    assert first_url not in text
    assert "Dernière MAJ" not in text
    assert "<b>ISBN :</b>" not in text

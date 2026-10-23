import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.bot.handlers.alerts_create import handle_category_selected, start_add_alert
from app.bot.handlers.alerts_create_confirm import (
    edit_alert_creation_prompt,
    handle_create_edit_choice,
)
from app.bot.states.alert_creation import AlertCreationState
from app.db.models.user import User
from app.domain.enums import BookSearchStatus
from app.services.book_metadata_service import BookResolution
from app.use_cases.book_search import BookSearchResult, BookWorkChoice
from tests.add_fakes import BNF_EDITION, DraftState, fake_bnf, fake_deepseek
from tests.add_journey import drain, patched, repositories
from tests.screens import confirm_press, message_with_recap, work_buttons


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("price", ["", " 12€"])
async def test_catalogue_fallback_opens_recap_without_manual_category_or_price(enabled, price):
    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses par Martin Panchaud" + price)
    repos = repositories(BNF_EDITION.isbn13)
    bnf = fake_bnf(BookResolution(best=BNF_EDITION))
    with ExitStack() as stack:
        for boundary in patched(repos, bnf, fake_deepseek({}), natural_enabled=enabled):
            stack.enter_context(boundary)
        parser = stack.enter_context(
            patch(
                "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
                AsyncMock(return_value=None),
            )
        )
        await start_add_alert(message, state)
        await drain(repos["tasks"])
    assert state.state == AlertCreationState.confirm_creation
    assert state.data["isbn"] == BNF_EDITION.isbn13
    assert state.data["author"] == BNF_EDITION.author
    assert state.data.get("max_item_price_cents") == (1200 if price else None)
    bnf.resolve_for_alert.assert_awaited_once_with("La couleur des choses", "Martin Panchaud")
    assert parser.await_count == int(enabled)
    text = screen.edit_text.call_args[0][0]
    assert "Indique ton prix" not in text
    assert "Choisis la catégorie" not in text
    assert ("12,00 €" if price else "Non renseigné") in text
    buttons = screen.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert any(b.callback_data.startswith("alert_create_confirm") for row in buttons for b in row)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [BookSearchStatus.NOT_FOUND, BookSearchStatus.UNAVAILABLE])
async def test_catalogue_without_candidate_keeps_category_choice(status):
    state = DraftState()
    message, screen = message_with_recap("/add Raspberry Pi 4")
    repos = repositories(BNF_EDITION.isbn13)
    with ExitStack() as stack:
        for boundary in patched(repos, fake_bnf(None), fake_deepseek({}), natural_enabled=False):
            stack.enter_context(boundary)
        search = stack.enter_context(patch("app.bot.handlers.alerts_helpers.BookSearchUseCase"))
        search.return_value.search_by_title = AsyncMock(
            return_value=BookSearchResult(status=status)
        )
        await start_add_alert(message, state)
    search.return_value.search_by_title.assert_awaited_once_with(
        "Raspberry Pi 4",
        None,
        include_series_catalogue=True,
    )
    message.answer.assert_awaited_once()
    assert "Choisis la catégorie" in screen.edit_text.call_args[0][0]
    assert state.state == AlertCreationState.waiting_for_category
    assert not state.data.get("category_slug")
    assert not state.data.get("isbn")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [BookSearchStatus.AMBIGUOUS, BookSearchStatus.PARTIAL])
async def test_catalogue_candidates_keep_unconfirmed_identity_and_author_choices(status):
    state = DraftState()
    message, screen = message_with_recap("/add Frankenstein")
    repos = repositories(BNF_EDITION.isbn13)
    works = (
        BookWorkChoice(title="Frankenstein", author="David Sala", isbn13="9782203221819"),
        BookWorkChoice(title="Frankenstein", author="George Bess", isbn13="9782344039229"),
    )
    with ExitStack() as stack:
        for boundary in patched(repos, fake_bnf(None), fake_deepseek({}), natural_enabled=False):
            stack.enter_context(boundary)
        search = stack.enter_context(patch("app.bot.handlers.alerts_helpers.BookSearchUseCase"))
        search.return_value.search_by_title = AsyncMock(
            return_value=BookSearchResult(status=status, works=works)
        )
        await start_add_alert(message, state)
    assert state.state == AlertCreationState.confirm_creation
    assert not state.data.get("isbn")
    assert not state.data.get("book_context")
    assert [work["author"] for work in state.data["work_choices"]] == ["David Sala", "George Bess"]
    if status == BookSearchStatus.AMBIGUOUS:
        buttons = work_buttons(screen.edit_text.call_args.kwargs["reply_markup"])
        assert any("Sala" in button.text for button in buttons)
        assert any("Bess" in button.text for button in buttons)


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Steam Deck", "Steam Deck 100"])
async def test_category_can_be_changed_from_recap_without_retaining_book_identity(title):
    state = DraftState()
    await state.update_data(
        name=title,
        category_slug="bd_comics",
        max_item_price_cents=12000,
        isbn=BNF_EDITION.isbn13,
        author=BNF_EDITION.author,
        is_series=True,
        book_context={"title": BNF_EDITION.title},
        series_volumes=[{"isbn13": BNF_EDITION.isbn13}],
        series_total=1,
        edition_alternatives=[{"isbn13": BNF_EDITION.isbn13}],
        work_choices=[{"title": BNF_EDITION.title}],
        keyword_note="Ancienne note",
    )
    await state.set_state(AlertCreationState.confirm_creation)
    callback, screen = confirm_press()
    await edit_alert_creation_prompt(callback, state)
    keyboard = screen.edit_text.call_args.kwargs["reply_markup"]
    assert any(
        b.callback_data == "al_create_edit:category"
        for row in keyboard.inline_keyboard
        for b in row
    )
    callback.data = "al_create_edit:category"
    await handle_create_edit_choice(callback, state)
    assert state.state == AlertCreationState.waiting_for_category
    callback.data = "add_cat:gaming"
    repos = repositories(BNF_EDITION.isbn13)
    with ExitStack() as stack:
        for boundary in patched(repos, fake_bnf(None), fake_deepseek({})):
            stack.enter_context(boundary)
        await handle_category_selected(
            callback, state, None, User(id=uuid.uuid4(), telegram_user_id=42)
        )
    assert state.state == AlertCreationState.confirm_creation
    assert state.data["category_slug"] == "gaming"
    assert state.data["max_item_price_cents"] == 12000
    assert state.data["name"] == title
    for field in (
        "isbn",
        "author",
        "book_context",
        "series_volumes",
        "edition_alternatives",
        "work_choices",
        "keyword_note",
        "is_series",
    ):
        assert not state.data.get(field), field


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [None, "17,50"])
async def test_optional_price_is_editable_and_persisted_without_an_imposed_default(price):
    from app.bot.handlers.alerts_create import save_alert
    from app.bot.handlers.alerts_create_confirm import process_creation_edit_input
    from tests.screens import incoming_message

    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    repos = repositories(BNF_EDITION.isbn13)
    user = User(id=uuid.uuid4(), telegram_user_id=42)
    session = AsyncMock()
    with ExitStack() as stack:
        for boundary in patched(
            repos,
            fake_bnf(BookResolution(best=BNF_EDITION)),
            fake_deepseek({}),
            natural_enabled=False,
        ):
            stack.enter_context(boundary)
        await start_add_alert(message, state, session, user)
        callback, screen = confirm_press()
        if price is not None:
            callback.data = "al_create_edit:price"
            await handle_create_edit_choice(callback, state)
            assert state.state == AlertCreationState.waiting_for_edit_field
            await process_creation_edit_input(incoming_message(price), state)
            assert state.state == AlertCreationState.confirm_creation
        callback.data = "alert_create_confirm"
        await save_alert(callback, state, user, session)
        await drain(repos["tasks"])
    values = repos["alert_repo"].create.call_args.kwargs
    assert values["isbn"] == BNF_EDITION.isbn13
    assert values["max_item_price_cents"] == (1750 if price is not None else None)
    assert "Alerte créée" in screen.edit_text.call_args[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["parser", "catalogue"])
@pytest.mark.parametrize("replaced", [False, True])
async def test_late_fallback_cannot_restore_cancelled_or_replaced_draft(stage, replaced):
    state = DraftState()
    message, screen = message_with_recap("/add Frankenstein")
    repos = repositories(BNF_EDITION.isbn13)

    async def invalidate(*_args, **_kwargs):
        await state.clear()
        if replaced:
            await state.update_data(creation_token="new", name="Nouvelle demande")
            await state.set_state(AlertCreationState.waiting_for_query)
        return None if stage == "parser" else BookSearchResult(status=BookSearchStatus.NOT_FOUND)

    with ExitStack() as stack:
        for boundary in patched(repos, fake_bnf(None), fake_deepseek({})):
            stack.enter_context(boundary)
        parser = stack.enter_context(
            patch(
                "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
                AsyncMock(side_effect=invalidate if stage == "parser" else None, return_value=None),
            )
        )
        search = stack.enter_context(patch("app.bot.handlers.alerts_helpers.BookSearchUseCase"))
        search.return_value.search_by_title = AsyncMock(side_effect=invalidate)
        await start_add_alert(message, state)
    parser.assert_awaited_once()
    assert state.data == ({"creation_token": "new", "name": "Nouvelle demande"} if replaced else {})
    assert await state.get_state() == (
        AlertCreationState.waiting_for_query.state if replaced else None
    )
    screen.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_choosing_current_category_preserves_identity_and_manual_criteria():
    state = DraftState()
    await state.update_data(
        name="Titre",
        category_slug="bd_comics",
        isbn=BNF_EDITION.isbn13,
        include_keywords=["personnalisé"],
        max_item_price_cents=1600,
    )
    await state.set_state(AlertCreationState.confirm_creation)
    before = await state.get_data()
    callback, screen = confirm_press()
    callback.data = "al_create_edit:category"
    await handle_create_edit_choice(callback, state)
    callback.data = "add_cat:bd_comics"
    with patch("app.bot.handlers.alerts_create.process_bd_query_lookup", AsyncMock()) as lookup:
        await handle_category_selected(callback, state, None, None)
    lookup.assert_not_awaited()
    assert state.state == AlertCreationState.confirm_creation
    assert {key: state.data[key] for key in before} == before


@pytest.mark.asyncio
async def test_opening_category_editor_invalidates_an_in_flight_book_lookup():
    from app.bot.handlers.alerts_helpers import process_bd_query_lookup

    state = DraftState()
    await state.update_data(name="Frankenstein", category_slug="bd_comics")
    message, lookup_screen = message_with_recap("Frankenstein")
    callback, category_screen = confirm_press()
    callback.data = "al_create_edit:category"

    async def choose_category(*_args, **_kwargs):
        await handle_create_edit_choice(callback, state)
        return BookSearchResult(status=BookSearchStatus.NOT_FOUND)

    repos = repositories(BNF_EDITION.isbn13)
    with (
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=repos["category_repo"]),
        patch("app.bot.handlers.alerts_helpers.BookSearchUseCase") as search,
    ):
        search.return_value.search_by_title = AsyncMock(side_effect=choose_category)
        await process_bd_query_lookup(message, state, "Frankenstein", None)
    assert state.state == AlertCreationState.waiting_for_category
    assert "Choisis la catégorie" in category_screen.edit_text.call_args[0][0]
    lookup_screen.edit_text.assert_not_awaited()

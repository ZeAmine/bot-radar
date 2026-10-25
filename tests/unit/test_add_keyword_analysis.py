import asyncio
import uuid
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from app.bot.handlers.alerts_create import save_alert, start_add_alert
from app.bot.handlers.alerts_create_confirm import (
    handle_create_edit_choice,
    process_creation_edit_input,
)
from app.bot.states.alert_creation import AlertCreationState
from app.db.models.user import User
from app.services.ai.modules.keyword_variants import KeywordVariantsModule, KeywordVariantsOutcome
from app.services.ai.schemas import BookContext
from app.services.book_metadata_service import BookResolution
from tests.add_fakes import BNF_EDITION, DraftState, fake_bnf, fake_llm, llm_payload
from tests.add_journey import drain, patched, repositories
from tests.screens import confirm_press, incoming_message, message_with_recap


@contextmanager
def keyword_journey(*, natural=True, enabled=True, configured=True, book=None):
    repos = repositories(BNF_EDITION.isbn13)
    with ExitStack() as stack:
        for boundary in patched(
            repos,
            fake_bnf(BookResolution(best=book or BNF_EDITION)),
            fake_llm(llm_payload()),
            natural_enabled=natural,
        ):
            stack.enter_context(boundary)
        stack.enter_context(
            patch.object(
                KeywordVariantsModule,
                "is_enabled",
                new_callable=PropertyMock,
                return_value=enabled,
            )
        )
        stack.enter_context(
            patch(
                "app.bot.handlers.alerts_create.keyword_variants_module.client",
                MagicMock(is_configured=configured),
            )
        )
        try:
            yield repos
        finally:
            for coro, _name in repos["tasks"]:
                coro.close()
            repos["tasks"].clear()


def buttons(screen):
    return [
        button.callback_data.split("@")[0]
        for row in screen.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("natural", [True, False])
async def test_recap_precedes_slow_analysis_and_creation_waits(natural):
    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses par Martin Panchaud")
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return KeywordVariantsOutcome(
            variants=["la couleur des chsoes"],
            exclusions=["keeping two"],
            answered=True,
        )

    with (
        keyword_journey(natural=natural) as repos,
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module.generate_variants_and_exclusions",
            AsyncMock(side_effect=generate),
        ),
    ):
        await start_add_alert(message, state)
        text = screen.edit_text.call_args.args[0]
        assert "<b>Prix maximum :</b>" in text
        assert BNF_EDITION.title in text
        assert "alert_create_confirm" not in buttons(screen)
        assert "alert_create_edit" in buttons(screen)
        assert "alert_create_cancel" in buttons(screen)
        assert "alert_create_ai_variants" not in buttons(screen)
        assert len(repos["tasks"]) == 1
        task = asyncio.create_task(drain(repos["tasks"]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            callback, _ = confirm_press()
            await save_alert(
                callback, state, User(id=uuid.uuid4(), telegram_user_id=42), AsyncMock()
            )
            repos["alert_repo"].create.assert_not_awaited()
            assert state.data.get("creation_outcome") is None
        finally:
            release.set()
            await task
    assert state.data["variants"] == ["la couleur des chsoes"]
    assert "keeping two" in state.data["exclude_keywords"]
    assert "alert_create_confirm" in buttons(screen)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("price", "17,50", 1750),
        ("variants", "couleur des chozes", ["couleur des chozes"]),
        ("exclude_keywords", "/skip", []),
    ],
)
async def test_manual_edits_survive_analysis_without_restart(field, value, expected):
    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return KeywordVariantsOutcome(
            variants=["la couleur des chsoes"],
            exclusions=["keeping two"],
            answered=True,
        )

    with (
        keyword_journey() as repos,
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module.generate_variants_and_exclusions",
            AsyncMock(side_effect=generate),
        ) as generate_mock,
    ):
        await start_add_alert(message, state)
        task = asyncio.create_task(drain(repos["tasks"]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            callback, edit_screen = confirm_press()
            callback.data = f"al_create_edit:{field}"
            await handle_create_edit_choice(callback, state)
            await process_creation_edit_input(incoming_message(value), state)
        finally:
            release.set()
            await task
        generate_mock.assert_awaited_once()
    key = "max_item_price_cents" if field == "price" else field
    assert state.data[key] == expected
    assert state.data["keyword_analysis_status"] == "completed"
    assert state.state == AlertCreationState.confirm_creation


@pytest.mark.asyncio
async def test_ai_result_does_not_replace_a_field_prompt():
    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    with keyword_journey() as repos:
        await start_add_alert(message, state)
        callback, _ = confirm_press()
        callback.data = "al_create_edit:price"
        await handle_create_edit_choice(callback, state)
        before = screen.edit_text.await_count
        await drain(repos["tasks"])
    assert state.state == AlertCreationState.waiting_for_edit_field
    assert screen.edit_text.await_count == before
    assert state.data["keyword_analysis_status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "configured"), [(False, True), (True, False)])
async def test_unavailable_analysis_never_blocks_creation_or_offers_retry(enabled, configured):
    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    with keyword_journey(enabled=enabled, configured=configured) as repos:
        await start_add_alert(message, state)
        assert not repos["tasks"]
    assert "alert_create_confirm" in buttons(screen)
    assert "alert_create_ai_variants" not in buttons(screen)
    assert state.data["keyword_analysis_status"] == "unavailable"


def test_actual_prompt_contains_language_and_volume_without_price():
    context = BookContext(
        title="Absolute Martian Manhunter",
        author="Deniz Camp",
        volume_number=2,
        language="fr",
        isbn13="9791026855682",
    )
    prompt = KeywordVariantsModule._build_user_prompt(context.title, "bd_comics", context)
    assert "langue : fr" in prompt
    assert "tome : 2" in prompt
    assert "9791026855682" in prompt


@pytest.mark.asyncio
async def test_failed_analysis_allows_one_explicit_retry():
    from app.bot.handlers.alerts_create import handle_create_ai_variants

    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    with keyword_journey() as repos:
        with patch(
            "app.bot.handlers.alerts_create.keyword_variants_module.generate_variants_and_exclusions",
            AsyncMock(return_value=KeywordVariantsOutcome.unanswered()),
        ):
            await start_add_alert(message, state)
            await drain(repos["tasks"])
        assert "alert_create_confirm" in buttons(screen)
        assert "alert_create_ai_variants" in buttons(screen)
        callback, retry_screen = confirm_press()
        callback.data = "alert_create_ai_variants"
        await asyncio.wait_for(handle_create_ai_variants(callback, state), 1)
        assert state.data["keyword_analysis_status"] == "pending"
        assert len(repos["tasks"]) == 1
        await handle_create_ai_variants(callback, state)
        assert len(repos["tasks"]) == 1
        await drain(repos["tasks"])
    assert state.data["keyword_analysis_status"] == "completed"
    assert "alert_create_confirm" in buttons(retry_screen)
    assert "alert_create_ai_variants" not in buttons(retry_screen)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "success"),
    [
        ({"variants": [], "exclusions": []}, True),
        ({"variants": ["La couleur des choses", "galaxy s24"], "exclusions": ["1", "x"]}, True),
        ({"variants": 17, "exclusions": {}}, False),
        (None, False),
    ],
)
async def test_raw_answer_is_validated_filtered_and_reported_as_retained_values(raw, success):
    from app.bot.handlers.alerts_create import keyword_variants_module

    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    with keyword_journey() as repos:
        generate = KeywordVariantsModule.generate_variants_and_exclusions.__get__(
            keyword_variants_module
        )
        with (
            patch.object(keyword_variants_module, "generate_variants_and_exclusions", generate),
            patch.object(
                keyword_variants_module.client, "generate_json", AsyncMock(return_value=raw)
            ) as request,
        ):
            await start_add_alert(message, state)
            before = dict(state.data)
            await drain(repos["tasks"])
            request.assert_awaited_once()
    assert state.data["variants"] == before["variants"]
    assert state.data["exclude_keywords"] == before["exclude_keywords"]
    assert state.data["keyword_analysis_status"] == ("completed" if success else "failed")
    text = screen.edit_text.call_args.args[0]
    assert ("Aucune proposition supplémentaire pertinente retenue." in text) == success
    assert "alert_create_confirm" in buttons(screen)
    assert ("alert_create_ai_variants" in buttons(screen)) != success


@pytest.mark.asyncio
async def test_exception_releases_creation_without_losing_existing_criteria():
    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    with (
        keyword_journey() as repos,
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module.generate_variants_and_exclusions",
            AsyncMock(side_effect=RuntimeError("simulée")),
        ),
    ):
        await start_add_alert(message, state)
        before = dict(state.data)
        await drain(repos["tasks"])
    assert state.data["keyword_analysis_status"] == "failed"
    assert state.data["variants"] == before["variants"]
    assert state.data["exclude_keywords"] == before["exclude_keywords"]
    assert "alert_create_confirm" in buttons(screen)


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
async def test_late_generation_does_not_restore_cancelled_or_replaced_draft(replace):
    from app.bot.handlers.alerts_create_confirm import handle_cancel_command

    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return KeywordVariantsOutcome(variants=["la couleur des chsoes"], answered=True)

    with (
        keyword_journey() as repos,
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module.generate_variants_and_exclusions",
            AsyncMock(side_effect=generate),
        ),
    ):
        await start_add_alert(message, state)
        task = asyncio.create_task(drain(repos["tasks"]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await handle_cancel_command(incoming_message("/cancel"), state)
            if replace:
                await state.update_data(name="Autre article", creation_token="new")
            expected = dict(state.data)
            edits = screen.edit_text.await_count
        finally:
            release.set()
            await task
    assert state.data == expected
    assert screen.edit_text.await_count == edits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "query"), [("bd_comics", "La couleur des choses"), ("tech", "Steam Deck 512Go")]
)
async def test_category_then_query_also_schedules_analysis(category, query):
    from app.bot.handlers.alerts_create import handle_category_selected, handle_query_input

    state = DraftState()
    message, _ = message_with_recap("/add")
    with keyword_journey(natural=False) as repos:
        await start_add_alert(message, state)
        callback, _ = confirm_press()
        callback.data = f"add_cat:{category}@{state.data['creation_token']}"
        user = User(id=uuid.uuid4(), telegram_user_id=42)
        await handle_category_selected(callback, state, AsyncMock(), user)
        await handle_query_input(incoming_message(query), state, AsyncMock(), user)
        assert state.data["keyword_analysis_status"] == "pending"
        assert len(repos["tasks"]) == 1
        await drain(repos["tasks"])
    assert state.data["keyword_analysis_status"] == "completed"


@pytest.mark.asyncio
async def test_homonyms_wait_for_choice_before_any_analysis():
    from app.bot.handlers.alerts_create_confirm import select_alert_creation_work
    from app.domain.enums import BookSearchStatus
    from app.use_cases.book_search import BookSearchResult, BookWorkChoice

    state = DraftState()
    message, screen = message_with_recap("/add Frankenstein")
    result = BookSearchResult(
        status=BookSearchStatus.AMBIGUOUS,
        works=(
            BookWorkChoice(title="Frankenstein", author="David Sala", isbn13="9782203221819"),
            BookWorkChoice(title="Frankenstein", author="George Bess", isbn13="9782344039229"),
        ),
    )
    with keyword_journey(natural=False) as repos:
        with patch("app.bot.handlers.alerts_helpers.BookSearchUseCase") as search:
            search.return_value.search_by_title = AsyncMock(return_value=result)
            await start_add_alert(message, state)
        assert not repos["tasks"]
        assert "alert_create_confirm" not in buttons(screen)
        callback, _ = confirm_press()
        callback.data = "al_create_work:0"
        from app.services.book_metadata_service import BibliographicReport, BookMetadata
        from app.use_cases.book_search import result_from_report

        selected = result_from_report(
            BibliographicReport(
                kind="title",
                resolution=BookResolution(
                    best=BookMetadata(
                        title="Frankenstein",
                        author="David Sala",
                        isbn13="9782203221819",
                    )
                ),
            ),
            query_title="Frankenstein",
        )
        with patch("app.bot.handlers.alerts_create_confirm.BookSearchUseCase") as confirmation:
            confirmation.return_value.search_by_isbn = AsyncMock(return_value=selected)
            await select_alert_creation_work(callback, state)
        assert len(repos["tasks"]) == 1
        await drain(repos["tasks"])
    assert state.data["keyword_analysis_status"] == "completed"


@pytest.mark.asyncio
async def test_result_edits_the_latest_recap_after_a_price_edit():
    state = DraftState()
    message, initial = message_with_recap("/add La couleur des choses")
    message.bot.edit_message_text = AsyncMock()
    initial.message_id, initial.chat.id = 201, 42
    price_message = incoming_message("17,50")
    price_message.answer.return_value.message_id = 202
    price_message.answer.return_value.chat = MagicMock(id=42)
    with keyword_journey() as repos:
        await start_add_alert(message, state)
        callback, _ = confirm_press()
        callback.data = "al_create_edit:price"
        await handle_create_edit_choice(callback, state)
        await process_creation_edit_input(price_message, state)
        await drain(repos["tasks"])
    call = message.bot.edit_message_text.call_args.kwargs
    assert call["chat_id"] == 42
    assert call["message_id"] == 202
    assert "17,50 €" in call["text"]
    assert "couleurdeschoses" in call["text"]
    assert "Analyse des variantes et exclusions en cours" not in call["text"]


@pytest.mark.asyncio
async def test_identity_change_restarts_analysis_and_discards_the_old_result():
    state = DraftState()
    message, _ = message_with_recap("/add La couleur des choses")
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return KeywordVariantsOutcome(exclusions=["ancienne confusion"], answered=True)

    with keyword_journey() as repos:
        with patch(
            "app.bot.handlers.alerts_create.keyword_variants_module.generate_variants_and_exclusions",
            AsyncMock(side_effect=generate),
        ):
            await start_add_alert(message, state)
            old_token = state.data["keyword_analysis_token"]
            coro, _ = repos["tasks"].pop()
            task = asyncio.create_task(coro)
            try:
                await asyncio.wait_for(entered.wait(), 1)
                callback, _ = confirm_press()
                callback.data = "al_create_edit:name"
                await handle_create_edit_choice(callback, state)
                await process_creation_edit_input(incoming_message("Blacksad"), state)
                assert state.data["keyword_analysis_token"] != old_token
            finally:
                release.set()
                await task
        assert state.data["keyword_analysis_status"] == "pending"
        assert "ancienne confusion" not in state.data["exclude_keywords"]
        await drain(repos["tasks"])
    assert state.data["name"] == "Blacksad"
    assert state.data["keyword_analysis_status"] == "completed"


@pytest.mark.asyncio
async def test_final_recap_criteria_are_the_values_passed_to_persistence():
    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    with keyword_journey() as repos:
        await start_add_alert(message, state)
        await drain(repos["tasks"])
        draft = dict(state.data)
        recap = screen.edit_text.call_args.args[0]
        callback, final_screen = confirm_press()
        await save_alert(callback, state, User(id=uuid.uuid4(), telegram_user_id=42), AsyncMock())
        await drain(repos["tasks"])
    values = repos["alert_repo"].create.call_args.kwargs
    for field in (
        "name",
        "isbn",
        "variants",
        "exclude_keywords",
        "include_keywords",
        "max_item_price_cents",
    ):
        assert values[field] == draft.get(field)
    for value in values["variants"] + values["exclude_keywords"]:
        assert value in recap
        assert value in final_screen.edit_text.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("La couleur des chsoes", True),
        ("La couleur des choses", True),
        ("La couleur des choses étude critique", False),
        ("Absolute Martian Manhunter tome 3", False),
    ],
)
async def test_retained_keywords_are_replayed_in_the_existing_matcher(title, expected):
    from app.core.constants import AlertStatus
    from app.db.models.alert import Alert
    from app.db.models.category import Category
    from app.domain.rules.listing_match import evaluate_listing_against_alert
    from app.utils.text import normalize_text

    numbered = title.startswith("Absolute")
    canonical = "Absolute Martian Manhunter tome 2" if numbered else "La couleur des choses"
    state = DraftState()
    message, _ = message_with_recap("/add " + canonical)
    from app.services.book_metadata_service import BookMetadata

    book = BookMetadata(
        title=canonical,
        isbn13="9791026855682" if numbered else BNF_EDITION.isbn13,
    )
    with (
        keyword_journey(natural=False, book=book) as repos,
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module.generate_variants_and_exclusions",
            AsyncMock(
                return_value=KeywordVariantsOutcome(
                    variants=(
                        ["absolute martian manhunter tome 3"]
                        if numbered
                        else ["la couleur des chsoes"]
                    ),
                    exclusions=["étude critique"],
                    answered=True,
                )
            ),
        ),
    ):
        await start_add_alert(message, state)
        await drain(repos["tasks"])
    alert = Alert(
        name=canonical,
        category=Category(slug="bd_comics", label="Livres", emoji="📚"),
        status=AlertStatus.ACTIVE,
        isbn=None,
        include_keywords=[canonical],
        variants=state.data["variants"],
        exclude_keywords=state.data["exclude_keywords"],
        max_item_price_cents=None,
        accepted_conditions=["Très bon état"],
    )
    result = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title=normalize_text(title),
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1000,
    )
    assert result.is_match is expected
    if "étude critique" in title:
        assert result.reason_code == "user_exclusion"
    if numbered:
        assert state.data["variants"] == []


@pytest.mark.asyncio
async def test_initial_edit_failure_keeps_the_existing_fallback_and_completes_analysis():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    screen.edit_text.side_effect = TelegramBadRequest(
        method=EditMessageText(chat_id=42, message_id=201, text="récapitulatif"),
        message="message cannot be edited",
    )
    fallback = MagicMock(message_id=202)
    fallback.chat.id = 42
    message.answer.side_effect = [screen, fallback]
    message.bot.edit_message_text = AsyncMock()
    with keyword_journey() as repos:
        await start_add_alert(message, state)
        assert len(repos["tasks"]) == 1
        await drain(repos["tasks"])
    assert state.data["keyword_analysis_status"] == "completed"
    assert message.bot.edit_message_text.call_args.kwargs["message_id"] == 202


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("variants", "galaxy s24, couleur des chozes", ["couleur des chozes"]),
        ("exclude_keywords", "couleur, keeping two", ["keeping two"]),
    ],
)
async def test_manual_criteria_display_the_same_filtered_values_that_are_saved(
    field, raw, expected
):
    state = DraftState()
    message, _ = message_with_recap("/add La couleur des choses")
    with keyword_journey() as repos:
        await start_add_alert(message, state)
        await drain(repos["tasks"])
        callback, _ = confirm_press()
        callback.data = "al_create_edit:" + field
        await handle_create_edit_choice(callback, state)
        edit = incoming_message(raw)
        await process_creation_edit_input(edit, state)
        assert state.data[field] == expected
        text = edit.answer.call_args.args[0]
        assert ", ".join(expected) in text
        confirm, _ = confirm_press()
        await save_alert(confirm, state, User(id=uuid.uuid4(), telegram_user_id=42), AsyncMock())
        await drain(repos["tasks"])
    assert repos["alert_repo"].create.call_args.kwargs[field] == expected


@pytest.mark.asyncio
async def test_return_to_current_category_keeps_the_recap_live_until_analysis_finishes():
    from app.bot.handlers.alerts_create import handle_category_selected

    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    with keyword_journey() as repos:
        await start_add_alert(message, state)
        callback, _ = confirm_press()
        callback.message = screen
        callback.data = "al_create_edit:category"
        await handle_create_edit_choice(callback, state)
        callback.data = "add_cat:bd_comics@" + state.data["creation_token"]
        await handle_category_selected(callback, state, None, None)
        assert len(repos["tasks"]) == 1
        await drain(repos["tasks"])
    assert state.data["keyword_analysis_status"] == "completed"
    assert "alert_create_confirm" in buttons(screen)
    assert state.data["keyword_recap_visible"] is True


@pytest.mark.asyncio
async def test_reentering_the_same_title_preserves_the_running_generation():
    state = DraftState()
    message, screen = message_with_recap("/add La couleur des choses")
    with keyword_journey() as repos:
        await start_add_alert(message, state)
        generation = state.data["keyword_analysis_token"]
        callback, _ = confirm_press()
        callback.data = "al_create_edit:name"
        await handle_create_edit_choice(callback, state)
        await process_creation_edit_input(incoming_message(state.data["name"]), state)
        assert state.data["keyword_analysis_token"] == generation
        assert state.data["keyword_analysis_status"] == "pending"
        assert len(repos["tasks"]) == 1
        await drain(repos["tasks"])
    assert state.data["keyword_analysis_status"] == "completed"
    assert state.data["variants"] == ["couleurdeschoses"]

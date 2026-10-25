"""Flux de création et d'ajout d'alertes (/add, /addisbn, /addurl)."""

import asyncio
import contextlib
import html
import logging
import urllib.parse
import uuid
from typing import Any

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.creation_drafts import CreationAttempt, draft_guard
from app.bot.handlers._alerts_router import (
    STALE_CREATION_MESSAGE,
    UNKNOWN_CREATION_MESSAGE,
    bind_creation_keyboard,
    creation_callback_matches,
    router,
)
from app.bot.handlers.alerts_helpers import (
    ALERT_CREATED_TITLE,
    _bump_search_token,
    build_creation_recap,
    creation_card,
    creation_volume_issue,
    creation_work_choice_required,
    draft_updates_from_search,
    extract_inline_price_and_clean_query,
    keyword_note,
    populate_creation_prices,
    process_bd_query_lookup,
    process_tech_or_clothes_query,
    refresh_creation_series,
    render_existing_alert_response,
)
from app.bot.keyboards.alerts import get_category_selection_keyboard
from app.bot.labels import CRITERIA_ENRICHED_NOTE, escape_display, format_price_cents
from app.bot.states.alert_creation import AlertCreationState
from app.core.constants import (
    DEFAULT_BD_CONDITIONS,
    DEFAULT_CLOTHES_CONDITIONS,
    DEFAULT_GAMING_CONDITIONS,
    DEFAULT_TECH_CONDITIONS,
    AlertStatus,
    CategorySlug,
    MatchConfidence,
)
from app.core.tasks import safe_create_task
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.category_repo import CategoryRepository
from app.domain.entities.alert_preparation import PreparationRequest
from app.domain.enums import BookSearchStatus
from app.domain.rules.keyword_hygiene import sanitize_exclusions, sanitize_variants
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome, keyword_variants_module
from app.services.ai.modules.natural_add import natural_add_module
from app.services.ai.schemas import BookContext
from app.services.alert_service import (
    AlertServiceError,
    parse_price_to_cents,
)
from app.services.market_price_service import MarketPriceService
from app.use_cases.book_search import BookSearchResult
from app.use_cases.prepare_bd_alert import prepare_bd_alert
from app.utils.isbn import extract_clean_isbn
from app.utils.text import normalize_text, strip_html_tags

logger = logging.getLogger(__name__)

_CONFIRMATION_FOOTER = "<i>Tu seras prévenu des annonces correspondantes.</i>"


def _get_alert_repo(session: Any) -> Any:
    import unittest.mock

    import app.bot.handlers.alerts as alerts_mod

    if isinstance(AlertRepository, unittest.mock.Base):
        return AlertRepository(session)
    return getattr(alerts_mod, "AlertRepository", AlertRepository)(session)


def _merge_keywords(*groups: list[str] | None) -> list[str]:
    """Fusionne des listes de mots-clés en préservant l'ordre et en dédupliquant."""
    merged: list[str] = []
    for group in groups:
        for term in group or []:
            value = str(term).strip()
            if not value or any(value.lower() == kept.lower() for kept in merged):
                continue
            merged.append(value)
    return merged


def _variants_for_category(
    values: list[str] | None,
    name: str,
    includes: list[str] | None,
) -> list[str]:
    """Variantes retenues par le nettoyeur, jugées sur l'intention de recherche entière.

    Les références sont le nom **et** les mots-clés inclus : une variante peut être
    l'écriture d'une inclusion qualifiée (« blacksad integral » pour « blacksad
    intégrale »), ou un alias de marque (« geforce rtx 4070 » pour « NVIDIA RTX 4070 »),
    sans avoir le titre seul comme orthographe exacte.

    La catégorie ne filtre rien ici. Le mécanisme de variante n'est pas propre aux
    livres : le parcours d'ingestion le tient pour acquis sur une alerte tech, et c'est
    ce parcours qui le prouve de bout en bout. Le garde d'identité, lui, s'applique
    partout — c'est lui qui écarte le produit voisin.
    """
    return sanitize_variants(values, name, references=includes or ())


async def _finalize_loading_message(
    loading_msg: Message | bool,
    event: Message | CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> Any:
    """Édite le message d'attente si possible, sinon envoie une nouvelle réponse.

    Reste tolérant aux messages non éditables (réponse de callback renvoyant un bool).
    """
    try:
        edit_text = getattr(loading_msg, "edit_text", None)
        if callable(edit_text):
            await edit_text(text, reply_markup=keyboard)
            return loading_msg
    except TelegramBadRequest as exc:
        if "message is not modified" in exc.message.lower():
            return loading_msg
    except Exception:
        pass

    target: Any = event.message if isinstance(event, CallbackQuery) else event
    if target is None:
        return None
    try:
        return await target.answer(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        logger.warning("Réponse Telegram refusée (%s), renvoi du récap en texte brut.", exc)
        return await target.answer(strip_html_tags(text), reply_markup=keyboard, parse_mode=None)


def _draft_matches(current: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """Compare l'identité de l'article et la génération de son analyse."""
    return bool(current) and not current.get("creation_outcome") and all(
        current.get(key) == snapshot.get(key)
        for key in (
            "name", "category_slug", "isbn", "author", "publisher", "language",
            "requested_volume", "is_series", "book_context", "creation_token",
            "keyword_analysis_token",
        )
    )


def _creation_message_values(message: Any) -> dict[str, Any]:
    message_id = getattr(message, "message_id", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if isinstance(message_id, int) and isinstance(chat_id, int):
        return {"keyword_message_id": message_id, "keyword_chat_id": chat_id}
    return {}


async def show_creation_recap(
    *, event: Message | CallbackQuery, state: FSMContext, target: Any = None,
    expected_token: str | None = None,
) -> None:
    async with draft_guard(state):
        current = await state.get_data()
        if (not current or current.get("creation_outcome")
                or current.get("creation_token") != expected_token
                or await state.get_state() != AlertCreationState.confirm_creation.state):
            return
        recap, keyboard = build_creation_recap(current)
        if target is not None and callable(getattr(target, "edit_text", None)):
            sent = await _finalize_loading_message(target, event, recap, keyboard)
        else:
            sent = await event.answer(recap, reply_markup=keyboard)
        await state.update_data(keyword_recap_visible=True, **_creation_message_values(sent))


async def present_creation_recap(
    *, event: Message | CallbackQuery, state: FSMContext, draft: dict[str, Any],
    target: Any = None, retry: bool = False,
) -> None:
    async with draft_guard(state):
        current = await state.get_data()
        if retry and current.get("keyword_analysis_status") != "failed":
            return
        if not _draft_matches(current, draft):
            return
        token = uuid.uuid4().hex
        available = bool(keyword_variants_module.is_enabled
                         and keyword_variants_module.client.is_configured)
        if creation_work_choice_required(current):
            status, note = None, "Choisis l’œuvre avant l’analyse des variantes et exclusions."
        elif available:
            status, note = "pending", "Analyse des variantes et exclusions en cours…"
        else:
            status, note = "unavailable", "Analyse IA indisponible : aucune proposition ajoutée."
        await state.update_data(
            keyword_analysis_status=status, keyword_analysis_token=token,
            keyword_note=note, keywords_finalized=False, keyword_retry_available=False,
        )
        snapshot = await state.get_data()
    try:
        await show_creation_recap(
            event=event, state=state, target=target, expected_token=snapshot.get("creation_token"),
        )
    finally:
        if status == "pending":
            context = snapshot.get("book_context")
            safe_create_task(
                _search_keywords_in_background(
                    loading_msg=target, event=event, state=state, snapshot=snapshot,
                    name=str(snapshot.get("name") or ""),
                    category_slug=str(
                        snapshot.get("category_slug") or CategorySlug.BD_COMICS.value
                    ),
                    book_context=BookContext(**context) if context else None,
                ),
                name="add-variantes",
            )


async def relaunch_keyword_search(
    *, event: Message | CallbackQuery, loading_msg: Message | bool,
    state: FSMContext, draft: dict[str, Any],
) -> None:
    """Affiche le récapitulatif et relance l'analyse de l'article retenu."""
    await present_creation_recap(event=event, state=state, draft=draft, target=loading_msg)


async def _search_keywords_in_background(
    *, loading_msg: Any, event: Message | CallbackQuery, state: FSMContext,
    snapshot: dict[str, Any], name: str, category_slug: str, book_context: Any,
) -> None:
    """Complète uniquement le brouillon et la génération encore applicables."""
    async with draft_guard(state):
        if not _draft_matches(await state.get_data(), snapshot):
            return
    cancelled = False
    try:
        outcome = await keyword_variants_module.generate_variants_and_exclusions(
            name, category_slug=category_slug, context=book_context,
        )
    except asyncio.CancelledError:
        cancelled = True
        outcome = KeywordVariantsOutcome.unanswered()
    except Exception as exc:
        logger.warning("Analyse de mots-clés interrompue : %s", type(exc).__name__)
        outcome = KeywordVariantsOutcome.unanswered()

    async with draft_guard(state):
        current = await state.get_data()
        if not _draft_matches(current, snapshot):
            if cancelled:
                raise asyncio.CancelledError
            return
        manual = current.get("keyword_manual_fields") or []
        variants = list(current.get("variants") or [])
        exclusions = list(current.get("exclude_keywords") or [])
        accepted_variants = _variants_for_category(
            _merge_keywords(variants, outcome.variants), name, current.get("include_keywords"),
        ) if outcome.answered and "variants" not in manual else variants
        accepted_exclusions = sanitize_exclusions(
            _merge_keywords(exclusions, outcome.exclusions), name,
            author=current.get("author"), publisher=current.get("publisher"),
        ) if outcome.answered and "exclude_keywords" not in manual else exclusions
        added_variants = [v for v in accepted_variants if v not in variants]
        added_exclusions = [v for v in accepted_exclusions if v not in exclusions]
        retry = bool(not outcome.answered and keyword_variants_module.is_enabled
                     and keyword_variants_module.client.is_configured)
        note: str | None
        enriched = bool(added_variants or added_exclusions)
        if not outcome.answered:
            note = keyword_note(outcome, retry_label=None)
        else:
            note = CRITERIA_ENRICHED_NOTE if enriched else None
        await state.update_data(
            variants=accepted_variants, exclude_keywords=accepted_exclusions,
            keyword_note=note, keywords_finalized=outcome.answered,
            keyword_analysis_status="completed" if outcome.answered else "failed",
            keyword_retry_available=retry,
        )
        updated = await state.get_data()
        if (not cancelled and updated.get("keyword_recap_visible", True)
                and await state.get_state() == AlertCreationState.confirm_creation.state):
            recap, keyboard = build_creation_recap(updated)
            message_id = updated.get("keyword_message_id")
            chat_id = updated.get("keyword_chat_id")
            if message_id is not None and chat_id is not None and event.bot is not None:
                await event.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=recap, reply_markup=keyboard,
                )
            else:
                await _finalize_loading_message(loading_msg, event, recap, keyboard)
    if cancelled:
        raise asyncio.CancelledError


def _get_category_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "CategoryRepository", CategoryRepository)(session)


def _get_book_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "BookRepository", BookRepository)(session)


@router.message(Command("add", "ajouter"))
@router.message(Command("addisbn"))
@router.message(Command("addurl"))
@router.callback_query(F.data == "btn_add_alert")
async def start_add_alert(
    event: Message | CallbackQuery,
    state: FSMContext,
    db_session: Any = None,
    db_user: User | None = None,
) -> None:
    """Point d'entrée unifié pour l'ajout d'alertes multi-catégories."""
    token = uuid.uuid4().hex[:12]
    async with draft_guard(state):
        await state.clear()
        await state.update_data(creation_token=token, draft_revision=0)

    inline_query: str | None = None
    if isinstance(event, Message):
        raw_text = (event.text or "").strip()
        parts = raw_text.split(maxsplit=1)
        if len(parts) > 1:
            inline_query = parts[1].strip()

    if inline_query and db_user and db_session:
        clean_query, inline_price_cents = extract_inline_price_and_clean_query(inline_query)
        isbn_candidate = extract_clean_isbn(clean_query)
        alert_repo = _get_alert_repo(db_session)
        existing = await alert_repo.find_existing(
            db_user.id,
            isbn=isbn_candidate,
            title=clean_query,
        )
        if existing:
            await render_existing_alert_response(
                event,
                state,
                existing,
                new_price_cents=inline_price_cents,
                expected_creation_token=token,
            )
            return

    if inline_query and extract_clean_isbn(extract_inline_price_and_clean_query(inline_query)[0]):
        await process_bd_query_lookup(
            event, state, inline_query, db_session, db_user=db_user,
            expected_creation_token=token,
        )
        return

    loading_msg = None
    if inline_query and natural_add_module.is_enabled:
        if isinstance(event, Message) and event.bot:
            with contextlib.suppress(Exception):
                await event.bot.send_chat_action(chat_id=event.chat.id, action="typing")

        loading_text = (
            "🪄 <b>Création d'alerte en langage naturel</b>\n\n"
            "⏳ <i>L'IA analyse votre demande pour "
            f"« <b>{html.escape(inline_query)}</b> »...</i>"
        )
        loading_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⏳ Recherche IA en cours...", callback_data="noop")]
            ]
        )
        loading_msg = await event.answer(loading_text, reply_markup=loading_kb)

        parsed = await natural_add_module.parse_prompt(inline_query)
        if parsed:
            if (await state.get_data()).get("creation_token") != token:
                return
            if db_user and db_session:
                alert_repo = _get_alert_repo(db_session)
                existing = await alert_repo.find_existing(
                    db_user.id,
                    isbn=parsed.isbn13,
                    title=parsed.name,
                )
                if existing:
                    await render_existing_alert_response(
                        event,
                        state,
                        existing,
                        new_price_cents=parsed.max_price_cents,
                        expected_creation_token=token,
                        target_msg=loading_msg,
                    )
                    return

            state_data = {
                "creation_token": token,
                "original_input": inline_query,
                "search_queries": parsed.search_queries,
                "draft_revision": 0,
                "name": parsed.name,
                "category_slug": parsed.category_slug,
                "isbn": parsed.isbn13,
                "requested_volume": parsed.requested_volume,
                "language": parsed.book_context.language if parsed.book_context else None,
                "is_series": parsed.is_series,
                "author": parsed.author,
                "publisher": parsed.publisher,
                "include_keywords": parsed.include_keywords or [parsed.name],
                "exclude_keywords": parsed.exclude_keywords,
                "variants": [],
                "max_item_price_cents": parsed.max_price_cents,
                "selected_conditions": parsed.conditions,
                "isbn_note": parsed.isbn_note,
                "book_unidentified": parsed.book_unidentified,
                "book_context": (
                    parsed.book_context.model_dump() if parsed.book_context else None
                ),
                "edition_alternatives": [
                    edition.model_dump() for edition in parsed.edition_alternatives
                ],
                "work_choices": [
                    work.model_dump() for work in parsed.work_choices
                ],
            }
            if parsed.bibliographic_identity:
                state_data.update(parsed.bibliographic_identity)
            await populate_creation_prices(state_data, _get_book_repo(db_session))
            async with draft_guard(state):
                if (await state.get_data()).get("creation_token") != token:
                    return
                await state.update_data(data=state_data)
                await state.set_state(AlertCreationState.confirm_creation)

            await present_creation_recap(
                event=event, state=state, draft=await state.get_data(), target=loading_msg,
            )
            return

        if await process_bd_query_lookup(
            event, state, inline_query, db_session, db_user=db_user,
            expected_creation_token=token, require_catalogue_match=True,
            status_message=loading_msg,
        ):
            return

        async with draft_guard(state):
            if (await state.get_data()).get("creation_token") != token:
                return
            await state.update_data(inline_query=inline_query)
            await state.set_state(AlertCreationState.waiting_for_category)
        prompt_text = (
            f"➕ <b>Créer une alerte</b>\n\n"
            f"<b>Article :</b> « {html.escape(inline_query)} »\n"
            "<i>Choisis la catégorie de ce produit.</i>"
        )
        keyboard = bind_creation_keyboard(get_category_selection_keyboard(), token)
        await _finalize_loading_message(loading_msg, event, prompt_text, keyboard)
        return

    if inline_query:
        loading_msg = await event.answer("🔍 Recherche automatique de l'œuvre...")
        if await process_bd_query_lookup(
            event, state, inline_query, db_session, db_user=db_user,
            expected_creation_token=token, require_catalogue_match=True,
            status_message=loading_msg,
        ):
            return

    async with draft_guard(state):
        if (await state.get_data()).get("creation_token") != token:
            return
        if inline_query:
            await state.update_data(inline_query=inline_query)
        await state.set_state(AlertCreationState.waiting_for_category)
    if inline_query:
        prompt_text = (
            f"➕ <b>Créer une alerte</b>\n\n"
            f"<b>Article :</b> « {html.escape(inline_query)} »\n"
            "<i>Choisis la catégorie de ce produit.</i>"
        )
    else:
        prompt_text = (
            "➕ <b>Créer une alerte</b>\n\n"
            "<i>Choisis la catégorie du produit à surveiller.</i>"
        )

    keyboard = bind_creation_keyboard(get_category_selection_keyboard(), token)

    if loading_msg is not None:
        await _finalize_loading_message(loading_msg, event, prompt_text, keyboard)
    elif isinstance(event, CallbackQuery):
        await event.answer()
        if event.message:
            await event.message.answer(prompt_text, reply_markup=keyboard)
    else:
        await event.answer(prompt_text, reply_markup=keyboard)


@router.callback_query(AlertCreationState.waiting_for_category, F.data.startswith("add_cat:"))
async def handle_category_selected(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: Any,
    db_user: User,
    creation_callback_data: str | None = None,
) -> None:
    """Traite la sélection de catégorie et oriente vers le flux adapté."""
    interaction = (creation_callback_data if creation_callback_data is not None
                   else callback.data or "")
    async with draft_guard(state):
        initial = await state.get_data()
        generation = initial.get("search_token")
        applicable = (
            creation_callback_matches(interaction, initial)
            and await state.get_state() == AlertCreationState.waiting_for_category.state
        )
    await callback.answer()
    if not applicable:
        return
    category_slug = interaction.partition("@")[0].split(":")[1]

    unchanged_draft = None
    async with draft_guard(state):
        data = await state.get_data()
        if (
            not creation_callback_matches(interaction, data)
            or data.get("search_token") != generation
            or await state.get_state() != AlertCreationState.waiting_for_category.state
        ):
            return
        search_token = await _bump_search_token(state)
        inline_query = data.get("inline_query")
        if data.get("editing_category") and data.get("category_slug") == category_slug:
            await state.update_data(editing_category=False)
            await state.set_state(AlertCreationState.confirm_creation)
            unchanged_draft = await state.get_data()
        else:
            if data.get("editing_category"):
                updates = draft_updates_from_search(
                    BookSearchResult(status=BookSearchStatus.NOT_FOUND),
                    query_title=data["name"], category_slug=category_slug,
                )
                await state.update_data(
                    **updates, editing_category=False, keyword_note=None, keywords_finalized=False,
                    identification_sources=[],
                )
            if not inline_query:
                await state.update_data(category_slug=category_slug)
                await state.set_state(AlertCreationState.waiting_for_query)

    if unchanged_draft is not None:
        await show_creation_recap(
            event=callback, state=state, target=callback.message,
            expected_token=unchanged_draft.get("creation_token"),
        )
        return

    if inline_query:
        if category_slug == CategorySlug.BD_COMICS.value:
            await process_bd_query_lookup(
                callback, state, inline_query, db_session, db_user=db_user,
                expected_creation_token=data.get("creation_token"),
                expected_search_token=search_token,
                query_is_title=bool(data.get("editing_category")),
            )
        else:
            await process_tech_or_clothes_query(
                callback, state, inline_query, category_slug, db_session, db_user=db_user,
                expected_creation_token=data.get("creation_token"),
                expected_search_token=search_token,
                query_is_title=bool(data.get("editing_category")),
            )
        return

    if category_slug == CategorySlug.BD_COMICS.value:
        prompt = (
            "📚 <b>Alerte Livres & BD</b>\n\n"
            "<i>Entre le titre de l'œuvre que tu souhaites surveiller "
            "(avec auteur optionnel).</i>\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    elif category_slug == CategorySlug.TECH.value:
        prompt = (
            "💻 <b>Alerte Électronique</b>\n\n"
            "<i>Quel produit ou modèle électronique recherches-tu ?</i>\n\n"
            "<i>(ex: Raspberry Pi 4, Steam Deck 512Go, Sony WH-1000XM4...)</i>\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    elif category_slug == CategorySlug.GAMING.value:
        prompt = (
            "🎮 <b>Alerte Jeux Vidéo</b>\n\n"
            "<i>Quel jeu ou console recherches-tu ?</i>\n\n"
            "<i>(ex: Mario Kart 8 Switch, Zelda Tears of the Kingdom, Switch Oled...)</i>\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    else:
        prompt = (
            "👕 <b>Alerte Vêtements</b>\n\n"
            "<i>Quel vêtement ou modèle recherches-tu ?</i>\n\n"
            "<i>(ex: Veste Carhartt Detroit, Pull Ralph Lauren M...)</i>\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )

    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(prompt)


@router.message(AlertCreationState.waiting_for_query)
async def handle_query_input(
    message: Message,
    state: FSMContext,
    db_session: Any,
    db_user: User,
) -> None:
    """Traite la saisie du titre/produit et oriente vers la recherche adaptée."""
    query = (message.text or "").strip()
    if not query or len(query) < 2:
        await message.answer(
            "<i>La recherche doit contenir au moins 2 caractères. Réessaie :</i>"
        )
        return

    data = await state.get_data()
    category_slug = data.get("category_slug", CategorySlug.BD_COMICS.value)

    if category_slug == CategorySlug.BD_COMICS.value:
        await process_bd_query_lookup(
            message, state, query, db_session, db_user=db_user,
            expected_creation_token=data.get("creation_token"),
        )
    else:
        await process_tech_or_clothes_query(
            message, state, query, category_slug, db_session, db_user=db_user,
            expected_creation_token=data.get("creation_token"),
        )


@router.callback_query(F.data.startswith("upd_alert_price:"))
async def handle_update_existing_alert_price(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: Any,
) -> None:
    """Met à jour directement le prix d'une alerte déjà existante."""
    await callback.answer()
    parts = callback.data.split(":")  # type: ignore
    data = await state.get_data()
    if data.get("creation_token") or parts == ["upd_alert_price", ""]:
        alert_id_str = str(data.get("existing_alert_id") or "")
        proposed_price = data.get("new_max_price_cents")
        if not alert_id_str or proposed_price is None:
            return
        price_cents = int(proposed_price)
    else:
        alert_id_str = parts[1]
        price_cents = int(parts[2])

    alert_repo = _get_alert_repo(db_session)
    existing = await alert_repo.get_by_id(uuid.UUID(alert_id_str))
    if existing:
        existing.max_item_price_cents = price_cents
        await db_session.commit()
        await state.clear()
        new_price_str = format_price_cents(price_cents)
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"✅ <b>Prix maximum mis à jour</b>\n\n"
                f"📚 <b>{escape_display(existing.name)}</b>\n"
                f"<b>Nouveau prix maximum :</b> {new_price_str}\n\n"
                "L'alerte existante a été actualisée sans créer de doublon."
            )
    else:
        await state.clear()


@router.message(AlertCreationState.waiting_for_max_price)
async def process_max_price_input(
    message: Message,
    state: FSMContext,
    db_session: Any,
) -> None:
    """Valide le montant maximum et affiche le récapitulatif pour confirmation."""
    text = (message.text or "").strip()
    try:
        price_cents = parse_price_to_cents(text)
    except AlertServiceError as exc:
        await message.answer(f"⚠️ {exc}\nMerci d'indiquer un montant valide (ex: 25 ou 50) :")
        return

    data = await state.get_data()
    existing_id = data.get("existing_alert_id")
    if existing_id:
        alert_repo = _get_alert_repo(db_session)
        existing = await alert_repo.get_by_id(uuid.UUID(existing_id))
        if existing:
            existing.max_item_price_cents = price_cents
            await db_session.commit()
            await state.clear()
            new_price_str = format_price_cents(price_cents)
            await message.answer(
                f"✅ <b>Prix maximum mis à jour</b>\n\n"
                f"📚 <b>{escape_display(existing.name)}</b>\n"
                f"<b>Nouveau prix maximum :</b> {new_price_str}\n\n"
                "L'alerte existante a été actualisée sans créer de doublon."
            )
            return

    await state.update_data(max_item_price_cents=price_cents)
    await state.set_state(AlertCreationState.confirm_creation)

    updated_data = {**data, "max_item_price_cents": price_cents}
    recap_text, keyboard = build_creation_recap(updated_data)
    await message.answer(recap_text, reply_markup=keyboard)


@router.callback_query(AlertCreationState.confirm_creation, F.data == "alert_create_ai_variants")
async def handle_create_ai_variants(callback: CallbackQuery, state: FSMContext) -> None:
    """Génère et ajoute les variantes orthographiques et exclusions au brouillon d'alerte."""
    await callback.answer()
    await present_creation_recap(
        event=callback, state=state, draft=await state.get_data(),
        target=callback.message, retry=True,
    )


@router.callback_query(AlertCreationState.confirm_creation, F.data == "alert_create_confirm")
async def save_alert(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    db_session: Any,
    creation_attempt: CreationAttempt | None = None,
) -> None:
    """Consomme le brouillon, y compris pour un appel direct hors du dispatcher."""
    if creation_attempt is not None:
        await _save_alert(callback, state, db_user, db_session, creation_attempt)
        return
    await callback.answer()
    async with draft_guard(state) as attempt:
        draft = await state.get_data()
        if (
            not draft or draft.get("creation_outcome") in {"unknown", "committed"}
            or (
                isinstance(callback, CallbackQuery)
                and not creation_callback_matches(callback.data or "", draft)
            )
        ):
            attempt.release()
            await callback.answer(
                UNKNOWN_CREATION_MESSAGE
                if draft.get("creation_outcome") == "unknown" else STALE_CREATION_MESSAGE
            )
            return
        await _save_alert(callback, state, db_user, db_session, attempt)


async def _save_alert(
    callback: CallbackQuery, state: FSMContext, db_user: User, db_session: Any,
    creation_attempt: CreationAttempt,
) -> None:
    """Écrit l'alerte sous protection, puis prépare et présente hors de celle-ci."""
    data = await state.get_data()
    if data.get("keyword_analysis_status") == "pending" or creation_work_choice_required(data):
        creation_attempt.release()
        await callback.answer(
            "Attends la fin de l’analyse et le choix de l’œuvre.", show_alert=True,
        )
        return
    if creation_volume_issue(data):
        creation_attempt.release()
        recap_text, keyboard = build_creation_recap(data)
        if callback.message and hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(recap_text, reply_markup=keyboard)
        return

    category_slug = data.get("category_slug", CategorySlug.BD_COMICS.value)
    name = data.get("name") or "Alerte"
    isbn = data.get("isbn")
    author = data.get("author")
    publisher = data.get("publisher")
    includes = data.get("include_keywords", [])
    excludes = sanitize_exclusions(data.get("exclude_keywords", []), name,
                                   author=author, publisher=publisher)
    variants = _variants_for_category(data.get("variants", []), name, includes)
    max_price = data.get("max_item_price_cents")
    conditions = data.get("selected_conditions") or (
        list(DEFAULT_BD_CONDITIONS)
        if category_slug == CategorySlug.BD_COMICS.value
        else (
            list(DEFAULT_GAMING_CONDITIONS)
            if category_slug == CategorySlug.GAMING.value
            else (
                list(DEFAULT_TECH_CONDITIONS)
                if category_slug == CategorySlug.TECH.value
                else list(DEFAULT_CLOTHES_CONDITIONS)
            )
        )
    )
    cat_id_str = data.get("category_id")

    stored_draft = {
        **data,
        "exclude_keywords": excludes,
        "variants": variants,
        "selected_conditions": conditions,
        "keyword_note": data.get("keyword_note") if data.get("keywords_finalized") else None,
    }

    category_repo = _get_category_repo(db_session)
    category = None
    if cat_id_str:
        with contextlib.suppress(Exception):
            category = await category_repo.get_by_id(uuid.UUID(cat_id_str))
    if not category:
        category = await category_repo.get_by_slug(category_slug)

    alert_repo = _get_alert_repo(db_session)

    if category_slug == CategorySlug.BD_COMICS.value:
        book_repo = _get_book_repo(db_session)
        book = None

        if isbn:
            book = await book_repo.get_by_isbn13(isbn)
            if not book:
                created = await book_repo.create(
                    title=name,
                    normalized_title=normalize_text(name),
                    isbn13=isbn,
                    author=author,
                    publisher=publisher,
                    language=data.get("language") or "und",
                )
                book = await book_repo.get_by_id(created.id)

            vinted_url = f"https://www.vinted.fr/catalog?search_start_type=item_page_isbn&search_text={isbn}&order=newest_first"
            alert = await alert_repo.create(
                user_id=db_user.id,
                category_id=category.id,
                name=name,
                isbn=isbn,
                include_keywords=includes,
                exclude_keywords=excludes,
                variants=variants,
                max_item_price_cents=max_price,
                accepted_conditions=conditions,
                vinted_search_url=vinted_url,
            )

            if book:
                await book_repo.link_alert_to_book(alert.id, book.id, MatchConfidence.HIGH)
        else:
            vinted_url = f"https://www.vinted.fr/catalog?search_text={urllib.parse.quote_plus(name)}&order=newest_first"
            alert = await alert_repo.create(
                user_id=db_user.id,
                category_id=category.id,
                name=name,
                isbn=None,
                include_keywords=includes,
                exclude_keywords=excludes,
                variants=variants,
                max_item_price_cents=max_price,
                accepted_conditions=conditions,
                vinted_search_url=vinted_url,
            )

        if data.get("is_series"):
            for volume in data.get("series_volumes") or []:
                if not volume.get("confirmed") or not volume.get("isbn13"):
                    continue
                identified = await book_repo.get_by_isbn13(volume["isbn13"])
                if identified is None:
                    identified = await book_repo.create(
                        title=volume["title"], normalized_title=normalize_text(volume["title"]),
                        isbn13=volume["isbn13"], author=volume.get("author"),
                        publisher=volume.get("publisher"), language=volume.get("language") or "und",
                        publication_year=volume.get("publication_year"),
                        format_book=volume.get("format_detail"),
                    )
                identified.title = volume["title"]
                identified.normalized_title = normalize_text(volume["title"])
                await book_repo.link_alert_to_book(alert.id, identified.id, MatchConfidence.HIGH)

        alert.status = AlertStatus.PAUSED
        alert.preparation = PreparationRequest(
            author_hint=author,
            bibliographic_mode=("series" if data.get("is_series") is True
                                else "one_shot" if data.get("is_series") is False else None),
            series_reference_publisher=data.get("series_reference_publisher"),
            series_volumes=[{k: v for k, v in row.items() if k != "price_lines"}
                            for row in data.get("series_volumes") or []],
        ).stored()
        from app.services.book_market_pricing import recalculate_invalidated_series
        await recalculate_invalidated_series(db_session)
        await creation_attempt.commit_and_close(db_session)
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text("📊 <i>Préparation des prix du marché…</i>")
            except Exception:
                logger.warning("Message de préparation indisponible", exc_info=True)

        from app.services.book_metadata_service import CatalogueVerdict

        prepared_catalogues: list[CatalogueVerdict] = []
        prepared = await prepare_bd_alert(
            alert.id, **({"catalogue_sink": prepared_catalogues} if data.get("is_series") else {}),
        )
        current = await alert_repo.get_by_id(alert.id, refresh=True)
        if current is None:
            return  # Alerte supprimée pendant la préparation : ne rien réattacher.
        alert = current
        identity_changed = (alert.name, alert.isbn) != (name, isbn)
        vinted_url = alert.vinted_search_url or vinted_url
        if not prepared or alert.status != AlertStatus.ACTIVE:
            from app.bot.formatter import send_alert_card
            from app.bot.keyboards.alerts import get_alert_card_keyboard
            from app.use_cases.pause_alert import issue_pause_button

            if callback.message and hasattr(callback.message, "edit_text"):
                with contextlib.suppress(Exception):
                    await send_alert_card(
                        callback.message,
                        alert,
                        reply_markup=get_alert_card_keyboard(
                            alert.id, alert.status, 0, 1, category_slug,
                            preparation=alert.preparation,
                            pause_callback=await issue_pause_button(alert, db_session),
                        ),
                        edit=True,
                    )
            return
        books = await book_repo.get_all_books_for_alert(alert.id)
        stored_draft.update(
            name=alert.name, isbn=alert.isbn, include_keywords=alert.include_keywords,
            exclude_keywords=alert.exclude_keywords, variants=alert.variants,
            max_item_price_cents=alert.max_item_price_cents,
            selected_conditions=alert.accepted_conditions,
        )
        extra_lines: list[str] = []
        if stored_draft.get("is_series"):
            refresh_creation_series(
                stored_draft, books,
                prepared_catalogues[-1] if prepared_catalogues else None,
            )
            await populate_creation_prices(stored_draft, book_repo)
        else:
            for priced_book in books:
                summary = MarketPriceService.get_comparison_summary(
                    None, priced_book, for_creation=True,
                )
                if summary:
                    extra_lines.extend(summary.split("\n"))
        resp_text = creation_card(
            stored_draft,
            header=ALERT_CREATED_TITLE,
            footer=_CONFIRMATION_FOOTER,
            extra_lines=extra_lines or None,
        )
        if identity_changed:
            from app.bot.formatter import format_alert_card

            resp_text = format_alert_card(alert)
            if extra_lines:
                resp_text += "\n\n" + "\n".join(extra_lines)
    else:
        vinted_url = (
            f"https://www.vinted.fr/catalog?search_text={urllib.parse.quote_plus(name)}"
            "&order=newest_first"
        )
        alert = await alert_repo.create(
            user_id=db_user.id,
            category_id=category.id,
            name=name,
            isbn=None,
            include_keywords=includes,
            exclude_keywords=excludes,
            variants=variants,
            max_item_price_cents=max_price,
            accepted_conditions=conditions,
            vinted_search_url=vinted_url,
        )
        await creation_attempt.commit_and_close(db_session)

        resp_text = creation_card(
            stored_draft,
            header=ALERT_CREATED_TITLE,
            footer=_CONFIRMATION_FOOTER,
        )

    vinted_btn = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Voir la recherche Vinted", url=vinted_url)]]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        try:
            await callback.message.edit_text(
                resp_text, reply_markup=vinted_btn, disable_web_page_preview=True
            )
        except Exception:
            logger.warning("Confirmation de création indisponible", exc_info=True)

"""Confirmation, annulation et édition du flux /add."""

import html
import logging
import uuid
from typing import Any, cast

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.creation_drafts import CreationAttempt, draft_guard
from app.bot.handlers._alerts_router import (
    STALE_CREATION_MESSAGE,
    bind_creation_keyboard,
    router,
)
from app.bot.handlers.alerts_create import (
    _variants_for_category,
    present_creation_recap,
    relaunch_keyword_search,
    show_creation_recap,
)
from app.bot.handlers.alerts_helpers import (
    KEYWORDS_FROM_PREVIOUS_WORK,
    _resolve_edition_choices,
    build_creation_recap,
    realign_draft_to_isbn,
)
from app.bot.keyboards.alerts import (
    get_category_selection_keyboard,
    get_conditions_toggle_keyboard,
)
from app.bot.labels import (
    BTN_BACK_TO_MENU,
    BTN_BACK_TO_RECAP,
    FIELD_CONDITIONS,
    FIELD_EXCLUDE_KEYWORDS,
    FIELD_INCLUDE_KEYWORDS,
    FIELD_ISBN,
    FIELD_MAX_PRICE,
    FIELD_VARIANTS,
)
from app.bot.states.alert_creation import AlertCreationState
from app.core.constants import (
    STANDARD_CONDITIONS,
    CategorySlug,
)
from app.domain.enums import BookSearchStatus
from app.domain.rules.book_volume import requested_volume, volume_title
from app.domain.rules.keyword_hygiene import sanitize_exclusions
from app.services.ai.schemas import book_context_from
from app.services.alert_service import (
    AlertServiceError,
    parse_comma_separated_list,
    parse_price_to_cents,
)
from app.services.book_metadata_service import generate_recommended_keywords
from app.use_cases.book_search import (
    BookSearchUseCase,
    creation_bibliographic_identity,
    metadata_from_result,
)
from app.utils.isbn import extract_clean_isbn

logger = logging.getLogger("radar.alerts_create_confirm")

# L'annulation est incluse avant la création, sinon /cancel serait pris pour un titre.
cancel_router = Router(name="alerts_cancel")


@cancel_router.message(Command("cancel", "annuler"), StateFilter("*"))
@cancel_router.message(
    F.text.casefold().in_({"/cancel", "/annuler", "cancel", "annuler"}),
    StateFilter("*"),
)
async def handle_cancel_command(message: Message, state: FSMContext) -> None:
    """Annule la création ou la modification d'alerte en cours à tout moment."""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("ℹ️ Aucune opération en cours à annuler.")
        return

    async with draft_guard(state):
        await state.clear()
    await message.answer("❌ Opération annulée avec succès.")


# Brouillon vidé : re-rendre un récap viderait les critères.
DRAFT_GONE_MESSAGE = STALE_CREATION_MESSAGE
EMPTY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[])


async def _end_draft_without_recap(callback: CallbackQuery) -> None:
    """Referme une création dont le brouillon a disparu, sans récap par défaut."""
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(DRAFT_GONE_MESSAGE, reply_markup=EMPTY_KEYBOARD)


# Bouton périmé : acquitter sans réécrire pour ne pas effacer la confirmation.
@router.callback_query(F.data == "alert_create_confirm")
async def stale_alert_create_confirm(callback: CallbackQuery) -> None:
    """Acquitte un « Créer l'alerte » périmé, pour que le bouton cesse de tourner."""
    await callback.answer(DRAFT_GONE_MESSAGE)


@router.callback_query(F.data == "alert_create_cancel")
async def cancel_alert_creation(
    callback: CallbackQuery, state: FSMContext, creation_attempt: CreationAttempt | None = None,
) -> None:
    """Annule la création de l'alerte via bouton inline."""
    if creation_attempt is None:
        await callback.answer()
        async with draft_guard(state):
            await state.clear()
    else:
        await state.clear()
        creation_attempt.release()
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text("❌ Création de l'alerte annulée.")


@router.callback_query(F.data.startswith("al_create_edition:"))
async def select_alert_creation_edition(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Change l'ISBN de l'alerte à partir des éditions concurrentes proposées.

    L'ISBN n'est pas une formalité : il contraint la recherche Vinted, le matching et
    la cote de marché. Le choix se fait donc ici, tant que l'alerte n'est pas créée.
    """
    raw_index = (callback.data or "").rsplit(":", 1)[-1]
    data = await state.get_data()
    editions = data.get("edition_alternatives") or []

    if not raw_index.isdigit() or not 0 <= int(raw_index) < len(editions):
        await callback.answer("Édition inconnue.", show_alert=True)
        return

    chosen = editions[int(raw_index)]
    isbn13 = str(chosen.get("isbn13") or "").strip()
    if not isbn13:
        await callback.answer("Édition sans ISBN exploitable.", show_alert=True)
        return

    # Un clic ne confirme pas un ISBN : repasse par la politique d'autorité.
    title = str(data.get("name") or "").strip()
    try:
        confirmed = await BookSearchUseCase(declared_source=True).search_by_isbn(
            isbn13, expected_title=title or None
        )
    except Exception as exc:
        logger.debug("Revalidation d'édition impossible pour %s : %s", isbn13, exc)
        await callback.answer(
            "Vérification impossible pour l'instant : réessaie dans un moment.",
            show_alert=True,
        )
        return
    if confirmed.status != BookSearchStatus.FOUND:
        await callback.answer(
            "Cette édition n'est pas confirmable pour l'instant : réessaie dans un moment.",
            show_alert=True,
        )
        return

    meta = metadata_from_result(confirmed)
    if meta is None:
        await callback.answer("Édition non confirmée.", show_alert=True)
        return
    updates: dict[str, object] = {
        **creation_bibliographic_identity(confirmed),
        "isbn": isbn13, "is_series": False, "isbn_note": None,
        "language": meta.language, "book_context": book_context_from(meta).model_dump(),
        "author": meta.author, "publisher": meta.publisher,
    }
    if meta.publisher is None and chosen.get("publisher"):
        updates["publisher"] = chosen["publisher"]

    async with draft_guard(state):
        if not _same_draft(await state.get_data(), data):
            return
        await state.update_data(data=updates)
        await state.update_data(draft_revision=int(data.get("draft_revision") or 0) + 1,
                                pending_identity=None)
        updated_data = await state.get_data()
        await state.set_state(AlertCreationState.confirm_creation)
    await callback.answer("✅ Édition mise à jour")

    if callback.message and hasattr(callback.message, "edit_text"):
        recap_text, keyboard = build_creation_recap(updated_data)
        try:
            await callback.message.edit_text(recap_text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            # Édition identique : Telegram refuse « message is not modified ».
            if "message is not modified" not in str(exc):
                raise
            logger.debug(
                "Édition déjà retenue : carte identique, édition Telegram ignorée."
            )


@router.callback_query(F.data.startswith("al_create_work:"))
async def select_alert_creation_work(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Rattache le brouillon à l'œuvre homonyme que l'utilisateur désigne.

    Nommer l'œuvre engage son ISBN dans le matching, l'URL de recherche Vinted et la
    clé de cote de marché : le récap est le dernier moment où l'annuler est gratuit.
    Choisir un ouvrage remet aussi les mots-clés à zéro, puisqu'ils avaient été
    raisonnés sur l'autre.
    """
    raw_index = (callback.data or "").rsplit(":", 1)[-1]
    data = await state.get_data()
    works = data.get("work_choices") or []

    if not raw_index.isdigit() or not 0 <= int(raw_index) < len(works):
        await callback.answer("Œuvre inconnue.", show_alert=True)
        return

    chosen = works[int(raw_index)]
    isbn13 = str(chosen.get("isbn13") or "").strip()
    if not isbn13:
        await callback.answer("Œuvre sans ISBN exploitable.", show_alert=True)
        return

    title = str(data.get("name") or chosen.get("title") or "")

    # Lever l'ambiguïté n'est pas confirmer : repasse par la politique d'autorité.
    try:
        confirmed = await BookSearchUseCase(declared_source=True).search_by_isbn(
            isbn13, expected_title=title or None
        )
    except Exception as exc:
        logger.debug("Revalidation d'œuvre impossible pour %s : %s", isbn13, exc)
        await callback.answer(
            "Vérification impossible pour l'instant : réessaie dans un moment.",
            show_alert=True,
        )
        return
    if confirmed.status != BookSearchStatus.FOUND:
        await callback.answer(
            "Cette œuvre n'est pas confirmable pour l'instant : réessaie dans un moment.",
            show_alert=True,
        )
        return

    chosen_meta = metadata_from_result(confirmed)
    if chosen_meta is None:
        await callback.answer("Œuvre non confirmée.", show_alert=True)
        return
    author = chosen_meta.author

    updates: dict[str, object] = {
        # L'état vierge de l'ouvrage précédent vient en premier.
        **KEYWORDS_FROM_PREVIOUS_WORK,
        "isbn": isbn13,
        "is_series": False,
        "isbn_note": None,
        "name": chosen_meta.title,
        "include_keywords": generate_recommended_keywords(chosen_meta.title)[0],
        "author": author,
        "publisher": chosen_meta.publisher,
        "language": chosen_meta.language,
        "edition_alternatives": await _resolve_edition_choices(title, author),
        "book_context": book_context_from(chosen_meta).model_dump(),
    }
    async with draft_guard(state):
        if not _same_draft(await state.get_data(), data):
            return
        await state.update_data(data=updates)
        await state.update_data(draft_revision=int(data.get("draft_revision") or 0) + 1,
                                pending_identity=None)
        updated_data = await state.get_data()
        await state.set_state(AlertCreationState.confirm_creation)
    await callback.answer("✅ Œuvre retenue")

    if callback.message and hasattr(callback.message, "edit_text"):
        target = cast(Message, callback.message)
        await relaunch_keyword_search(
            event=target,
            loading_msg=target,
            state=state,
            draft=updated_data,
        )


@router.callback_query(
    F.data.in_(["alert_create_edit", "al_create_edit:menu"])
)
async def edit_alert_creation_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Affiche le menu complet de modification des informations d'alerte."""
    await callback.answer()
    await state.update_data(keyword_recap_visible=False)
    data = await state.get_data()
    if not data.get("category_slug"):
        await _end_draft_without_recap(callback)
        return
    name = data.get("name")
    category_slug = data.get("category_slug", CategorySlug.BD_COMICS.value)
    isbn = data.get("isbn")

    title = f" pour « {html.escape(name)} »" if name else ""
    text = (
        f"✏️ <b>Modifier l'alerte{title}</b>\n\n"
        "Choisis l'information à modifier :"
    )

    # Libellés nus : la valeur courante s'affiche après la saisie.
    buttons = [
        [InlineKeyboardButton(text="Nom / Titre", callback_data="al_create_edit:name")],
        [InlineKeyboardButton(text="Catégorie", callback_data="al_create_edit:category")],
    ]

    if category_slug == CategorySlug.BD_COMICS.value or isbn:
        buttons.append(
            [InlineKeyboardButton(text=FIELD_ISBN, callback_data="al_create_edit:isbn")]
        )

    buttons.extend([
        [InlineKeyboardButton(text=FIELD_MAX_PRICE, callback_data="al_create_edit:price")],
        [
            InlineKeyboardButton(
                text=FIELD_INCLUDE_KEYWORDS,
                callback_data="al_create_edit:include_keywords",
            )
        ],
        [
            InlineKeyboardButton(
                text=FIELD_EXCLUDE_KEYWORDS,
                callback_data="al_create_edit:exclude_keywords",
            )
        ],
        [
            InlineKeyboardButton(
                text=FIELD_VARIANTS, callback_data="al_create_edit:variants"
            )
        ],
        [
            InlineKeyboardButton(
                text=FIELD_CONDITIONS, callback_data="al_create_edit:conditions"
            )
        ],
        [
            InlineKeyboardButton(
                text=BTN_BACK_TO_RECAP, callback_data="al_create_edit:back"
            )
        ],
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    keyboard = bind_creation_keyboard(keyboard, data.get("creation_token"))
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


async def prompt_new_price(callback: CallbackQuery, state: FSMContext) -> None:
    """Demande la saisie d'un nouveau montant (compatibilité)."""
    await handle_create_edit_choice(callback, state)


@router.callback_query(F.data.startswith("al_create_edit:"))
async def handle_create_edit_choice(callback: CallbackQuery, state: FSMContext) -> None:
    """Affiche l'invite de saisie pour le champ sélectionné lors de la création."""
    parts = (callback.data or "").split(":")
    field = parts[1] if len(parts) > 1 else ""

    if field == "back":
        await back_to_recap(callback, state)
        return
    if field == "menu":
        await edit_alert_creation_prompt(callback, state)
        return
    if field == "category":
        await callback.answer()
        await state.update_data(keyword_recap_visible=False)
        async with draft_guard(state):
            data = await state.get_data()
            if not data.get("category_slug"):
                await _end_draft_without_recap(callback)
                return
            await state.update_data(
                editing_category=True, inline_query=data["name"],
                draft_revision=int(data.get("draft_revision") or 0) + 1,
                search_token=uuid.uuid4().int,
            )
            await state.set_state(AlertCreationState.waiting_for_category)
        keyboard = bind_creation_keyboard(
            get_category_selection_keyboard(), data.get("creation_token"),
        )
        if callback.message and hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(
                "✏️ <b>Catégorie</b>\n\n<i>Choisis la catégorie de ce produit.</i>",
                reply_markup=keyboard,
            )
        return
    if field == "conditions":
        await callback.answer()
        await state.update_data(keyword_recap_visible=False)
        data = await state.get_data()
        selected = list(data.get("selected_conditions") or STANDARD_CONDITIONS)
        text = (
            "✏️ <b>États acceptés</b>\n\n"
            "Coche ou décoche les états souhaités puis clique sur Confirmer."
        )
        keyboard = get_conditions_toggle_keyboard(selected)
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text=BTN_BACK_TO_MENU, callback_data="al_create_edit:menu")
        ])
        keyboard = bind_creation_keyboard(keyboard, data.get("creation_token"))
        if callback.message and hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(text, reply_markup=keyboard)
        return

    await callback.answer()
    data = await state.get_data()
    await state.set_state(AlertCreationState.waiting_for_edit_field)
    await state.update_data(current_edit_field=field, keyword_recap_visible=False)

    field_labels = {
        "name": ("Nom / Titre", data.get("name", ""), "Envoie le nouveau nom / titre :"),
        "isbn": (
            FIELD_ISBN,
            data.get("isbn", "") or "",
            "Envoie le nouvel ISBN (10 ou 13 chiffres, ou /skip pour retirer) :",
        ),
        "price": (
            "Prix maximum (en euros)",
            f"{data.get('max_item_price_cents', 0) / 100:.2f}".replace(".", ",")
            if data.get("max_item_price_cents")
            else "",
            "Envoie le nouveau prix max en € (ex: 15 ou 25) :",
        ),
        "include_keywords": (
            FIELD_INCLUDE_KEYWORDS,
            ", ".join(data.get("include_keywords", [])),
            "Envoie les mots-clés séparés par des virgules :",
        ),
        "exclude_keywords": (
            FIELD_EXCLUDE_KEYWORDS,
            ", ".join(data.get("exclude_keywords", [])),
            "Envoie les exclusions séparées par des virgules (ou /skip pour effacer) :",
        ),
        "variants": (
            FIELD_VARIANTS,
            ", ".join(data.get("variants", [])),
            "Envoie les variantes séparées par des virgules (ou /skip pour effacer) :",
        ),
    }

    title, current_val, hint = field_labels.get(
        field, (field, "", "Envoie la nouvelle valeur :")
    )

    if current_val:
        text = (
            f"✏️ <b>{title}</b>\n\n"
            f"Valeur actuelle :\n<code>{html.escape(current_val)}</code>\n\n"
            f"{hint}"
        )
    else:
        text = (
            f"✏️ <b>{title}</b>\n\n"
            f"Valeur actuelle : Non renseignée\n\n"
            f"{hint}"
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_BACK_TO_MENU, callback_data="al_create_edit:menu")],
            [
                InlineKeyboardButton(
                    text=BTN_BACK_TO_RECAP, callback_data="al_create_edit:back"
                )
            ],
        ]
    )
    keyboard = bind_creation_keyboard(keyboard, data.get("creation_token"))
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("cond_toggle:"))
async def handle_creation_condition_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Bascule l'état d'une condition pendant la création d'alerte."""
    parts = (callback.data or "").split(":")
    if len(parts) != 2 or not parts[1].isdecimal():
        await callback.answer("Sélection invalide.", show_alert=True)
        return
    idx = int(parts[1])
    if not 0 <= idx < len(STANDARD_CONDITIONS):
        await callback.answer("Sélection invalide.", show_alert=True)
        return
    target_cond = STANDARD_CONDITIONS[idx]
    data = await state.get_data()
    selected = list(data.get("selected_conditions") or STANDARD_CONDITIONS)
    if target_cond in selected:
        selected.remove(target_cond)
    else:
        selected.append(target_cond)
    await state.update_data(selected_conditions=selected)
    await callback.answer()
    keyboard = get_conditions_toggle_keyboard(selected)
    keyboard.inline_keyboard.append([
        InlineKeyboardButton(text=BTN_BACK_TO_MENU, callback_data="al_create_edit:menu")
    ])
    keyboard = bind_creation_keyboard(keyboard, data.get("creation_token"))
    if callback.message and hasattr(callback.message, "edit_reply_markup"):
        await callback.message.edit_reply_markup(reply_markup=keyboard)


@router.callback_query(F.data == "cond_confirm")
async def handle_creation_condition_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Confirme les conditions et retourne au récapitulatif."""
    await callback.answer("États mis à jour !")
    await back_to_recap(callback, state)


def _same_draft(current: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    return bool(current) and all(current.get(key) == snapshot.get(key) for key in (
        "creation_token", "draft_revision", "name", "isbn", "current_edit_field",
        "pending_identity",
    ))


async def _publish_edited_draft(
    message: Message, state: FSMContext, snapshot: dict[str, Any], updates: dict[str, Any],
    *, identity_changed: bool,
) -> None:
    async with draft_guard(state):
        if not _same_draft(await state.get_data(), snapshot):
            return
        current = {**await state.get_data(), **updates}
        updates = {
            **updates,
            "variants": _variants_for_category(
                current.get("variants"), current.get("name") or "", current.get("include_keywords"),
            ),
            "exclude_keywords": sanitize_exclusions(
                current.get("exclude_keywords") or [], current.get("name") or "",
                author=current.get("author"), publisher=current.get("publisher"),
            ),
        }
        if (snapshot.get("keyword_analysis_status") == "completed"
                and snapshot.get("current_edit_field") in ("variants", "exclude_keywords")):
            updates["keyword_note"] = None
        await state.update_data(**updates, pending_identity=None,
                                draft_revision=int(snapshot.get("draft_revision") or 0) + 1,
                                search_token=uuid.uuid4().int)
        await state.set_state(AlertCreationState.confirm_creation)
        current = await state.get_data()
    if identity_changed:
        await present_creation_recap(event=message, state=state, draft=current)
    else:
        await show_creation_recap(
            event=message, state=state, expected_token=current.get("creation_token"),
        )


@router.message(AlertCreationState.waiting_for_edit_field)
async def process_creation_edit_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data.get("current_edit_field")
    raw_text = (message.text or "").strip()
    clears = raw_text.lower() in ("/skip", "-", "aucun", "aucune", "clear")
    updates: dict[str, Any] = {}
    identity_changed = False
    if field == "name":
        if len(raw_text) < 2:
            await message.answer("⚠️ Le nom doit comporter au moins 2 caractères. Réessaie :")
            return
        number = requested_volume(raw_text) or data.get("requested_volume")
        identity_changed = raw_text != data.get("name")
        updates = {**(KEYWORDS_FROM_PREVIOUS_WORK if identity_changed else {}),
                   "name": volume_title(raw_text, number) if number else raw_text,
                   "requested_volume": number}
    elif field == "isbn":
        identity_changed = True
        if clears:
            updates = {**KEYWORDS_FROM_PREVIOUS_WORK, "isbn": None,
                       "is_series": bool(data.get("is_series")), "language": None,
                       "isbn_note": None, "edition_alternatives": [], "work_choices": []}
        else:
            isbn = extract_clean_isbn(raw_text)
            if not isbn:
                await message.answer("⚠️ Format d'ISBN invalide. Réessaie (ou /skip) :")
                return
            realignment = await realign_draft_to_isbn(data, isbn)
            if realignment.conflict:
                if not _same_draft(await state.get_data(), data):
                    return
                async with draft_guard(state):
                    if not _same_draft(await state.get_data(), data):
                        return
                    nonce = uuid.uuid4().hex[:10]
                    await state.update_data(pending_identity={"isbn": isbn, "token": nonce})
                keyboard = bind_creation_keyboard(InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Remplacer l’œuvre",
                                         callback_data=f"al_create_replace:yes:{nonce}"),
                    InlineKeyboardButton(text="Conserver",
                                         callback_data=f"al_create_replace:no:{nonce}"),
                ]]), data.get("creation_token"))
                await message.answer(f"⚠️ {html.escape(realignment.conflict)}\n\n"
                                     "Confirme le remplacement. Réessaie ou /skip.",
                                     reply_markup=keyboard)
                return
            updates = realignment.updates
    elif field == "price":
        try:
            updates = {"max_item_price_cents": parse_price_to_cents(raw_text)}
        except AlertServiceError as exc:
            await message.answer(f"⚠️ {exc}\nMerci d'indiquer un montant valide :")
            return
    elif field in ("include_keywords", "exclude_keywords", "variants"):
        parsed = [] if clears else parse_comma_separated_list(raw_text)
        if field == "include_keywords" and not parsed:
            await message.answer("⚠️ Au moins un mot-clé requis. Réessaie :")
            return
        updates = {field: parsed}
        if field in ("variants", "exclude_keywords"):
            updates["keyword_manual_fields"] = list(dict.fromkeys([
                *(data.get("keyword_manual_fields") or []), field,
            ]))
    await _publish_edited_draft(message, state, data, updates, identity_changed=identity_changed)


@router.callback_query(F.data.startswith("al_create_replace:"))
async def confirm_creation_identity(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    pending = data.get("pending_identity")
    parts = (callback.data or "").split(":")
    if (not pending or len(parts) != 3 or pending.get("token") != parts[2]
            or not isinstance(callback.message, Message)):
        return
    if parts[1] not in {"yes", "no"}:
        return
    if parts[1] == "no":
        await _publish_edited_draft(callback.message, state, data, {}, identity_changed=False)
        return
    result = await realign_draft_to_isbn(data, pending["isbn"], replace_identity=True)
    if result.conflict or result.updates.get("identity_unconfirmed"):
        await callback.message.answer("L’identité n’a pas pu être confirmée. Réessaie ou /skip.")
        return
    await _publish_edited_draft(
        callback.message, state, data, result.updates, identity_changed=True,
    )


@router.callback_query(F.data == "al_create_edit:back")
async def back_to_recap(callback: CallbackQuery, state: FSMContext) -> None:
    """Retourne directement au récapitulatif avec les boutons de confirmation."""
    await callback.answer()
    data = await state.get_data()
    if not data.get("category_slug"):
        await _end_draft_without_recap(callback)
        return
    await state.set_state(AlertCreationState.confirm_creation)

    await show_creation_recap(
        event=callback, state=state, target=callback.message,
        expected_token=data.get("creation_token"),
    )


"""Confirmation, annulation et édition du flux /add."""

import html
import logging
from typing import cast

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers._alerts_router import (
    STALE_CREATION_MESSAGE,
    bind_creation_keyboard,
    router,
)
from app.bot.handlers.alerts_create import relaunch_keyword_search
from app.bot.handlers.alerts_helpers import (
    KEYWORDS_FROM_PREVIOUS_WORK,
    _resolve_edition_choices,
    build_creation_recap,
    realign_draft_to_isbn,
)
from app.bot.keyboards.alerts import get_conditions_toggle_keyboard
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
from app.services.ai.schemas import book_context_from
from app.services.alert_service import (
    AlertServiceError,
    parse_comma_separated_list,
    parse_price_to_cents,
)
from app.use_cases.book_search import BookSearchUseCase, metadata_from_result
from app.utils.isbn import extract_clean_isbn

logger = logging.getLogger("radar.alerts_create_confirm")

# L'annulation vit sur son propre routeur, inclus **avant** celui de la création.
# La saisie d'un titre est un handler d'état qui accepte tout texte ; enregistré
# avant, il absorbait `/cancel` comme un titre et lançait une recherche BNF sur le
# mot « cancel ». Le routeur dédié rend l'annulation prioritaire quel que soit
# l'ordre d'import des modules.
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

    await state.clear()
    await message.answer("❌ Opération annulée avec succès.")


# Brouillon vidé (alerte déjà créée, session expirée) : re-rendre un récap par
# défaut afficherait « Titre : Alerte » et des critères vides, que l'utilisateur
# lirait comme une création en cours. On le dit, et on retire le clavier qui n'a
# plus d'objet.
DRAFT_GONE_MESSAGE = STALE_CREATION_MESSAGE
EMPTY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[])


async def _end_draft_without_recap(callback: CallbackQuery) -> None:
    """Referme une création dont le brouillon a disparu, sans récap par défaut."""
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(DRAFT_GONE_MESSAGE, reply_markup=EMPTY_KEYBOARD)


# `save_alert` n'agit que dans l'état `confirm_creation`. Le bouton, lui, survit à
# l'état dans l'historique : après une création, une annulation ou une session
# perdue. Sans ce filet, aucun handler ne répondait au callback et Telegram faisait
# tourner le bouton indéfiniment. On acquitte sans réécrire le message : un second
# appui suit souvent une création réussie, et éditer effacerait la confirmation.
@router.callback_query(F.data == "alert_create_confirm")
async def stale_alert_create_confirm(callback: CallbackQuery) -> None:
    """Acquitte un « Créer l'alerte » périmé, pour que le bouton cesse de tourner."""
    await callback.answer(DRAFT_GONE_MESSAGE)


@router.callback_query(F.data == "alert_create_cancel")
async def cancel_alert_creation(callback: CallbackQuery, state: FSMContext) -> None:
    """Annule la création de l'alerte via bouton inline."""
    await callback.answer()
    await state.clear()
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

    # Un clic ne confirme pas un ISBN : l'édition repasse par la politique
    # d'autorité. Le choix d'hier, rejoué aujourd'hui, ne contourne donc jamais un
    # statut `partial` (repli pendant une panne) ni une contradiction.
    title = str(data.get("name") or "").strip()
    try:
        confirmed = await BookSearchUseCase().search_by_isbn(isbn13, expected_title=title or None)
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
        "isbn": isbn13, "is_series": False, "isbn_note": None,
        "language": meta.language, "book_context": book_context_from(meta).model_dump(),
        "author": meta.author, "publisher": meta.publisher,
    }
    # L'éditeur et l'année décrits par la BNF suivent l'édition choisie.
    if meta.publisher is None and chosen.get("publisher"):
        updates["publisher"] = chosen["publisher"]

    if data.get("creation_token") and (
        (await state.get_data()).get("creation_token") != data["creation_token"]
    ):
        await callback.answer(STALE_CREATION_MESSAGE)
        return
    await state.update_data(data=updates)
    updated_data = await state.get_data()
    await state.set_state(AlertCreationState.confirm_creation)
    await callback.answer("✅ Édition mise à jour")

    if callback.message and hasattr(callback.message, "edit_text"):
        recap_text, keyboard = build_creation_recap(updated_data)
        try:
            await callback.message.edit_text(recap_text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            # Cliquer l'édition **déjà retenue** reconstruit une carte identique ; Telegram
            # refuse alors une édition « sans changement ». Ce n'est pas une panne : la
            # carte affichée est déjà la bonne, et l'ISBN n'a pas bougé. Toute autre
            # erreur Telegram (formatage, droits, réseau) doit continuer de remonter.
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

    # Lever l'ambiguïté n'est pas confirmer : l'œuvre choisie repasse par la politique
    # d'autorité. Pendant une panne, aucun callback ne promeut donc un candidat en
    # `found` — le brouillon reste celui d'une alerte textuelle.
    try:
        confirmed = await BookSearchUseCase().search_by_isbn(isbn13, expected_title=title or None)
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
        # D'abord l'état vierge laissé par l'ouvrage précédent, ensuite la fiche de
        # l'œuvre choisie : le spread ne doit pas écraser ce qu'on vient d'établir.
        **KEYWORDS_FROM_PREVIOUS_WORK,
        "isbn": isbn13,
        "is_series": False,
        "isbn_note": None,
        "author": author,
        "publisher": chosen_meta.publisher,
        "language": chosen_meta.language,
        "edition_alternatives": await _resolve_edition_choices(title, author),
        "book_context": book_context_from(chosen_meta).model_dump(),
    }
    if data.get("creation_token") and (
        (await state.get_data()).get("creation_token") != data["creation_token"]
    ):
        await callback.answer(STALE_CREATION_MESSAGE)
        return
    await state.update_data(data=updates)
    updated_data = await state.get_data()
    await state.set_state(AlertCreationState.confirm_creation)
    await callback.answer("✅ Œuvre retenue")

    if callback.message and hasattr(callback.message, "edit_text"):
        recap_text, keyboard = build_creation_recap(updated_data)
        target = cast(Message, callback.message)
        await target.edit_text(recap_text, reply_markup=keyboard)
        relaunch_keyword_search(
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

    # Un libellé nu, sans valeur entre parenthèses : la valeur courante est déjà
    # affichée juste après la saisie, et sur un téléphone la parenthèse tronque le
    # libellé au lieu de l'éclairer. Chaque bouton nomme le champ, rien d'autre.
    buttons = [
        [InlineKeyboardButton(text="Nom / Titre", callback_data="al_create_edit:name")],
    ]

    # Pour les BD/Comics ou si un ISBN est présent
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
    if field == "conditions":
        await callback.answer()
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

    # Champs textuels
    await callback.answer()
    data = await state.get_data()
    await state.set_state(AlertCreationState.waiting_for_edit_field)
    await state.update_data(current_edit_field=field)

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


@router.message(AlertCreationState.waiting_for_edit_field)
async def process_creation_edit_input(message: Message, state: FSMContext) -> None:
    """Traite la saisie textuelle pour le champ en cours de modification à la création."""
    data = await state.get_data()
    field = data.get("current_edit_field")
    raw_text = (message.text or "").strip()
    # Un changement d'identité invalide les mots-clés : ils seront recalculés.
    identity_changed = False

    if field == "name":
        if len(raw_text) < 2:
            await message.answer("⚠️ Le nom doit comporter au moins 2 caractères. Réessaie :")
            return
        data = await state.get_data()
        number = requested_volume(raw_text) or data.get("requested_volume")
        await state.update_data(
            name=volume_title(raw_text, number) if number else raw_text,
            requested_volume=number,
        )

    elif field == "isbn":
        # `isbn_note` décrit la provenance de l'ISBN ou de son absence : changer l'ISBN
        # invalide cette note, qui sinon contredit le récap (« non confirmé par la BNF »
        # affiché sous un ISBN que l'utilisateur vient de faire confirmer).
        if raw_text.lower() in ("/skip", "-", "aucun", "aucune", "clear"):
            await state.update_data(
                isbn=None,
                is_series=not bool(data.get("requested_volume")),
                language=None,
                isbn_note=None,
                # Les éditions proposal étaient celles de l'ISBN abandonné.
                edition_alternatives=[],
                work_choices=[],
                **KEYWORDS_FROM_PREVIOUS_WORK,
            )
        else:
            clean_isbn = extract_clean_isbn(raw_text)
            if not clean_isbn:
                await message.answer(
                    "⚠️ Format d'ISBN invalide (doit comporter 10 ou 13 chiffres). "
                    "Réessaie (ou /skip) :"
                )
                return
            # Changer d'ISBN, c'est changer d'ouvrage : toute la fiche d'identité et les
            # mots-clés calculés sur l'ancien sont réécrits, pas conservés.
            realignment = await realign_draft_to_isbn(data, clean_isbn)
            if realignment.conflict:
                # Garder cet ISBN ferait matcher l'alerte sur un autre livre et coter
                # celui-ci : on nomme le vrai titre et on redemande la saisie.
                await message.answer(
                    f"⚠️ {realignment.conflict}\n\n"
                    "Réessaie avec l'ISBN de l'ouvrage voulu (ou /skip) :"
                )
                return
            await state.update_data(data=realignment.updates)
            identity_changed = True

    elif field == "price":
        try:
            price_cents = parse_price_to_cents(raw_text)
            await state.update_data(max_item_price_cents=price_cents)
        except AlertServiceError as exc:
            await message.answer(f"⚠️ {exc}\nMerci d'indiquer un montant valide (ex: 15 ou 25) :")
            return

    elif field == "include_keywords":
        parsed = parse_comma_separated_list(raw_text)
        if not parsed:
            await message.answer("⚠️ Au moins un mot-clé requis. Réessaie :")
            return
        await state.update_data(include_keywords=parsed)

    elif field == "exclude_keywords":
        if raw_text.lower() in ("/skip", "-", "aucun", "aucune"):
            await state.update_data(exclude_keywords=[])
        else:
            await state.update_data(exclude_keywords=parse_comma_separated_list(raw_text))

    elif field == "variants":
        if raw_text.lower() in ("/skip", "-", "aucun", "aucune"):
            await state.update_data(variants=[])
        else:
            await state.update_data(variants=parse_comma_separated_list(raw_text))

    # Réafficher le récapitulatif mis à jour
    updated_data = await state.get_data()
    await state.set_state(AlertCreationState.confirm_creation)
    recap_text, keyboard = build_creation_recap(updated_data)
    recap_msg = await message.answer(recap_text, reply_markup=keyboard)

    if identity_changed:
        # Les variantes et exclusions portaient sur l'ancien ouvrage : le récap s'affiche
        # sans elles, puis se complète sur place quand le moteur a répondu.
        relaunch_keyword_search(
            event=message,
            loading_msg=recap_msg,
            state=state,
            draft=updated_data,
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

    recap_text, keyboard = build_creation_recap(data)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(recap_text, reply_markup=keyboard)


"""Confirmation, annulation et édition du flux /add."""

import html
import logging
from typing import cast

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers._alerts_router import router
from app.bot.handlers.alerts_create import relaunch_keyword_search
from app.bot.handlers.alerts_helpers import (
    KEYWORDS_FROM_PREVIOUS_WORK,
    _resolve_edition_choices,
    build_creation_recap,
    realign_draft_to_isbn,
)
from app.bot.keyboards.alerts import get_conditions_toggle_keyboard
from app.bot.states.alert_creation import AlertCreationState
from app.core.constants import (
    STANDARD_CONDITIONS,
    CategorySlug,
)
from app.services.ai.schemas import book_context_from
from app.services.alert_service import (
    AlertServiceError,
    parse_comma_separated_list,
    parse_price_to_cents,
)
from app.services.book_metadata_service import BookMetadata
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
DRAFT_GONE_MESSAGE = (
    "ℹ️ Cette création est terminée. Utilise /add pour créer une nouvelle alerte."
)
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

    updates: dict[str, object] = {"isbn": isbn13, "is_series": False, "isbn_note": None}
    # L'éditeur et l'année décrits par la BNF suivent l'édition choisie.
    if chosen.get("publisher"):
        updates["publisher"] = chosen["publisher"]

    await state.update_data(data=updates)
    updated_data = await state.get_data()
    await state.set_state(AlertCreationState.confirm_creation)
    await callback.answer("✅ Édition mise à jour")

    if callback.message and hasattr(callback.message, "edit_text"):
        recap_text, keyboard = build_creation_recap(updated_data)
        await callback.message.edit_text(recap_text, reply_markup=keyboard)


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
    author = str(chosen.get("author") or "").strip() or None
    chosen_meta = BookMetadata(
        title=str(chosen.get("title") or title),
        author=author,
        publisher=chosen.get("publisher"),
        isbn13=isbn13,
        publication_year=chosen.get("publication_year"),
    )

    updates: dict[str, object] = {
        # D'abord l'état vierge laissé par l'ouvrage précédent, ensuite la fiche de
        # l'œuvre choisie : le spread ne doit pas écraser ce qu'on vient d'établir.
        **KEYWORDS_FROM_PREVIOUS_WORK,
        "isbn": isbn13,
        "is_series": False,
        "isbn_note": None,
        "author": author,
        "publisher": chosen.get("publisher") or None,
        "edition_alternatives": await _resolve_edition_choices(title, author),
        "book_context": book_context_from(chosen_meta).model_dump(),
    }
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
            [InlineKeyboardButton(text="ISBN", callback_data="al_create_edit:isbn")]
        )

    buttons.extend([
        [InlineKeyboardButton(text="Prix maximum", callback_data="al_create_edit:price")],
        [
            InlineKeyboardButton(
                text="Mots-clés inclus", callback_data="al_create_edit:include_keywords"
            )
        ],
        [
            InlineKeyboardButton(
                text="Mots-clés exclus", callback_data="al_create_edit:exclude_keywords"
            )
        ],
        [InlineKeyboardButton(text="Variantes", callback_data="al_create_edit:variants")],
        [
            InlineKeyboardButton(
                text="États acceptés", callback_data="al_create_edit:conditions"
            )
        ],
        [
            InlineKeyboardButton(
                text="← Retour au récapitulatif", callback_data="al_create_edit:back"
            )
        ],
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
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
            InlineKeyboardButton(text="← Retour au menu", callback_data="al_create_edit:menu")
        ])
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
            "ISBN",
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
            "Mots-clés inclus",
            ", ".join(data.get("include_keywords", [])),
            "Envoie les mots-clés séparés par des virgules :",
        ),
        "exclude_keywords": (
            "Mots-clés exclus",
            ", ".join(data.get("exclude_keywords", [])),
            "Envoie les exclusions séparées par des virgules (ou /skip pour effacer) :",
        ),
        "variants": (
            "Variantes",
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
            [InlineKeyboardButton(text="← Retour au menu", callback_data="al_create_edit:menu")],
            [
                InlineKeyboardButton(
                    text="← Retour au récapitulatif", callback_data="al_create_edit:back"
                )
            ],
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("cond_toggle:"))
async def handle_creation_condition_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Bascule l'état d'une condition pendant la création d'alerte."""
    parts = (callback.data or "").split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await callback.answer("Sélection invalide.", show_alert=True)
        return
    idx = int(parts[1])
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
        InlineKeyboardButton(text="← Retour au menu", callback_data="al_create_edit:menu")
    ])
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
        await state.update_data(name=raw_text)

    elif field == "isbn":
        # `isbn_note` décrit la provenance de l'ISBN ou de son absence : changer l'ISBN
        # invalide cette note, qui sinon contredit le récap (« non confirmé par la BNF »
        # affiché sous un ISBN que l'utilisateur vient de faire confirmer).
        if raw_text.lower() in ("/skip", "-", "aucun", "aucune", "clear"):
            await state.update_data(
                isbn=None,
                is_series=True,
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


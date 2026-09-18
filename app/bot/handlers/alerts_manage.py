"""Flux de gestion, modification (/edit), pause/reprise (/pause, /resume)
et suppression (/delete) d'alertes.
"""

import html
import logging
import urllib.parse
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.formatter import format_alert_card
from app.bot.handlers.alerts_view import show_alerts, view_single_alert
from app.bot.keyboards.alerts import (
    get_alert_card_keyboard,
    get_alert_edit_fields_keyboard,
    get_delete_confirmation_keyboard,
    get_edit_category_selection_keyboard,
)
from app.bot.states.alert_creation import AlertEditState
from app.core.constants import AlertStatus, CategorySlug
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.category_repo import CategoryRepository
from app.services.alert_service import (
    AlertServiceError,
    parse_comma_separated_list,
    parse_price_to_cents,
    validate_and_parse_vinted_url,
)
from app.utils.isbn import extract_clean_isbn

logger = logging.getLogger(__name__)

router = Router(name="alerts_manage")


def _get_alert_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "AlertRepository", AlertRepository)(session)


def _get_category_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "CategoryRepository", CategoryRepository)(session)


@router.message(Command("edit"))
async def handle_edit_command(
    message: Message,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche le sélecteur de catégorie pour parcourir et modifier les alertes."""
    args = (message.text or "").strip().split(maxsplit=1)
    if len(args) > 1:
        await show_alerts(message, db_user, db_session)
        return

    await show_edit_categories(message, db_user, db_session)


async def show_edit_categories(
    target: Message | CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche le menu de sélection de catégorie pour /edit."""
    alert_repo = _get_alert_repo(db_session)
    total_count = await alert_repo.count_by_user(db_user.id)
    if total_count == 0:
        msg = "Aucune alerte active pour le moment. Utilise /add pour en créer une."
        if isinstance(target, CallbackQuery):
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(msg)
        else:
            await target.answer(msg)
        return

    cat_repo = _get_category_repo(db_session)
    active_categories = await cat_repo.get_all_active()
    counts = await alert_repo.count_by_user_grouped_by_category(db_user.id)

    text = (
        "📁 <b>Gestion et modification de vos alertes</b>\n\n"
        "Choisissez une catégorie pour afficher et modifier vos alertes :"
    )
    keyboard = get_edit_category_selection_keyboard(active_categories, counts, total_count)

    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message and hasattr(target.message, "edit_text"):
            await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "ed_cat_menu")
async def handle_edit_category_menu(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Retourne au sélecteur de catégorie dans /edit."""
    await show_edit_categories(callback, db_user, db_session)


@router.callback_query(F.data.startswith("ed_cat:"))
async def handle_select_edit_category(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche la première alerte de la catégorie choisie."""
    await callback.answer()
    parts = callback.data.split(":")  # type: ignore
    category_slug = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0

    alert_repo = _get_alert_repo(db_session)
    total_count = await alert_repo.count_by_user(db_user.id, category_slug=category_slug)
    if total_count == 0:
        await callback.answer("Aucune alerte dans cette catégorie.", show_alert=True)
        return

    page = max(0, min(page, total_count - 1))
    alerts = await alert_repo.list_by_user(
        db_user.id, limit=1, offset=page, category_slug=category_slug
    )
    if not alerts:
        await callback.answer("Aucune alerte trouvée.", show_alert=True)
        return

    alert = alerts[0]
    card_text = format_alert_card(alert, current_index=page, total_count=total_count)
    keyboard = get_alert_card_keyboard(
        alert.id,
        alert.status,
        current_page=page,
        total_pages=total_count,
        category_slug=category_slug,
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(card_text, reply_markup=keyboard)


@router.callback_query(F.data == "al_close")
async def handle_close_alert_menu(callback: CallbackQuery) -> None:
    """Ferme le menu d'alertes."""
    await callback.answer()
    if callback.message and hasattr(callback.message, "delete"):
        await callback.message.delete()


@router.callback_query(F.data.startswith("al_edit:"))
async def select_edit_field(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche les options de modification pour une alerte donnée."""
    await callback.answer()
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    cat_slug = alert.category.slug if alert.category else None
    cat_emoji = alert.category.emoji if alert.category else "🎯"
    text = (
        f"Modifier l'alerte {cat_emoji} « <b>{html.escape(alert.name)}</b> ».\n"
        "Choisis le critère à mettre à jour :"
    )
    keyboard = get_alert_edit_fields_keyboard(alert.id, category_slug=cat_slug)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("ed_f:"))
async def prompt_new_field_value(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Demande la nouvelle valeur pour le champ sélectionné."""
    await callback.answer()
    parts = callback.data.split(":")  # type: ignore
    alert_id = parts[1]
    field = parts[2]

    await state.set_state(AlertEditState.waiting_for_new_value)
    await state.update_data(edit_alert_id=alert_id, edit_field=field)

    prompts = {
        "name": "Envoie le nouveau nom / titre :",
        "isbn": "Envoie le nouvel ISBN-10 ou ISBN-13 (ex: 9782381961903) :",
        "url": "Envoie le nouveau lien Vinted (HTTPS) :",
        "include_keywords": "Envoie les nouveaux mots-clés inclus (séparés par des virgules) :",
        "exclude_keywords": "Envoie les nouveaux mots-clés à exclure (ou /skip pour aucun) :",
        "variants": "Envoie les nouvelles variantes (ou /skip pour aucune) :",
        "max_price": "Envoie le nouveau prix maximum de l'article en euros (ex: 15) :",
        "conditions": (
            "Envoie les états acceptés séparés par des virgules\n"
            "(ex: Neuf avec étiquette, Très bon état) :"
        ),
    }
    msg = prompts.get(field, "Envoie la nouvelle valeur :")
    if callback.message and hasattr(callback.message, "answer"):
        await callback.message.answer(msg)


@router.message(AlertEditState.waiting_for_new_value)
async def save_edited_field_value(
    message: Message,
    state: FSMContext,
    db_user: User,
    db_session: Any,
) -> None:
    """Applique la modification du champ en base de données."""
    data = await state.get_data()
    alert_id = uuid.UUID(data["edit_alert_id"])
    field = data["edit_field"]
    text = (message.text or "").strip()

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await message.answer("Alerte introuvable.")
        await state.clear()
        return

    try:
        if field == "name":
            if len(text) < 2:
                raise AlertServiceError("Le nom doit contenir au moins 2 caractères.")
            alert.name = text
            if alert.category and alert.category.slug in (
                CategorySlug.TECH.value,
                CategorySlug.CLOTHES.value,
                CategorySlug.GAMING.value,
            ):
                alert.vinted_search_url = f"https://www.vinted.fr/catalog?search_text={urllib.parse.quote_plus(text)}&order=newest_first"
        elif field == "isbn":
            clean_isbn = extract_clean_isbn(text)
            if not clean_isbn:
                raise AlertServiceError(
                    "Format d'ISBN invalide (doit comporter 10 ou 13 chiffres)."
                )
            alert.isbn = clean_isbn
            if clean_isbn not in alert.include_keywords:
                alert.include_keywords = [clean_isbn] + [
                    kw for kw in alert.include_keywords if not kw.isdigit()
                ]
        elif field == "url":
            parsed = validate_and_parse_vinted_url(text)
            alert.vinted_search_url = parsed["url"]
        elif field == "include_keywords":
            kw = parse_comma_separated_list(text)
            if not kw:
                raise AlertServiceError("Au moins un mot-clé est requis.")
            alert.include_keywords = kw
        elif field == "exclude_keywords":
            alert.exclude_keywords = (
                [] if text.lower() == "/skip" else parse_comma_separated_list(text)
            )
        elif field == "variants":
            alert.variants = [] if text.lower() == "/skip" else parse_comma_separated_list(text)
        elif field == "max_price":
            alert.max_item_price_cents = parse_price_to_cents(text)
        elif field == "conditions":
            alert.accepted_conditions = parse_comma_separated_list(text) if text else None

        await db_session.commit()
        await state.clear()

        cat_slug = alert.category.slug if alert.category else "all"
        total_count = await alert_repo.count_by_user(db_user.id, category_slug=cat_slug)

        await message.answer(f"✅ Champ <b>{field}</b> mis à jour avec succès !")
        card_text = format_alert_card(alert, current_index=0, total_count=total_count)
        await message.answer(
            card_text,
            reply_markup=get_alert_card_keyboard(
                alert.id,
                alert.status,
                current_page=0,
                total_pages=total_count,
                category_slug=cat_slug,
            ),
        )
    except AlertServiceError as exc:
        await message.answer(f"⚠️ {exc}\nMerci de réessayer :")


@router.message(Command("pause"))
async def handle_pause_command(message: Message, db_user: User, db_session: Any) -> None:
    """Affiche les alertes actives pour mise en pause."""
    await show_alerts(message, db_user, db_session)


@router.callback_query(F.data.startswith("al_pause:"))
async def toggle_pause_alert(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Met en pause une alerte active."""
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore
    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    await alert_repo.set_status(alert, AlertStatus.PAUSED)
    await db_session.commit()
    await callback.answer("⏸ Alerte mise en pause.")
    await view_single_alert(callback, db_user, db_session)


@router.message(Command("resume"))
async def handle_resume_command(message: Message, db_user: User, db_session: Any) -> None:
    """Affiche les alertes pour réactivation."""
    await show_alerts(message, db_user, db_session)


@router.callback_query(F.data.startswith("al_resume:"))
async def toggle_resume_alert(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Réactive une alerte mise en pause."""
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore
    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    await alert_repo.set_status(alert, AlertStatus.ACTIVE)
    await db_session.commit()
    await callback.answer("▶️ Alerte réactivée.")
    await view_single_alert(callback, db_user, db_session)


@router.message(Command("delete"))
async def handle_delete_command(message: Message, db_user: User, db_session: Any) -> None:
    """Affiche les alertes pour suppression."""
    await show_alerts(message, db_user, db_session)


@router.callback_query(F.data.startswith("al_del:"))
async def prompt_delete_confirmation(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Demande la confirmation obligatoire avant la suppression."""
    await callback.answer()
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    msg = f"Supprimer définitivement l'alerte « <b>{alert.name}</b> » ?"
    keyboard = get_delete_confirmation_keyboard(alert.id)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(msg, reply_markup=keyboard)


@router.callback_query(F.data.startswith("al_del_confirm:"))
async def confirm_soft_delete_alert(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Exécute la suppression définitive et complète de l'alerte et de ses données associées."""
    await callback.answer()
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        if isinstance(callback.message, Message):
            await callback.message.edit_text("Alerte déjà supprimée.")
        return

    alert_name = alert.name
    await alert_repo.delete_completely(alert)
    await db_session.commit()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"🗑 L'alerte « <b>{alert_name}</b> » et toutes ses données associées "
            "ont été supprimées définitivement.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📁 Retour aux catégories", callback_data="ed_cat_menu"
                        )
                    ]
                ]
            ),
        )

"""Flux d'affichage et de consultation des alertes (/show, /list)."""

import html
import logging
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.formatter import format_alert_card
from app.bot.handlers.alerts_helpers import format_alerts_list_text
from app.bot.keyboards.alerts import (
    get_alert_card_keyboard,
    get_alerts_list_keyboard,
    get_list_category_selection_keyboard,
)
from app.bot.labels import STATUS_ACTIVE, STATUS_PAUSED
from app.core.constants import AlertStatus, CategorySlug
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.category_repo import CategoryRepository

logger = logging.getLogger(__name__)

router = Router(name="alerts_view")


def _get_alert_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "AlertRepository", AlertRepository)(session)


def _get_category_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "CategoryRepository", CategoryRepository)(session)


CATEGORY_ARG_MAP = {
    "bd": CategorySlug.BD_COMICS.value,
    "bd_comics": CategorySlug.BD_COMICS.value,
    "livre": CategorySlug.BD_COMICS.value,
    "livres": CategorySlug.BD_COMICS.value,
    "book": CategorySlug.BD_COMICS.value,
    "books": CategorySlug.BD_COMICS.value,
    "tech": CategorySlug.TECH.value,
    "electronique": CategorySlug.TECH.value,
    "électronique": CategorySlug.TECH.value,
    "gaming": CategorySlug.GAMING.value,
    "jeux": CategorySlug.GAMING.value,
    "jeu": CategorySlug.GAMING.value,
    "jeuxvideo": CategorySlug.GAMING.value,
    "jeux_video": CategorySlug.GAMING.value,
    "game": CategorySlug.GAMING.value,
    "games": CategorySlug.GAMING.value,
    "clothes": CategorySlug.CLOTHES.value,
    "vetement": CategorySlug.CLOTHES.value,
    "vetements": CategorySlug.CLOTHES.value,
    "vêtement": CategorySlug.CLOTHES.value,
    "vêtements": CategorySlug.CLOTHES.value,
    "all": "all",
    "tout": "all",
    "toutes": "all",
}


@router.message(Command("show"))
async def show_alerts(
    message: Message,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche la première alerte active de l'utilisateur avec pagination
    ou recherche par terme/ISBN.
    """
    alert_repo = _get_alert_repo(db_session)

    # Vérifier si l'utilisateur a passé un terme de recherche (ex: /show Soli ou ISBN)
    args = (message.text or "").strip().split(maxsplit=1)
    if len(args) > 1:
        query = args[1].strip()
        matched_alerts = await alert_repo.search_by_user(db_user.id, query=query, limit=10)
        if not matched_alerts:
            await message.answer(
                f"⚠️ Aucune alerte trouvée correspondant à « <b>{html.escape(query)}</b> ».\n\n"
                "Tape /show pour faire défiler toutes tes alertes ou /add pour en créer une."
            )
            return

        if len(matched_alerts) == 1:
            alert = matched_alerts[0]
            card_text = format_alert_card(alert, current_index=0, total_count=1)
            keyboard = get_alert_card_keyboard(
                alert.id, alert.status, current_page=0, total_pages=1
            )
            await message.answer(card_text, reply_markup=keyboard)
            return

        # Plusieurs correspondances : afficher la liste avec boutons d'accès direct
        buttons = [
            [
                InlineKeyboardButton(
                    text=(
                        f"{a.name} "
                        f"({STATUS_ACTIVE if a.status == AlertStatus.ACTIVE else STATUS_PAUSED})"
                    ),
                    callback_data=f"al_view:{a.id}",
                )
            ]
            for a in matched_alerts
        ]
        buttons.append(
            [InlineKeyboardButton(text="Liste des alertes", callback_data="al_page:0")]
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            f"{len(matched_alerts)} alertes correspondent à « {html.escape(query)} » :",
            reply_markup=keyboard,
        )
        return

    # Comportement par défaut sans argument : pagination complète
    total_count = await alert_repo.count_by_user(db_user.id)
    if total_count == 0:
        await message.answer("Aucune alerte active pour le moment. Utilise /add pour en créer une.")
        return

    alerts = await alert_repo.list_by_user(db_user.id, limit=1, offset=0)
    alert = alerts[0]

    card_text = format_alert_card(alert, current_index=0, total_count=total_count)
    keyboard = get_alert_card_keyboard(
        alert.id, alert.status, current_page=0, total_pages=total_count
    )
    await message.answer(card_text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("al_page:"))
async def paginate_alerts(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Navigue entre les cartes d'alertes."""
    await callback.answer()
    parts = callback.data.split(":")  # type: ignore
    if len(parts) == 2:
        category_slug = "all"
        page = int(parts[1])
    else:
        category_slug = parts[1]
        page = int(parts[2])

    alert_repo = _get_alert_repo(db_session)
    total_count = await alert_repo.count_by_user(db_user.id, category_slug=category_slug)
    if total_count == 0:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "Aucune alerte trouvée dans cette catégorie.",
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
        return

    page = max(0, min(page, total_count - 1))
    alerts = await alert_repo.list_by_user(
        db_user.id, limit=1, offset=page, category_slug=category_slug
    )
    if not alerts:
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


@router.callback_query(F.data.startswith("al_view:"))
async def view_single_alert(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche la carte d'une alerte spécifique après action ou retour."""
    await callback.answer()
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert or alert.status == AlertStatus.DELETED:
        if callback.message and hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(
                "Alerte introuvable.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📁 Catégories", callback_data="ed_cat_menu")]
                    ]
                ),
            )
        return

    cat_slug = alert.category.slug if alert.category else "all"
    total_count = await alert_repo.count_by_user(db_user.id, category_slug=cat_slug)
    card_text = format_alert_card(alert, current_index=0, total_count=total_count)
    keyboard = get_alert_card_keyboard(
        alert.id,
        alert.status,
        current_page=0,
        total_pages=total_count,
        category_slug=cat_slug,
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(card_text, reply_markup=keyboard)


async def show_list_categories(
    target: Message | CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche le menu de sélection de catégorie pour /list."""
    alert_repo = _get_alert_repo(db_session)
    total_count = await alert_repo.count_by_user(db_user.id)
    if total_count == 0:
        msg = "Aucune alerte active pour le moment. Utilise /add pour en créer une."
        if isinstance(target, CallbackQuery):
            await target.answer()
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(msg)
        else:
            await target.answer(msg)
        return

    cat_repo = _get_category_repo(db_session)
    active_categories = await cat_repo.get_all_active()
    counts = await alert_repo.count_by_user_grouped_by_category(db_user.id)

    text = (
        "📁 <b>Catégories</b>\n\nChoisis une catégorie pour afficher tes alertes :"
    )
    keyboard = get_list_category_selection_keyboard(active_categories, counts, total_count)

    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message and hasattr(target.message, "edit_text"):
            await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


async def display_alerts_list(
    target: Message | CallbackQuery,
    db_user: User,
    db_session: Any,
    category_slug: str = "all",
    page: int = 0,
) -> None:
    """Affiche la liste paginée pour une catégorie donnée."""
    alert_repo = _get_alert_repo(db_session)
    total_count = await alert_repo.count_by_user(db_user.id, category_slug=category_slug)
    if total_count == 0:
        msg = "Aucune alerte trouvée dans cette catégorie."
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📁 Choisir une autre catégorie", callback_data="al_list_cat_menu"
                    )
                ],
                [InlineKeyboardButton(text="❌ Fermer", callback_data="al_close")],
            ]
        )
        if isinstance(target, CallbackQuery):
            await target.answer()
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(msg, reply_markup=reply_markup)
        else:
            await target.answer(msg, reply_markup=reply_markup)
        return

    cat_label = None
    if category_slug and category_slug != "all":
        cat_repo = _get_category_repo(db_session)
        cat_obj = await cat_repo.get_by_slug(category_slug)
        if cat_obj:
            cat_label = f"{cat_obj.emoji} {cat_obj.label}"

    total_pages = max(1, (total_count + 9) // 10)
    page = max(0, min(page, total_pages - 1))
    alerts = await alert_repo.list_by_user(
        db_user.id, limit=10, offset=page * 10, category_slug=category_slug
    )

    text = format_alerts_list_text(
        alerts, current_page=page, total_count=total_count, category_label=cat_label
    )
    keyboard = get_alerts_list_keyboard(
        alerts, current_page=page, total_pages=total_pages, category_slug=category_slug
    )

    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message and hasattr(target.message, "edit_text"):
            await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


@router.message(Command("list"))
async def list_alerts(
    message: Message,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche une vue d'ensemble compacte et paginée des alertes, par catégorie ou globale."""
    args = (message.text or "").strip().split(maxsplit=1)
    if len(args) > 1:
        raw_arg = args[1].lower().strip()
        matched_slug = CATEGORY_ARG_MAP.get(raw_arg)
        if matched_slug:
            await display_alerts_list(
                message, db_user, db_session, category_slug=matched_slug, page=0
            )
            return

    await show_list_categories(message, db_user, db_session)


@router.callback_query(F.data == "al_list_cat_menu")
async def handle_list_category_menu(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Retourne au menu de sélection de catégorie pour /list."""
    await show_list_categories(callback, db_user, db_session)


@router.callback_query(F.data.startswith("al_list_cat:"))
async def handle_select_list_category(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche la liste paginée pour la catégorie sélectionnée."""
    parts = callback.data.split(":")  # type: ignore
    category_slug = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    await display_alerts_list(callback, db_user, db_session, category_slug=category_slug, page=page)


@router.callback_query(F.data.startswith("al_list_page:"))
async def paginate_alerts_list(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Navigue entre les pages de la liste des alertes."""
    parts = callback.data.split(":")  # type: ignore
    if len(parts) >= 3:
        category_slug = parts[1]
        page = int(parts[2])
    else:
        category_slug = "all"
        page = int(parts[1])

    await display_alerts_list(callback, db_user, db_session, category_slug=category_slug, page=page)

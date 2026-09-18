"""Handler pour la commande /stats et ses callbacks interactifs."""

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.stats import get_stats_keyboard
from app.services.statistics_service import StatisticsService

router = Router(name="stats_router")


@router.message(Command("stats"))
async def handle_stats(message: Message, db_session: Any) -> None:
    """Affiche les statistiques d'activité de Radar Bot sur 7 jours par défaut."""
    stats_service = StatisticsService(db_session)
    stats_text = await stats_service.get_formatted_stats(days=7)
    keyboard = get_stats_keyboard(current_days=7)

    await message.answer(
        text=stats_text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("stats:"))
async def handle_stats_callback(callback: CallbackQuery, db_session: Any) -> None:
    """Bascule la période des statistiques (7 jours ou 30 jours)."""
    if not callback.data:
        await callback.answer()
        return

    days_str = callback.data.split(":")[1]
    try:
        days = int(days_str)
    except ValueError:
        days = 7

    stats_service = StatisticsService(db_session)
    stats_text = await stats_service.get_formatted_stats(days=days)
    keyboard = get_stats_keyboard(current_days=days)

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text=stats_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    await callback.answer()

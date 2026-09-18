"""Claviers inline pour la commande /stats."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_stats_keyboard(current_days: int = 7) -> InlineKeyboardMarkup:
    """Retourne le clavier permettant de basculer entre 7 jours et 30 jours."""
    btn_7_text = "🟢 7 jours" if current_days == 7 else "7 jours"
    btn_30_text = "🟢 30 jours" if current_days == 30 else "30 jours"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=btn_7_text, callback_data="stats:7"),
                InlineKeyboardButton(text=btn_30_text, callback_data="stats:30"),
            ]
        ]
    )

"""Claviers inline pour la commande /stats."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.labels import BTN_PERIOD_7, BTN_PERIOD_30, period_button


def get_stats_keyboard(current_days: int = 7) -> InlineKeyboardMarkup:
    """Retourne le clavier permettant de basculer entre 7 jours et 30 jours.

    La période affichée est cochée, l'autre reste neutre : deux pastilles
    identiques ne disent pas laquelle est active.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=period_button(BTN_PERIOD_7, current_days == 7),
                    callback_data="stats:7",
                ),
                InlineKeyboardButton(
                    text=period_button(BTN_PERIOD_30, current_days == 30),
                    callback_data="stats:30",
                ),
            ]
        ]
    )
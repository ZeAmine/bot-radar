"""Clavier inline pour le message d'accueil /start."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_start_keyboard() -> InlineKeyboardMarkup:
    """Retourne le clavier d'accueil avec le bouton 'Créer une alerte'."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Créer une alerte", callback_data="btn_add_alert")]
        ]
    )

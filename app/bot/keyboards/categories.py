"""Claviers de sélection des catégories."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.constants import CATEGORY_EMOJIS, CATEGORY_LABELS, CategorySlug


def get_categories_keyboard(prefix: str = "cat") -> InlineKeyboardMarkup:
    """Retourne le clavier inline des catégories disponibles."""
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{CATEGORY_EMOJIS[slug]} {CATEGORY_LABELS[slug]}",
                callback_data=f"{prefix}:{slug.value}",
            )
        ]
        for slug in CategorySlug
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

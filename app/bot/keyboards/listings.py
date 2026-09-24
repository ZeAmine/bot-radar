"""Claviers Telegram pour les notifications d'annonces."""

import uuid

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.constants import CategorySlug


def get_listing_alert_keyboard(
    listing_id: uuid.UUID,
    listing_url: str,
    category_slug: str,
    is_disliked: bool = False,
) -> InlineKeyboardMarkup:
    """Génère les boutons d'action d'une alerte d'annonce.

    Règles :
    - Ligne 1 : [🖼 Photos] et [🔗 Annonce]
    - Ligne 2 : [📊 Prix du marché] (si BD/Comics)
    - Ligne 3 (Ligne 2 si hors-BD) : [👎 Faux positif] (ou [✅ Signalé]) et [💬 Négocier]
    """
    row1 = [
        InlineKeyboardButton(text="🖼 Photos", callback_data=f"photos:{listing_id}"),
        InlineKeyboardButton(text="🔗 Annonce", url=listing_url),
    ]

    dislike_btn = (
        InlineKeyboardButton(
            text="✅ Signalé", callback_data=f"disliked:{listing_id}"
        )
        if is_disliked
        else InlineKeyboardButton(
            text="👎 Faux positif", callback_data=f"dislike:{listing_id}"
        )
    )

    row_actions = [
        dislike_btn,
        InlineKeyboardButton(text="💬 Négocier", callback_data=f"negotiate:{listing_id}"),
    ]

    rows = [row1]
    if category_slug == CategorySlug.BD_COMICS.value:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📊 Prix du marché", callback_data=f"market:{listing_id}"
                )
            ]
        )
    rows.append(row_actions)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_dislike_confirmation_keyboard(
    listing_id: uuid.UUID,
    listing_url: str,
    category_slug: str,
) -> InlineKeyboardMarkup:
    """Clavier demandant confirmation avant d'enregistrer un faux positif."""
    row1 = [
        InlineKeyboardButton(text="🖼 Photos", callback_data=f"photos:{listing_id}"),
        InlineKeyboardButton(text="🔗 Annonce", url=listing_url),
    ]

    row_confirm = [
        InlineKeyboardButton(
            text="⚠️ Confirmer ?", callback_data=f"confirm_dislike:{listing_id}"
        ),
        InlineKeyboardButton(
            text="❌ Annuler", callback_data=f"cancel_dislike:{listing_id}"
        ),
    ]

    rows = [row1]
    if category_slug == CategorySlug.BD_COMICS.value:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📊 Prix du marché", callback_data=f"market:{listing_id}"
                )
            ]
        )
    rows.append(row_confirm)

    return InlineKeyboardMarkup(inline_keyboard=rows)

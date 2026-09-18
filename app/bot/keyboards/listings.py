"""Claviers Telegram pour les notifications d'annonces."""

import uuid

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.constants import CategorySlug


def get_listing_alert_keyboard(
    listing_id: uuid.UUID,
    listing_url: str,
    category_slug: str,
    checkout_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Génère les boutons d'action d'une alerte d'annonce.

    Règles :
    - Ligne 1 : [🖼 Voir les photos] et [🔗 Voir Vinted]
    - Ligne 2 : [🛍 Payer] (avec #radar_autobuy=1) et optionnellement [📉 Prix du marché] (BD & Comics)
    """
    pay_url = checkout_url or (
        listing_url if listing_url.endswith("#radar_autobuy=1") else f"{listing_url}#radar_autobuy=1"
    )

    row1 = [
        InlineKeyboardButton(text="🖼 Voir les photos", callback_data=f"photos:{listing_id}"),
        InlineKeyboardButton(text="🔗 Voir Vinted", url=listing_url),
    ]

    if category_slug == CategorySlug.BD_COMICS.value:
        row2 = [
            InlineKeyboardButton(text="🛍 Payer", url=pay_url),
            InlineKeyboardButton(text="📉 Prix du marché", callback_data=f"market:{listing_id}"),
        ]
    else:
        row2 = [
            InlineKeyboardButton(text="🛍 Payer", url=pay_url),
        ]

    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])

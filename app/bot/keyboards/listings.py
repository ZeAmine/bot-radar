"""Claviers Telegram pour les notifications d'annonces."""

import re
import uuid

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.constants import CategorySlug


def build_vinted_checkout_url(item_id: str) -> str:
    """Construit l'URL native Vinted menant directement au checkout/paiement."""
    return f"https://www.vinted.fr/transaction/buy/new?transaction[item_id]={item_id}"


def get_listing_alert_keyboard(
    listing_id: uuid.UUID,
    listing_url: str,
    category_slug: str,
    checkout_url: str | None = None,
    external_id: str | None = None,
    is_disliked: bool = False,
) -> InlineKeyboardMarkup:
    """Génère les boutons d'action d'une alerte d'annonce.

    Règles :
    - Ligne 1 : [🖼 Photos] et [🔗 Annonce]
    - Ligne 2 : [📊 Prix du marché] (si BD/Comics) et [🛍 Payer]
    - Ligne 3 : [👎 Faux positif] et [💬 Négocier]
    """
    if checkout_url:
        pay_url = checkout_url
    elif external_id:
        pay_url = build_vinted_checkout_url(external_id)
    else:
        # Tente d'extraire l'identifiant depuis l'URL de l'annonce (ex: /items/12345-titre)
        match = re.search(r"/items/(\d+)", listing_url)
        pay_url = build_vinted_checkout_url(match.group(1)) if match else listing_url

    row1 = [
        InlineKeyboardButton(text="🖼 Photos", callback_data=f"photos:{listing_id}"),
        InlineKeyboardButton(text="🔗 Annonce", url=listing_url),
    ]

    if category_slug == CategorySlug.BD_COMICS.value:
        row2 = [
            InlineKeyboardButton(
                text="📊 Prix du marché", callback_data=f"market:{listing_id}"
            ),
            InlineKeyboardButton(text="💳 Payer", url=pay_url),
        ]
    else:
        row2 = [
            InlineKeyboardButton(text="💳 Payer", url=pay_url),
        ]

    dislike_btn = (
        InlineKeyboardButton(
            text="✅ Signalé faux positif", callback_data=f"disliked:{listing_id}"
        )
        if is_disliked
        else InlineKeyboardButton(
            text="👎 Faux positif", callback_data=f"dislike:{listing_id}"
        )
    )

    row3 = [
        dislike_btn,
        InlineKeyboardButton(text="💬 Négocier", callback_data=f"negotiate:{listing_id}"),
    ]

    row4 = [
        InlineKeyboardButton(
            text="🛍️ Dressing / Lot (bientôt)", callback_data="bundle_dressing_disabled"
        ),
    ]

    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3, row4])


def get_dislike_confirmation_keyboard(
    listing_id: uuid.UUID,
    listing_url: str,
    category_slug: str,
    checkout_url: str | None = None,
    external_id: str | None = None,
) -> InlineKeyboardMarkup:
    """Clavier demandant confirmation avant d'enregistrer un faux positif."""
    if checkout_url:
        pay_url = checkout_url
    elif external_id:
        pay_url = build_vinted_checkout_url(external_id)
    else:
        match = re.search(r"/items/(\d+)", listing_url)
        pay_url = build_vinted_checkout_url(match.group(1)) if match else listing_url

    row1 = [
        InlineKeyboardButton(text="🖼 Photos", callback_data=f"photos:{listing_id}"),
        InlineKeyboardButton(text="🔗 Annonce", url=listing_url),
    ]

    if category_slug == CategorySlug.BD_COMICS.value:
        row2 = [
            InlineKeyboardButton(
                text="📊 Prix du marché", callback_data=f"market:{listing_id}"
            ),
            InlineKeyboardButton(text="💳 Payer", url=pay_url),
        ]
    else:
        row2 = [
            InlineKeyboardButton(text="💳 Payer", url=pay_url),
        ]

    row3 = [
        InlineKeyboardButton(
            text="⚠️ Confirmer ?", callback_data=f"confirm_dislike:{listing_id}"
        ),
        InlineKeyboardButton(
            text="❌ Annuler", callback_data=f"cancel_dislike:{listing_id}"
        ),
    ]

    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])

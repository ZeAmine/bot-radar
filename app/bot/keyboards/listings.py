"""Claviers Telegram pour les notifications d'annonces."""

import uuid

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.labels import (
    BTN_BUY,
    BTN_CANCEL,
    BTN_CONFIRM,
    BTN_REPORTED,
    BTN_VIEW_LISTING,
)
from app.core.config import settings
from app.core.constants import CategorySlug
from app.db.models.listing import Listing


def get_listing_alert_keyboard(
    listing_id: uuid.UUID,
    listing_url: str,
    category_slug: str,
    is_disliked: bool = False,
    *,
    checkout_enabled: bool = False,
) -> InlineKeyboardMarkup:
    """Génère les boutons d'action d'une alerte d'annonce.

    Règles :
    - Ligne 1 : [Acheter] et [Voir l'annonce] quand la préparation du checkout
      est activée ; [Voir l'annonce] seule sinon.
    - Ligne 2 : [Prix du marché] (si BD/Comics)
    - Ligne 3 (Ligne 2 si hors-BD) : [Faux positif] (ou [Signalé]) et [Négocier]

    « Acheter » est un **callback** : rien n'est préparé à l'envoi, le checkout
    n'est tenté qu'au clic (invariant n°1 intact). « Voir l'annonce » reste
    l'accès sans automatisation et le repli si la préparation échoue.
    """
    row1 = [InlineKeyboardButton(text=BTN_VIEW_LISTING, url=listing_url)]
    if checkout_enabled:
        row1.insert(
            0,
            InlineKeyboardButton(text=BTN_BUY, callback_data=f"buy:{listing_id}"),
        )

    dislike_btn = (
        InlineKeyboardButton(text=BTN_REPORTED, callback_data=f"disliked:{listing_id}")
        if is_disliked
        else InlineKeyboardButton(text="Faux positif", callback_data=f"dislike:{listing_id}")
    )

    row_actions = [
        dislike_btn,
        InlineKeyboardButton(text="Négocier", callback_data=f"negotiate:{listing_id}"),
    ]

    rows = [row1]
    if category_slug == CategorySlug.BD_COMICS.value:
        rows.append(
            [InlineKeyboardButton(text="Prix du marché", callback_data=f"market:{listing_id}")]
        )
    rows.append(row_actions)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def listing_alert_keyboard(
    listing: Listing,
    is_disliked: bool = False,
) -> InlineKeyboardMarkup:
    """Adaptateur : l'annonce → le clavier, avec l'interrupteur appliqué une fois.

    Les trois appelants (envoi d'alerte, retour d'un faux positif, annulation)
    passent par ici : la forme du bouton « Acheter » ne peut pas diverger entre
    eux, et l'interrupteur n'est lu qu'à un seul endroit.
    """
    return get_listing_alert_keyboard(
        listing_id=listing.id,
        listing_url=listing.url,
        category_slug=listing.category_slug,
        is_disliked=is_disliked,
        checkout_enabled=settings.VINTED_CHECKOUT_PREPARATION_ENABLED,
    )


def get_dislike_confirmation_keyboard(
    listing_id: uuid.UUID,
    listing_url: str,
    category_slug: str,
) -> InlineKeyboardMarkup:
    """Clavier demandant confirmation avant d'enregistrer un faux positif.

    Le clavier remplace les actions de l'annonce, sans réécrire sa légende :
    « Voir l'annonce » suffit le temps de la confirmation.
    """
    row1 = [InlineKeyboardButton(text=BTN_VIEW_LISTING, url=listing_url)]

    row_confirm = [
        InlineKeyboardButton(text=BTN_CONFIRM, callback_data=f"confirm_dislike:{listing_id}"),
        InlineKeyboardButton(text=BTN_CANCEL, callback_data=f"cancel_dislike:{listing_id}"),
    ]

    rows = [row1]
    if category_slug == CategorySlug.BD_COMICS.value:
        rows.append(
            [InlineKeyboardButton(text="Prix du marché", callback_data=f"market:{listing_id}")]
        )
    rows.append(row_confirm)

    return InlineKeyboardMarkup(inline_keyboard=rows)

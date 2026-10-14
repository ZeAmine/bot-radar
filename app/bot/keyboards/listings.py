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

# Marqueur d'intention d'achat assisté, porté par le **fragment** de l'URL de
# l'annonce : les fragments ne partent jamais au serveur Vinted, et ce marqueur
# n'a d'effet que si le userscript est actif dans le navigateur de l'utilisateur.
# Les deux artefacts (ce module, `scripts/vinted_checkout_assist.user.js`)
# doivent s'accorder : un test lit le script et compare cette constante.
CHECKOUT_ASSIST_MARKER = "botradar-acheter"


def assisted_checkout_url(
    listing_url: str,
    external_id: str,
    source_name: str,
) -> str | None:
    """URL de l'annonce portant l'intention d'achat assisté, ou `None`.

    Le fragment porte aussi l'identifiant de l'article : le userscript vérifie
    que la page ouverte est bien celle demandée avant le moindre clic. Un
    fragment déjà présent est remplacé, jamais concaténé.
    """
    if source_name != "vinted" or not external_id.isdigit():
        return None
    base = listing_url.split("#", 1)[0]
    return f"{base}#{CHECKOUT_ASSIST_MARKER}={external_id}"


def get_listing_alert_keyboard(
    listing_id: uuid.UUID,
    listing_url: str,
    category_slug: str,
    is_disliked: bool = False,
    *,
    checkout_enabled: bool = False,
    assisted_checkout_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Génère les boutons d'action d'une alerte d'annonce.

    Règles :
    - Ligne 1 : [Acheter] et [Voir l'annonce] quand une action d'achat existe ;
      [Voir l'annonce] seule sinon.
    - Ligne 2 : [Prix du marché] (si BD/Comics)
    - Ligne 3 (Ligne 2 si hors-BD) : [Faux positif] (ou [Signalé]) et [Négocier]

    « Acheter » a deux formes, jamais les deux à la fois :
    - `checkout_enabled` (préparation serveur, `VINTED_CHECKOUT_PREPARATION_ENABLED`)
      → un **callback** : rien n'est préparé à l'envoi, le checkout n'est tenté
      qu'au clic (invariant n°1 intact) ;
    - `assisted_checkout_url` → un **lien** vers l'annonce, porteur du marqueur
      d'intention : c'est le navigateur de l'utilisateur, avec son userscript,
      qui déclenche le bouton natif « Acheter » une seule fois. Le serveur ne
      fait alors aucun appel à Vinted.

    « Voir l'annonce » reste l'accès sans automatisation, dans les deux cas.
    """
    row1: list[InlineKeyboardButton] = []
    if checkout_enabled:
        row1.append(
            InlineKeyboardButton(text=BTN_BUY, callback_data=f"buy:{listing_id}")
        )
    elif assisted_checkout_url:
        row1.append(InlineKeyboardButton(text=BTN_BUY, url=assisted_checkout_url))
    row1.append(InlineKeyboardButton(text=BTN_VIEW_LISTING, url=listing_url))

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
    """Adaptateur : l'annonce → le clavier, avec l'action d'achat configurée.

    Un seul endroit décide quelle forme prend « Acheter » : le flag serveur
    (`checkout_enabled`) prime ; sinon le lien assisté est proposé, et jamais
    pour une source non Vinted. Les trois appelants (envoi d'alerte, retour
    d'un faux positif, annulation) passent par ici pour ne pas diverger.
    """
    return get_listing_alert_keyboard(
        listing_id=listing.id,
        listing_url=listing.url,
        category_slug=listing.category_slug,
        is_disliked=is_disliked,
        checkout_enabled=settings.VINTED_CHECKOUT_PREPARATION_ENABLED,
        assisted_checkout_url=assisted_checkout_url(
            listing.url, listing.external_id, listing.source_name
        ),
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

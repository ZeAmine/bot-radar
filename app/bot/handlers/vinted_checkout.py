"""Handler du bouton « Acheter » : prépare le checkout Vinted à la demande.

Le clic ne fait rien d'autre que demander une préparation : aucune opération
n'existe à l'avance, et l'alerte initiale n'a subi aucun appel réseau
supplémentaire (invariant n°1). En cas d'échec, « Voir l'annonce » reste le
repli fonctionnel, et aucune relance automatique n'est tentée.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.handlers.alerts_helpers import parse_listing_id
from app.bot.labels import BTN_CONTINUE_CHECKOUT, BTN_VIEW_LISTING, escape_html
from app.domain.enums import VintedCheckoutStatus
from app.use_cases.prepare_vinted_checkout import (
    CheckoutPreparation,
    PrepareVintedCheckoutUseCase,
)

logger = logging.getLogger(__name__)

router = Router(name="vinted_checkout_router")

BUY_CALLBACK_PREFIX = "buy:"
PENDING_ANSWER = "Préparation du checkout Vinted…"

READY_TITLE = "🛒 <b>Checkout Vinted prêt</b>"

READY_FOOTER = (
    "<i>Choisis ton point relais et confirme le paiement dans Vinted : "
    "le paiement reste entièrement manuel.</i>"
)

# Un message par cause, compréhensible sans journal : « Voir l'annonce » suit
# dans le même clavier quand une annonce est connue.
REFUSAL_TEXTS: dict[VintedCheckoutStatus, str] = {
    VintedCheckoutStatus.BUSY: (
        "⏳ Une préparation est déjà en cours. Laisse-la se terminer, puis réessaie."
    ),
    VintedCheckoutStatus.PAUSED: (
        "⏸ La création de checkout est **coupée** après un échec. "
        "Réessaie plus tard, ou continue depuis l'annonce."
    ),
    VintedCheckoutStatus.DISABLED: (
        "La préparation du checkout est désactivée dans la configuration du bot."
    ),
    VintedCheckoutStatus.NOT_CONFIGURED: (
        "Aucune session Vinted n'est configurée pour préparer le checkout. "
        "Renseigne-la localement, puis réessaie."
    ),
    VintedCheckoutStatus.NOT_ASSOCIATED: (
        "Cette annonce n'est pas associée à tes alertes."
    ),
    VintedCheckoutStatus.SELLER_UNKNOWN: (
        "Vendeur introuvable pour cette annonce : elle ne porte pas son "
        "identifiant vendeur et la page ne le fournit pas. Continue depuis l'annonce."
    ),
    VintedCheckoutStatus.UNAVAILABLE: (
        "L'article n'est plus disponible (probablement vendu). "
        "Continue depuis l'annonce."
    ),
    VintedCheckoutStatus.AUTH_REQUIRED: (
        "La session Vinted n'est pas acceptée pour créer la transaction. "
        "Recapture **tous** les cookies (y compris les cookies de session "
        "HttpOnly), réinjecte-la, puis réessaie."
    ),
    VintedCheckoutStatus.BLOCKED: (
        "Vinted a demandé une vérification anti-robot. "
        "Termine l'achat depuis l'annonce ouverte sur ton téléphone."
    ),
    VintedCheckoutStatus.RATE_LIMITED: (
        "Vinted limite temporairement les demandes. Réessaie plus tard, "
        "ou passe par l'annonce."
    ),
    VintedCheckoutStatus.TIMEOUT: (
        "Vinted n'a pas répondu à temps. L'opération est peut-être en cours côté "
        "Vinted : vérifie tes messages avant de réessayer."
    ),
    VintedCheckoutStatus.NETWORK: (
        "Connexion à Vinted impossible. L'opération est peut-être en cours côté "
        "Vinted : vérifie tes messages avant de réessayer."
    ),
    VintedCheckoutStatus.UNEXPECTED: (
        "Réponse inattendue de Vinted. Passe par l'annonce, ou réessaie plus tard."
    ),
}


def _fallback_keyboard(listing_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=BTN_VIEW_LISTING, url=listing_url)]]
    )


def _ready_keyboard(checkout_url: str, listing_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_CONTINUE_CHECKOUT, url=checkout_url)],
            [InlineKeyboardButton(text=BTN_VIEW_LISTING, url=listing_url)],
        ]
    )


def format_preparation_result(
    preparation: CheckoutPreparation,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Texte et clavier de la réponse au clic, selon l'issue de la préparation.

    Une issue réussie rend le lien de checkout et garde « Voir l'annonce ».
    Toute autre issue conserve ce repli quand l'annonce est connue — l'échec
    d'une préparation ne doit jamais couper l'accès à l'annonce.
    """
    if preparation.status in (
        VintedCheckoutStatus.READY,
        VintedCheckoutStatus.ALREADY_READY,
    ) and preparation.checkout_url and preparation.listing_url:
        title = escape_html(preparation.listing_title or "")
        lines = [READY_TITLE]
        if title:
            lines.append(title)
        lines.extend(["", READY_FOOTER])
        return "\n".join(lines), _ready_keyboard(
            preparation.checkout_url, preparation.listing_url
        )

    text = REFUSAL_TEXTS.get(
        preparation.status,
        "La préparation du checkout est indisponible pour le moment.",
    )
    if preparation.status is VintedCheckoutStatus.PAUSED and preparation.retry_after_seconds:
        minutes = max(1, round(preparation.retry_after_seconds / 60))
        text += f"\n\n<i>Coupure encore active environ {minutes} min.</i>"
    if preparation.listing_url:
        return text, _fallback_keyboard(preparation.listing_url)
    return text, None


@router.callback_query(F.data.startswith(BUY_CALLBACK_PREFIX))
async def handle_buy_listing(callback: CallbackQuery, db_session: Any) -> None:
    """Prépare le checkout de l'annonce cliquée, uniquement à la demande.

    Deux taps rapprochés peuvent arriver dans deux updates distincts traités en
    parallèle : la déduplication est portée par le verrou Redis du cas d'usage
    (acquisition atomique `SET NX`), seule garantie partagée — aiogram n'offre
    pas de verrou de session générique, et un verrou en mémoire ne couvrirait
    pas le second clic traité sur une autre coroutine.
    """
    listing_id: uuid.UUID | None = await parse_listing_id(callback)
    if listing_id is None:
        return

    with contextlib.suppress(Exception):
        await callback.answer(PENDING_ANSWER)

    if callback.from_user is None:
        return
    try:
        preparation = await PrepareVintedCheckoutUseCase(db_session).execute(
            telegram_user_id=callback.from_user.id,
            listing_id=listing_id,
        )
    except Exception:
        logger.exception(
            "Préparation de checkout Vinted interrompue pour l'annonce %s.", listing_id
        )
        preparation = CheckoutPreparation(VintedCheckoutStatus.UNEXPECTED)
    if preparation.status in (
        VintedCheckoutStatus.READY,
        VintedCheckoutStatus.ALREADY_READY,
    ):
        logger.info("Checkout Vinted préparé pour l'annonce %s.", listing_id)
    text, keyboard = format_preparation_result(preparation)

    if isinstance(callback.message, Message):
        await callback.message.reply(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

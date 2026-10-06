import base64
import contextlib
import logging
import re
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.keyboards.listings import get_dislike_confirmation_keyboard
from app.bot.labels import BTN_CLOSE, escape_html
from app.db.models.listing import Listing
from app.db.repositories.autotune_repo import record_listing_dislike
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.listing_repo import ListingRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.services.ai.modules.negotiation import negotiation_module
from app.services.market_price_service import (
    MarketPriceService,
    clean_tome_display_title,
)
from app.use_cases.view_market_details import ViewMarketDetailsUseCase

logger = logging.getLogger(__name__)
router = Router(name="callbacks_router")


async def _market_error_response(
    callback: CallbackQuery,
    exc: Exception,
    *,
    context: str,
    message: str,
) -> None:
    """Répond à une erreur de panneau de marché, sans la propager à Telegram.

    Le panneau est déjà à l'écran quand le clic échoue : laisser
    l'exception remonter ferait rejeter le message entier par
    Telegram. On journalise pour l'exploitation, on prévient
    l'utilisateur, et on n'échoue jamais sur la réponse elle-même.
    """
    logger.error("%s: %s", context, exc, exc_info=True)
    with contextlib.suppress(Exception):
        await callback.answer(message, show_alert=True)


async def _parse_listing_id(callback: CallbackQuery) -> uuid.UUID | None:
    """UUID d'annonce porté par le callback, ou ``None`` — l'erreur est annoncée.

    Quatre boutons lisent ce même identifiant ; la réponse « Annonce
    invalide » est la même partout, elle vit donc ici.
    """
    raw_id = callback.data.split(":", 1)[1] if callback.data else ""
    try:
        return uuid.UUID(raw_id)
    except (IndexError, ValueError):
        await callback.answer("Annonce invalide.", show_alert=True)
        return None


async def _listing_or_answer(
    callback: CallbackQuery,
    db_session: Any,
    listing_id: uuid.UUID,
) -> Listing | None:
    """Annonce désignée, ou ``None`` — l'annonce introuvable est annoncée."""
    listing = await ListingRepository(db_session).get_by_id(listing_id)
    if listing is None:
        await callback.answer("Annonce introuvable.", show_alert=True)
    return listing

# Préfixe des boutons par tome du panneau « 📊 Prix du marché ». Telegram refuse
# un `callback_data` de plus de 64 octets — une contrainte de l'API, pas une
# préférence — et rejette alors le message entier avec BUTTON_DATA_INVALID : deux
# UUID en texte clair en faisaient 85, si bien que le panneau ne s'affichait plus
# du tout, précisément quand le tome n'était pas identifié, seul cas où ces
# boutons existent. Les deux identifiants passent donc en base64url sans
# remplissage : 57 octets, et le préfixe reste lisible.
TOME_REF_PREFIX = "market_tome:"


def _encode_uuid(value: uuid.UUID) -> str:
    """UUID en base64url sans remplissage (22 caractères)."""
    return base64.urlsafe_b64encode(value.bytes).decode("ascii").rstrip("=")


def _decode_uuid(raw: str) -> uuid.UUID:
    """Relit un UUID encodé par `_encode_uuid`, ou lève si la donnée est corrompue."""
    return uuid.UUID(bytes=base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def _encode_tome_ref(listing_id: uuid.UUID, book_id: uuid.UUID) -> str:
    """Référence de callback tenant dans la limite Telegram, et réversible."""
    return f"{TOME_REF_PREFIX}{_encode_uuid(listing_id)}:{_encode_uuid(book_id)}"


def _decode_tome_ref(data: str | None) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Relit la référence de bouton, ou ``None`` si elle ne se décode pas.

    Un callback tronqué ou forgé ne doit pas faire lever une exception : le
    panneau reste fermé et l'utilisateur reçoit le même avertissement que pour
    une annonce inconnue.
    """
    raw = data or ""
    if not raw.startswith(TOME_REF_PREFIX):
        return None
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    try:
        # ``binascii.Error`` (``ValueError``) et ``uuid.UUID`` lèvent toutes deux ici.
        return _decode_uuid(parts[1]), _decode_uuid(parts[2])
    except ValueError:
        return None


@router.callback_query(F.data.startswith("market:"))
@router.callback_query(F.data.startswith("market_details:"))
async def handle_view_market_details(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Affiche le panneau détaillé du prix du marché pour l'annonce BD sélectionnée.

    `market_details:` est le retour « Tous les tomes » : il réaffiche la liste des
    tomes en éditant le message en place. Sans ce filtre, le bouton du panneau de
    série ne routait vers aucun handler et restait mort.
    """
    try:
        await _show_market_details(callback, db_session)
    except Exception as exc:
        await _market_error_response(
            callback,
            exc,
            context="Erreur lors de l'affichage des détails du marché",
            message="Impossible de charger les cotes du marché pour le moment.",
        )


async def _show_market_details(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    listing_id = await _parse_listing_id(callback)
    if listing_id is None:
        return

    # La résolution — alerte, ISBN, tome, catalogue, cotation — vit dans le cas
    # d'usage : ce handler ne fait que présenter ce qu'elle renvoie.
    panel = await ViewMarketDetailsUseCase(db_session).execute(listing_id)
    if panel is None:
        await callback.answer("Annonce introuvable.", show_alert=True)
        return

    listing = panel.listing
    book = panel.book
    books_in_series = panel.books_in_series

    details_text = MarketPriceService.format_market_details_card(
        listing=listing,
        book=book,
        books_in_series=books_in_series,
        tome_result=panel.tome_result,
        latest_snapshot=panel.latest_snapshot,
    )
    tome_buttons: list[list[InlineKeyboardButton]] = []
    if not book and books_in_series and len(books_in_series) > 1:
        current_row: list[InlineKeyboardButton] = []
        for b in books_in_series:
            t_label = clean_tome_display_title(b.title)
            m = re.search(r"\b(?:tome|t|vol)\s*\.?\s*(\d+)\b", t_label, re.IGNORECASE)
            btn_text = f"T{m.group(1)}" if m else t_label[:8]
            current_row.append(
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=_encode_tome_ref(listing.id, b.id),
                )
            )
            if len(current_row) == 5:
                tome_buttons.append(current_row)
                current_row = []
        if current_row:
            tome_buttons.append(current_row)

    tome_buttons.append([InlineKeyboardButton(text=BTN_CLOSE, callback_data="close_market_panel")])
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=tome_buttons)

    if isinstance(callback.message, Message):
        is_market_view = "<b>Prix du marché</b>" in (callback.message.html_text or "")
        if (callback.data or "").startswith("market_details:") and is_market_view:
            await callback.message.edit_text(
                text=details_text,
                reply_markup=back_keyboard,
                disable_web_page_preview=True,
            )
        else:
            await callback.message.reply(
                text=details_text,
                reply_markup=back_keyboard,
                disable_web_page_preview=True,
            )
    await callback.answer()


@router.callback_query(F.data.startswith("market_tome:"))
async def handle_market_tome_select(callback: CallbackQuery, db_session: Any) -> None:
    """Affiche la comparaison pour le tome sélectionné depuis le panneau de série."""
    try:
        await _select_market_tome(callback, db_session)
    except Exception as exc:
        await _market_error_response(
            callback,
            exc,
            context="Erreur lors de la sélection du tome",
            message="Impossible de charger les cotes pour ce tome.",
        )


async def _select_market_tome(callback: CallbackQuery, db_session: Any) -> None:
    decoded = _decode_tome_ref(callback.data)
    if decoded is None:
        await callback.answer("Annonce invalide.", show_alert=True)
        return
    listing_id, book_id = decoded

    listing_repo = ListingRepository(db_session)
    listing = await listing_repo.get_by_id(listing_id)
    if not listing:
        await callback.answer("Annonce introuvable.", show_alert=True)
        return

    book_repo = BookRepository(db_session)
    book = await book_repo.get_by_id(book_id)
    if not book:
        await callback.answer("Livre introuvable.", show_alert=True)
        return

    books_in_series = None
    alert = await listing_repo.get_alert_for_listing(listing.id)
    if alert:
        books_in_series = await book_repo.get_all_books_for_alert(alert.id)

    latest_snap = None
    if book:
        market_repo = MarketPriceRepository(db_session)
        latest_snap = await market_repo.get_latest_snapshot(book.id)

    details_text = MarketPriceService.format_market_details_card(
        listing=listing,
        book=book,
        books_in_series=books_in_series,
        tome_result=None,
        latest_snapshot=latest_snap,
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Tous les tomes",
                    callback_data=f"market_details:{listing.id}",
                ),
                InlineKeyboardButton(text=BTN_CLOSE, callback_data="close_market_panel"),
            ]
        ]
    )

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text=details_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data == "close_market_panel")
async def handle_close_market_panel(callback: CallbackQuery) -> None:
    """Supprime le message de détail du marché."""
    if isinstance(callback.message, Message):
        await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data.startswith("negotiate:"))
async def handle_negotiate(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Génère une offre de négociation courtoise prête à copier en 1 tap."""
    listing_id = await _parse_listing_id(callback)
    if listing_id is None:
        return

    listing = await _listing_or_answer(callback, db_session, listing_id)
    if listing is None:
        return

    await callback.answer("⏳ Coach IA : Analyse de l'offre en cours...")
    if isinstance(callback.message, Message) and callback.message.bot:
        with contextlib.suppress(Exception):
            await callback.message.bot.send_chat_action(
                chat_id=callback.message.chat.id, action="typing"
            )

    result = await negotiation_module.generate_offer(listing)
    if not result:
        if isinstance(callback.message, Message):
            await callback.message.reply("❌ Impossible de calculer une offre pour cette annonce.")
        return

    text = negotiation_module.format_telegram_html(result)
    close_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_CLOSE, callback_data="close_market_panel")]
        ]
    )

    if isinstance(callback.message, Message):
        await callback.message.reply(
            text=text,
            reply_markup=close_keyboard,
        )


@router.callback_query(F.data.startswith("dislike:"))
async def handle_dislike(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Affiche le clavier de confirmation avant d'enregistrer le faux positif."""
    listing_id = await _parse_listing_id(callback)
    if listing_id is None:
        return

    listing = await _listing_or_answer(callback, db_session, listing_id)
    if listing is None:
        return

    confirm_kb = get_dislike_confirmation_keyboard(
        listing_id=listing.id,
        listing_url=listing.url,
        category_slug=listing.category_slug,
    )
    if isinstance(callback.message, Message):
        # La confirmation est un message à part, en réponse à l'annonce.
        #
        # Réutiliser le message d'annonce serait plus court : on y poserait le
        # clavier, et son texte porterait la question. Mais ce message est la
        # trace de l'annonce — son prix, sa cote, ses mots-clés — et cette
        # légende n'est pas reconstructible ici : la cote et les mots-clés
        # viennent de l'ingestion, pas du handler. Un « Annuler » ou un
        # « Confirmer » laisserait donc l'écran bloqué sur la question, sans
        # prix à jamais. Un message séparé se remplace, se supprime, et
        # n'abîme rien : c'est le clavier du message d'annonce qui reste
        # intact, avec son texte d'origine.
        confirmation_text = (
            f"Signaler cette annonce comme faux positif ?\n\n"
            f"{escape_html(listing.title)}\n"
            "Elle sera ignorée pour cette alerte."
        )
        with contextlib.suppress(Exception):
            await callback.message.reply(
                text=confirmation_text,
                reply_markup=confirm_kb,
                disable_web_page_preview=True,
            )

    await callback.answer("Confirmer le faux positif ?")


@router.callback_query(F.data.startswith("confirm_dislike:"))
async def handle_confirm_dislike(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Enregistre définitivement le faux positif après confirmation."""
    listing_id = await _parse_listing_id(callback)
    if listing_id is None:
        return

    listing_repo = ListingRepository(db_session)
    listing = await listing_repo.get_by_id(listing_id)
    alert = await listing_repo.get_alert_for_listing(listing_id)

    await record_listing_dislike(
        db_session,
        listing_id=listing_id,
        alert_id=alert.id if alert else None,
        chat_id=callback.from_user.id if callback.from_user else None,
    )
    await db_session.commit()

    if listing and isinstance(callback.message, Message):
        # Le message de confirmation se termine ici : on le supprime pour ne pas
        # laisser une question sans réponse au-dessus du fil. Le message
        # d'annonce, lui, garde son texte et son clavier.
        with contextlib.suppress(Exception):
            await callback.message.delete()

    await callback.answer(
        "👎 Faux positif enregistré ! L'IA l'utilisera pour auto-nettoyer vos alertes.",
        show_alert=True,
    )

    if alert and callback.from_user and callback.bot:
        from app.tasks.proactive_autotune import schedule_proactive_autotune_check

        schedule_proactive_autotune_check(
            alert_id=alert.id,
            bot=callback.bot,
            chat_id=callback.from_user.id,
        )


@router.callback_query(F.data.startswith("cancel_dislike:"))
async def handle_cancel_dislike(callback: CallbackQuery) -> None:
    """Annule la demande de faux positif et referme l'écran de confirmation.

    Le message d'annonce n'a jamais été modifié : il n'y a rien à restaurer.
    Seul le message de confirmation est supprimé. Le paramètre `db_session` a
    disparu avec la lecture qui servait à reconstruire le clavier — l'injection
    par le routeur reste tolérante, une signature plus courte ne casse rien.
    """
    listing_id = await _parse_listing_id(callback)
    if listing_id is None:
        return

    if isinstance(callback.message, Message):
        # Rien à restaurer : l'annonce n'a jamais été modifiée. Le message de
        # confirmation disparaît, ce qui laisse le fil dans l'état où
        # l'utilisateur était avant son appui.
        with contextlib.suppress(Exception):
            await callback.message.delete()

    await callback.answer("Annulé.")


@router.callback_query(F.data.startswith("disliked:"))
async def handle_already_disliked(callback: CallbackQuery) -> None:
    """Informe que cette annonce a déjà été marquée comme faux positif."""
    await callback.answer("Cette annonce a déjà été signalée comme faux positif.", show_alert=True)


@router.callback_query(F.data == "noop")
async def handle_noop_callback(callback: CallbackQuery) -> None:
    """Accusé de réception silencieux pour les boutons de statut (ex: '⏳ En cours...')."""
    await callback.answer()



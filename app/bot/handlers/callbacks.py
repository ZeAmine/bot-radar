import base64
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
from app.bot.keyboards.listings import (
    get_dislike_confirmation_keyboard,
    listing_alert_keyboard,
)
from app.bot.labels import BTN_CLOSE
from app.db.models.book import Book
from app.db.models.listing import Listing
from app.db.repositories.autotune_repo import record_listing_dislike
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.listing_repo import ListingRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.domain.rules.catalogue_search import is_uncatalogued
from app.services.ai.modules.negotiation import negotiation_module
from app.services.market_price_service import (
    MarketPriceService,
    clean_tome_display_title,
    extract_tome_number_from_title,
    listable_series_books,
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


def _tome_selection_rows(
    listing_id: uuid.UUID,
    books: list[Book],
) -> list[list[InlineKeyboardButton]]:
    """Rangées de boutons « choisir un tome » — une seule implémentation.

    La série présente ses tomes **dès qu'il y en a plus d'un**, quel que soit le
    panneau qui les affiche : le tome que le texte de l'annonce a désigné est un
    point de départ, pas un cul-de-sac. Cinq boutons par rangée : au-delà,
    Telegram refuse la ligne.
    """
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for book in sorted(
        listable_series_books(books),
        key=lambda b: (extract_tome_number_from_title(b.title) or 999, b.title),
    ):
        number = extract_tome_number_from_title(book.title)
        btn_text = f"T{number}" if number else clean_tome_display_title(book.title)[:20]
        current_row.append(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=_encode_tome_ref(listing_id, book.id),
            )
        )
        if len(current_row) == 5:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    return rows


@router.callback_query(F.data.startswith("market:"))
@router.callback_query(F.data.startswith("market_details:"))
async def handle_view_market_details(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Affiche le panneau détaillé du prix du marché pour l'annonce BD sélectionnée.

    `market_details:` reste routé pour les boutons des anciens messages ; les
    nouveaux panneaux ne proposent que les boutons individuels des tomes.
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
    if (callback.data or "").startswith("market_details:"):
        from app.bot.handlers.market_panel import select_panel_tome

        if await select_panel_tome(callback, None):
            return
    listing_id = await parse_listing_id(callback)
    if listing_id is None:
        return

    # La résolution — alerte, ISBN, tome, catalogue, cotation — vit dans le cas
    # d'usage : ce handler ne fait que présenter ce qu'elle renvoie.
    panel = await ViewMarketDetailsUseCase(db_session).execute(listing_id, refresh=False)
    if panel is None:
        await callback.answer("Annonce introuvable.", show_alert=True)
        return
    if (callback.data or "").startswith("market:"):
        from app.bot.handlers.market_panel import open_panel

        await callback.answer()
        await open_panel(callback, panel)
        return

    listing = panel.listing
    if listing is None:
        return
    book = panel.book
    books_in_series = panel.books_in_series

    details_text = MarketPriceService.format_market_details_card(
        listing=listing,
        book=book,
        books_in_series=books_in_series,
        tome_result=panel.tome_result,
        latest_snapshot=panel.latest_snapshot,
        series_total_cents=panel.series_total_cents,
        catalogue_exhausted=panel.catalogue_exhausted,
    )
    # Les tomes se choisissent depuis n'importe quel panneau de la série, résolu
    # ou non : condition unique `len(books_in_series) > 1` (voir helper).
    tome_buttons: list[list[InlineKeyboardButton]] = []
    if books_in_series and len(books_in_series) > 1:
        tome_buttons = _tome_selection_rows(listing.id, books_in_series)

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
    from app.bot.handlers.market_panel import select_panel_tome

    if await select_panel_tome(callback, book_id):
        return

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
        series_total_cents=alert.series_total_cents if alert else None,
        catalogue_exhausted=is_uncatalogued(alert) if alert else False,
    )

    # Depuis un tome, les autres tomes restent à portée de pouce : la sélection
    # n'est jamais un cul-de-sac (condition unique `len(books_in_series) > 1`).
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    if books_in_series and len(books_in_series) > 1:
        keyboard_rows = _tome_selection_rows(listing.id, books_in_series)
    keyboard_rows.append([InlineKeyboardButton(text=BTN_CLOSE, callback_data="close_market_panel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

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
        from app.bot.handlers.market_panel import close_panel

        close_panel(callback.message)
        await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data.startswith("negotiate:"))
async def handle_negotiate(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Génère une offre de négociation courtoise prête à copier en 1 tap."""
    listing_id = await parse_listing_id(callback)
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
    listing_id = await parse_listing_id(callback)
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
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=confirm_kb)

    await callback.answer("Confirmer le faux positif ?")


@router.callback_query(F.data.startswith("confirm_dislike:"))
async def handle_confirm_dislike(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Enregistre définitivement le faux positif après confirmation."""
    listing_id = await parse_listing_id(callback)
    if listing_id is None:
        return

    listing_repo = ListingRepository(db_session)
    listing = await listing_repo.get_by_id(listing_id)
    alert = await listing_repo.get_alert_for_listing(listing_id)

    if listing is None:
        await callback.answer("Annonce introuvable.")
        return

    recorded = await record_listing_dislike(
        db_session,
        listing_id=listing_id,
        alert_id=alert.id if alert else None,
        chat_id=callback.from_user.id if callback.from_user else None,
    )
    await db_session.commit()

    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=listing_alert_keyboard(
            listing, is_disliked=True,
        ))
    await callback.answer("Faux positif enregistré.")

    if recorded and alert and callback.from_user and callback.bot:
        from app.tasks.proactive_autotune import schedule_proactive_autotune_check

        schedule_proactive_autotune_check(
            alert_id=alert.id,
            bot=callback.bot,
            chat_id=callback.from_user.id,
        )


@router.callback_query(F.data.startswith("cancel_dislike:"))
async def handle_cancel_dislike(callback: CallbackQuery, db_session: Any) -> None:
    """Restaure le clavier de l'annonce sans toucher à son contenu."""
    listing_id = await parse_listing_id(callback)
    if listing_id is None:
        return
    listing = await _listing_or_answer(callback, db_session, listing_id)
    if listing is None:
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=listing_alert_keyboard(listing))
    await callback.answer("Annulé.")


@router.callback_query(F.data.startswith("disliked:"))
async def handle_already_disliked(callback: CallbackQuery) -> None:
    """Informe que cette annonce a déjà été marquée comme faux positif."""
    await callback.answer("Cette annonce a déjà été signalée comme faux positif.", show_alert=True)


@router.callback_query(F.data == "noop")
async def handle_noop_callback(callback: CallbackQuery) -> None:
    """Accusé de réception silencieux pour les boutons de statut (ex: '⏳ En cours...')."""
    await callback.answer()

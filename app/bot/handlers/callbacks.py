import asyncio
import contextlib
import logging
import re
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from app.bot.keyboards.listings import (
    get_dislike_confirmation_keyboard,
    get_listing_alert_keyboard,
)
from app.core.http_client import get_shared_http_client
from app.db.models.bot_event import BotEvent
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.listing_repo import ListingRepository
from app.services.ai.modules.negotiation import negotiation_module
from app.services.book_catalog_service import BookCatalogService
from app.services.market_price_service import MarketPriceService, clean_tome_display_title
from app.services.vinted_scraper import fetch_item_photos

logger = logging.getLogger(__name__)
router = Router(name="callbacks_router")


async def _build_media_group_buffers(photo_urls: list[str]) -> list[InputMediaPhoto]:
    """Télécharge les photos en parallèle pour les uploader directement sur Telegram.

    Évite l'erreur 'WEBPAGE_CURL_FAILED' causée par le blocage des serveurs Telegram par Cloudflare.
    """
    client = get_shared_http_client()
    media_items: list[InputMediaPhoto] = []

    async def _fetch_one(idx: int, url: str) -> tuple[int, bytes | None]:
        try:
            r = await client.get(url, timeout=6.0)
            if r.status_code == 200:
                return idx, r.content
        except Exception as err:
            logger.debug("Échec téléchargement photo %s : %s", url, err)
        return idx, None

    results = await asyncio.gather(*[_fetch_one(i, u) for i, u in enumerate(photo_urls[:10], 1)])
    for idx, content in sorted(results, key=lambda x: x[0]):
        if content:
            buf = BufferedInputFile(file=content, filename=f"photo_{idx}.webp")
            media_items.append(InputMediaPhoto(media=buf))

    return media_items


@router.callback_query(F.data.startswith("photos:"))
async def handle_view_photos(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Envoie l'album photo de l'annonce si plusieurs photos sont disponibles."""
    listing_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    listing_repo = ListingRepository(db_session)
    listing = await listing_repo.get_by_id(listing_id)
    if not listing:
        await callback.answer("Annonce introuvable.", show_alert=True)
        return

    photos = listing.photos or []
    if len(photos) <= 1 and listing.url:
        fetched = await fetch_item_photos(listing.url)
        if len(fetched) > 1:
            photos = fetched
            listing.photos = photos
            await db_session.commit()

    if len(photos) <= 1:
        await callback.answer("ℹ️ Une seule photo est disponible pour cette annonce.", show_alert=True)
        return

    await callback.answer()

    # Envoi de l'album avec upload binaire direct (insensible à WEBPAGE_CURL_FAILED)
    if callback.bot and callback.from_user:
        close_inline = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="← Fermer",
                        callback_data="close_album:",
                    )
                ]
            ]
        )
        try:
            media_group = await _build_media_group_buffers(photos[:10])
            if not media_group:
                if isinstance(callback.message, Message):
                    await callback.message.reply(
                        "ℹ️ Impossible de charger les photos pour le moment.",
                        reply_markup=close_inline,
                    )
                return

            if len(media_group) == 1:
                sent_msg = await callback.bot.send_photo(
                    chat_id=callback.from_user.id,
                    photo=media_group[0].media,
                )
                sent_messages = [sent_msg]
            else:
                sent_messages = await callback.bot.send_media_group(
                    chat_id=callback.from_user.id,
                    media=media_group,
                )

            msg_ids = ",".join(str(m.message_id) for m in sent_messages)
            close_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="← Fermer",
                            callback_data=f"close_album:{msg_ids}",
                        )
                    ]
                ]
            )
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=f"📸 <i>{len(sent_messages)} photos affichées</i>",
                reply_markup=close_keyboard,
            )
        except Exception as exc:
            logger.warning(
                "Erreur lors de l'envoi de la galerie photos pour %s : %s", listing_id, exc
            )
            if isinstance(callback.message, Message):
                await callback.message.reply(
                    "⚠️ Impossible d'afficher l'album photo pour le moment.",
                    reply_markup=close_inline,
                )


@router.callback_query(F.data.startswith("close_album:"))
async def handle_close_album(callback: CallbackQuery) -> None:
    """Supprime l'album photo envoyé et son bouton de fermeture."""
    raw_ids = callback.data.split(":", 1)[1] if callback.data else ""  # type: ignore
    msg_ids = [int(i) for i in raw_ids.split(",") if i.isdigit()]

    if callback.bot and callback.message:
        chat_id = callback.message.chat.id
        for mid in msg_ids:
            with contextlib.suppress(Exception):
                await callback.bot.delete_message(chat_id=chat_id, message_id=mid)
        with contextlib.suppress(Exception):
            await callback.message.delete()

    await callback.answer()


@router.callback_query(F.data.startswith("market:"))
async def handle_view_market_details(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Affiche le panneau détaillé du prix du marché pour l'annonce BD sélectionnée."""
    listing_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    listing_repo = ListingRepository(db_session)
    listing = await listing_repo.get_by_id(listing_id)
    if not listing:
        await callback.answer("Annonce introuvable.", show_alert=True)
        return

    book = None
    books_in_series = None
    tome_result = None
    alert = await listing_repo.get_alert_for_listing(listing.id)
    if alert:
        book_repo = BookRepository(db_session)
        if not alert.isbn:
            book, books_in_series, tome_result = await book_repo.resolve_book_for_listing(
                alert_id=alert.id,
                alert_name=alert.name,
                listing_title=listing.title,
                listing_description=listing.description,
            )
            # Si aucun tome n'a pu être résolu depuis le texte, vérifier l'identification visuelle
            if not book and books_in_series:
                payload = listing.raw_payload or {}
                if payload.get("is_lot") and payload.get("lot_tomes"):
                    from app.services.tome_extractor import TomeMatchResult
                    tome_result = TomeMatchResult(
                        is_lot=True,
                        lot_tomes=payload.get("lot_tomes"),
                        confidence="high",
                    )
                elif listing.photos:
                    t_identified = payload.get("identified_tome")
                    if t_identified:
                        for b in books_in_series:
                            pat = rf"\b(?:tome|t|vol|livre)\s*\.?\s*{t_identified}\b"
                            if re.search(pat, b.title, re.IGNORECASE):
                                book = b
                                break
        else:
            book = await book_repo.get_book_for_alert(alert.id)

    target_isbn = (
        (alert.isbn if alert else None) or listing.isbn13 or (book.isbn13 if book else None)
    )

    if not book and not books_in_series:
        catalog_service = BookCatalogService(db_session)
        book, _ = await catalog_service.identify_book(
            isbn13=target_isbn,
            title=listing.title,
        )

    # Si le tome spécifique de l'annonce n'est pas encore présent dans les livres de la série
    if not book and tome_result and tome_result.tome_number:
        from app.services.book_metadata_service import BookMetadataService

        meta = await BookMetadataService().lookup_by_title_and_author(listing.title)
        if meta and meta.isbn13:
            catalog_service = BookCatalogService(db_session)
            book = await catalog_service.add_book(
                title=listing.title,
                isbn13=meta.isbn13,
                author=meta.author,
                publisher=meta.publisher,
            )
            if alert:
                await BookRepository(db_session).link_alert_to_book(
                    alert_id=alert.id, book_id=book.id
                )
            if books_in_series is not None:
                books_in_series.append(book)
            target_isbn = meta.isbn13

    has_valid_price = (
        book is not None
        and book.market_cache is not None
        and (
            book.market_cache.lowest_delivered_price_cents is not None
            or book.market_cache.historical_low_1y_cents is not None
        )
    )

    # Si le livre ou le cache de prix est absent, tenter un rafraîchissement direct
    if (book and not has_valid_price) or (not book and not books_in_series):
        try:
            from app.core.constants import ComparisonStatus
            from app.db.repositories.market_price_repo import MarketPriceRepository
            from app.providers.chasse_aux_livres import ChasseAuxLivresProvider

            provider = ChasseAuxLivresProvider(timeout_seconds=6.0)
            fallback_title = alert.name if alert else listing.title
            offers = await provider.get_offers(isbn13=target_isbn, title=fallback_title)

            if offers:
                best = offers[0]
                hist_lows = [
                    o.raw_payload.get("historical_low_1y_cents")
                    for o in offers
                    if o.raw_payload and o.raw_payload.get("historical_low_1y_cents") is not None
                ]
                best_hist = min(hist_lows) if hist_lows else None

                catalog_service = BookCatalogService(db_session)
                if not book:
                    book = await catalog_service.add_book(
                        title=fallback_title,
                        isbn13=target_isbn,
                    )
                    if alert:
                        await BookRepository(db_session).link_alert_to_book(
                            alert_id=alert.id, book_id=book.id
                        )
                market_repo = MarketPriceRepository(db_session)
                await market_repo.save_snapshot(book.id, best)

                status_to_set = (
                    ComparisonStatus.AVAILABLE
                    if (best.delivered_price_cents > 0 and best.is_available)
                    else ComparisonStatus.UNAVAILABLE
                )
                lowest_price = best.delivered_price_cents if best.delivered_price_cents > 0 else None

                cache = await market_repo.update_or_create_cache(
                    book_id=book.id,
                    lowest_delivered_price_cents=lowest_price,
                    best_provider_name=best.provider_name if lowest_price else None,
                    best_source_url=best.source_url,
                    confidence=best.confidence,
                    comparison_status=status_to_set,
                    cache_duration_hours=24,
                    historical_low_1y_cents=best_hist,
                )
                book.market_cache = cache
                await db_session.commit()
        except Exception:
            pass

    details_text = MarketPriceService.format_market_details_card(
        listing=listing,
        book=book,
        books_in_series=books_in_series,
        tome_result=tome_result,
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
                    callback_data=f"market_tome:{listing.id}:{b.id}",
                )
            )
            if len(current_row) == 5:
                tome_buttons.append(current_row)
                current_row = []
        if current_row:
            tome_buttons.append(current_row)

    tome_buttons.append([InlineKeyboardButton(text="← Fermer", callback_data="close_market_panel")])
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=tome_buttons)

    if isinstance(callback.message, Message):
        is_market_view = "<b>Prix du marché</b>" in (callback.message.html_text or "")
        if callback.data.startswith("market_details:") and is_market_view:
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
    parts = callback.data.split(":")
    listing_id = uuid.UUID(parts[1])
    book_id = uuid.UUID(parts[2])

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

    details_text = MarketPriceService.format_market_details_card(
        listing=listing,
        book=book,
        books_in_series=books_in_series,
        tome_result=None,
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Tous les tomes",
                    callback_data=f"market_details:{listing.id}",
                ),
                InlineKeyboardButton(text="← Fermer", callback_data="close_market_panel"),
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
    raw_id = callback.data.split(":", 1)[1] if callback.data else ""  # type: ignore
    try:
        listing_id = uuid.UUID(raw_id)
    except ValueError:
        await callback.answer("Annonce invalide.", show_alert=True)
        return

    listing_repo = ListingRepository(db_session)
    listing = await listing_repo.get_by_id(listing_id)
    if not listing:
        await callback.answer("Annonce introuvable.", show_alert=True)
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
            [InlineKeyboardButton(text="← Fermer", callback_data="close_market_panel")]
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
    raw_id = callback.data.split(":", 1)[1] if callback.data else ""  # type: ignore
    try:
        listing_id = uuid.UUID(raw_id)
    except ValueError:
        await callback.answer("Annonce invalide.", show_alert=True)
        return

    listing_repo = ListingRepository(db_session)
    listing = await listing_repo.get_by_id(listing_id)
    if not listing:
        await callback.answer("Annonce introuvable.", show_alert=True)
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
    raw_id = callback.data.split(":", 1)[1] if callback.data else ""  # type: ignore
    try:
        listing_id = uuid.UUID(raw_id)
    except ValueError:
        await callback.answer("Annonce invalide.", show_alert=True)
        return

    listing_repo = ListingRepository(db_session)
    listing = await listing_repo.get_by_id(listing_id)
    alert = await listing_repo.get_alert_for_listing(listing_id)

    # Journaliser l'événement de faux positif
    event = BotEvent(
        event_type="listing_disliked",
        listing_id=listing_id,
        alert_id=alert.id if alert else None,
        metadata_={"chat_id": callback.from_user.id if callback.from_user else None},
    )
    db_session.add(event)
    await db_session.commit()

    if listing and isinstance(callback.message, Message):
        updated_kb = get_listing_alert_keyboard(
            listing_id=listing.id,
            listing_url=listing.url,
            category_slug=listing.category_slug,
            is_disliked=True,
        )
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=updated_kb)

    await callback.answer(
        "👎 Faux positif enregistré ! L'IA l'utilisera pour auto-nettoyer vos alertes.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("cancel_dislike:"))
async def handle_cancel_dislike(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Annule la demande de faux positif et restaure le clavier initial."""
    raw_id = callback.data.split(":", 1)[1] if callback.data else ""  # type: ignore
    try:
        listing_id = uuid.UUID(raw_id)
    except ValueError:
        await callback.answer("Annonce invalide.", show_alert=True)
        return

    listing_repo = ListingRepository(db_session)
    listing = await listing_repo.get_by_id(listing_id)

    if listing and isinstance(callback.message, Message):
        original_kb = get_listing_alert_keyboard(
            listing_id=listing.id,
            listing_url=listing.url,
            category_slug=listing.category_slug,
        )
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=original_kb)

    await callback.answer("Annulé.")


@router.callback_query(F.data.startswith("disliked:"))
async def handle_already_disliked(callback: CallbackQuery) -> None:
    """Informe que cette annonce a déjà été marquée comme faux positif."""
    await callback.answer("Cette annonce a déjà été signalée comme faux positif.", show_alert=True)


@router.callback_query(F.data == "noop")
async def handle_noop_callback(callback: CallbackQuery) -> None:
    """Accusé de réception silencieux pour les boutons de statut (ex: '⏳ En cours...')."""
    await callback.answer()



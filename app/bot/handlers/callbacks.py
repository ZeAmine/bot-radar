"""Handlers de callbacks inline pour les photos et détails de marché."""

import contextlib
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.types import (
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
from app.db.models.bot_event import BotEvent
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.listing_repo import ListingRepository
from app.services.ai.modules.negotiation import negotiation_module
from app.services.book_catalog_service import BookCatalogService
from app.services.market_price_service import MarketPriceService
from app.services.vinted_scraper import fetch_item_photos

router = Router(name="callbacks_router")


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
        # Tenter d'extraire la galerie complète depuis la page de l'annonce
        fetched = await fetch_item_photos(listing.url)
        if len(fetched) > 1:
            photos = fetched
            listing.photos = photos
            await db_session.commit()

    if len(photos) <= 1:
        await callback.answer(
            "Une seule photo est disponible pour cette annonce.",
            show_alert=True,
        )
        return

    await callback.answer()
    media_group: list[Any] = [InputMediaPhoto(media=url) for url in photos[:10]]

    # Envoi de l'album avec message de contrôle pour fermeture
    if callback.bot and callback.from_user:
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
                    if not t_identified and settings.AI_ENABLED:
                        try:
                            from app.services.ai.modules.vision_guard import vision_guard_module
                            v_res = await vision_guard_module.analyze_listing_photos(listing, max_photos=1)
                            if v_res and v_res.identified_tome:
                                t_identified = v_res.identified_tome
                                if isinstance(listing.raw_payload, dict):
                                    listing.raw_payload["identified_tome"] = t_identified
                                    await db_session.flush()
                        except Exception as vision_err:
                            logger.debug("Échec analyse visuelle pour market details: %s", vision_err)

                    if t_identified:
                        import re
                        for b in books_in_series:
                            if re.search(rf"\b(?:tome|t|vol|livre)\s*\.?\s*{t_identified}\b", b.title, re.IGNORECASE):
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
            if target_isbn:
                offers = await provider.get_offers(isbn13=target_isbn, title="")
            else:
                offers = await provider.get_offers(
                    isbn13=None,
                    title=alert.name if alert else listing.title,
                )

            if offers:
                best = offers[0]
                catalog_service = BookCatalogService(db_session)
                if not book:
                    book = await catalog_service.add_book(
                        title=alert.name if alert else listing.title,
                        isbn13=target_isbn,
                    )
                    if alert:
                        await BookRepository(db_session).link_alert_to_book(
                            alert_id=alert.id, book_id=book.id
                        )
                market_repo = MarketPriceRepository(db_session)
                await market_repo.save_snapshot(book.id, best)
                cache = await market_repo.update_or_create_cache(
                    book_id=book.id,
                    lowest_delivered_price_cents=best.delivered_price_cents,
                    best_provider_name=best.provider_name,
                    best_source_url=best.source_url,
                    confidence=best.confidence,
                    comparison_status=ComparisonStatus.AVAILABLE,
                    cache_duration_hours=24,
                    historical_low_1y_cents=best.raw_payload.get("historical_low_1y_cents")
                    if best.raw_payload
                    else None,
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
    back_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Fermer", callback_data="close_market_panel")]
        ]
    )

    if isinstance(callback.message, Message):
        await callback.message.reply(
            text=details_text,
            reply_markup=back_keyboard,
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

    result = await negotiation_module.generate_offer(listing)
    if not result:
        await callback.answer(
            "Impossible de calculer une offre pour cette annonce.", show_alert=True
        )
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
    await callback.answer()


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
        external_id=listing.external_id,
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

    # Journaliser l'événement de faux positif
    event = BotEvent(
        event_type="listing_disliked",
        listing_id=listing_id,
        metadata_={"chat_id": callback.from_user.id if callback.from_user else None},
    )
    db_session.add(event)
    await db_session.commit()

    if listing and isinstance(callback.message, Message):
        updated_kb = get_listing_alert_keyboard(
            listing_id=listing.id,
            listing_url=listing.url,
            category_slug=listing.category_slug,
            external_id=listing.external_id,
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
            external_id=listing.external_id,
        )
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=original_kb)

    await callback.answer("Annulé.")


@router.callback_query(F.data.startswith("disliked:"))
async def handle_already_disliked(callback: CallbackQuery) -> None:
    """Informe que cette annonce a déjà été marquée comme faux positif."""
    await callback.answer("Cette annonce a déjà été signalée comme faux positif.", show_alert=True)


@router.callback_query(F.data == "bundle_dressing_disabled")
async def handle_bundle_dressing_disabled(callback: CallbackQuery) -> None:
    """Informe que le chasseur de lot dressing est temporairement désactivé."""
    await callback.answer(
        "🛍️ Le Chasseur de Lot Dressing est actuellement désactivé.\n"
        "Cette fonctionnalité sera activée dans une prochaine mise à jour.",
        show_alert=True,
    )


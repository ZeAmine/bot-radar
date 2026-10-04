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

from app.bot.keyboards.listings import (
    get_dislike_confirmation_keyboard,
    get_listing_alert_keyboard,
)
from app.db.repositories.autotune_repo import record_listing_dislike
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.listing_repo import ListingRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.services.ai.modules.negotiation import negotiation_module
from app.services.book_catalog_service import BookCatalogService
from app.services.book_market_pricing import price_book_now
from app.services.market_price_service import (
    MarketPriceService,
    clean_tome_display_title,
    has_usable_price,
)

logger = logging.getLogger(__name__)
router = Router(name="callbacks_router")


@router.callback_query(F.data.startswith("market:"))
async def handle_view_market_details(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Affiche le panneau détaillé du prix du marché pour l'annonce BD sélectionnée."""
    try:
        await _show_market_details(callback, db_session)
    except Exception as exc:
        logger.error("Erreur lors de l'affichage des détails du marché: %s", exc, exc_info=True)
        with contextlib.suppress(Exception):
            await callback.answer(
                "Impossible de charger les cotes du marché pour le moment.",
                show_alert=True,
            )


async def _show_market_details(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    try:
        listing_id = uuid.UUID((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Annonce invalide.", show_alert=True)
        return

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
                raw_lot = payload.get("lot_tomes")
                lot_tomes = (
                    [int(t) for t in raw_lot if str(t).lstrip("-").isdigit()]
                    if isinstance(raw_lot, list)
                    else []
                )
                if payload.get("is_lot") and lot_tomes:
                    from app.services.tome_extractor import TomeMatchResult
                    tome_result = TomeMatchResult(
                        is_lot=True,
                        lot_tomes=lot_tomes,
                        confidence="high",
                    )
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

    has_valid_price = book is not None and has_usable_price(book.market_cache)

    # Si le livre ou le cache de prix est absent, tenter un rafraîchissement direct
    if (book and not has_valid_price) or (not book and not books_in_series):
        try:
            fallback_title = alert.name if alert else listing.title
            catalog_service = BookCatalogService(db_session)
            if not book:
                # La recherche se fait sur le nom de l'alerte, qui est aussi le titre
                # retenu pour la fiche : sans ISBN, c'est ce qui fait converger deux
                # ouvertures du panneau vers le même livre au lieu d'en créer un par
                # clic.
                identified, _ = await catalog_service.identify_book(
                    isbn13=target_isbn,
                    title=fallback_title,
                )
                book = identified or await catalog_service.add_book(
                    title=fallback_title,
                    isbn13=target_isbn,
                )
                if alert:
                    await BookRepository(db_session).link_alert_to_book(
                        alert_id=alert.id, book_id=book.id
                    )
            # Le livre doit être écrit avant que la cotation ne lise sa session.
            await db_session.commit()
            cache = await price_book_now(book.id, reason="market_button")
            if cache is not None:
                book.market_cache = cache
        except Exception as exc:
            logger.debug(
                "Rafraîchissement direct du marché ignoré pour l'annonce %s : %s",
                listing_id,
                exc,
            )

    target_book = book
    if (
        not target_book
        and books_in_series
        and len(books_in_series) == 1
        and not (tome_result and tome_result.is_lot)
    ):
        target_book = books_in_series[0]

    latest_snap = None
    if target_book:
        market_repo = MarketPriceRepository(db_session)
        latest_snap = await market_repo.get_latest_snapshot(target_book.id)

    details_text = MarketPriceService.format_market_details_card(
        listing=listing,
        book=target_book,
        books_in_series=books_in_series,
        tome_result=tome_result,
        latest_snapshot=latest_snap,
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
        logger.error("Erreur lors de la sélection du tome: %s", exc, exc_info=True)
        with contextlib.suppress(Exception):
            await callback.answer(
                "Impossible de charger les cotes pour ce tome.",
                show_alert=True,
            )


async def _select_market_tome(callback: CallbackQuery, db_session: Any) -> None:
    parts = (callback.data or "").split(":")
    try:
        listing_id = uuid.UUID(parts[1])
        book_id = uuid.UUID(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Annonce invalide.", show_alert=True)
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
    raw_id = callback.data.split(":", 1)[1] if callback.data else ""
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
    raw_id = callback.data.split(":", 1)[1] if callback.data else ""
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
    raw_id = callback.data.split(":", 1)[1] if callback.data else ""
    try:
        listing_id = uuid.UUID(raw_id)
    except ValueError:
        await callback.answer("Annonce invalide.", show_alert=True)
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

    if alert and callback.from_user and callback.bot:
        from app.tasks.proactive_autotune import schedule_proactive_autotune_check

        schedule_proactive_autotune_check(
            alert_id=alert.id,
            bot=callback.bot,
            chat_id=callback.from_user.id,
        )


@router.callback_query(F.data.startswith("cancel_dislike:"))
async def handle_cancel_dislike(
    callback: CallbackQuery,
    db_session: Any,
) -> None:
    """Annule la demande de faux positif et restaure le clavier initial."""
    raw_id = callback.data.split(":", 1)[1] if callback.data else ""
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



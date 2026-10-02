"""Cas d'usage : Ingestion, matching déterministe et orchestration d'alerte."""

import logging
import sys
import uuid
from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.bot_instance import bot as default_bot
from app.core.constants import CategorySlug
from app.core.tasks import safe_create_task
from app.db.models.bot_event import BotEvent
from app.db.models.listing import Listing
from app.db.repositories.book_repo import BookRepository, resolve_loaded_books
from app.db.repositories.listing_repo import ListingRepository
from app.domain.rules.listing_match import evaluate_listing_against_alert
from app.schemas.listing import (
    ListingIngestPayload,
    ListingIngestResponse,
    ListingMatchItem,
)
from app.services.book_catalog_service import BookCatalogService
from app.services.market_price_service import MarketPriceService
from app.services.telegram_service import send_listing_alert

logger = logging.getLogger("radar.use_cases.ingest_listing")


async def _prefetch_book_market_cache(book_id: uuid.UUID, isbn13: str | None, title: str) -> None:
    """Préchauffe le cache de marché pour le livre nouvellement associé."""
    from app.core.constants import ComparisonStatus
    from app.db.repositories.market_price_repo import MarketPriceRepository
    from app.db.session import AsyncSessionLocal
    from app.providers.chasse_aux_livres import ChasseAuxLivresProvider

    try:
        provider = ChasseAuxLivresProvider(timeout_seconds=8.0)
        offers = await provider.get_offers(isbn13=isbn13, title=title)
        if offers:
            best = offers[0]
            hist_lows = [
                o.raw_payload.get("historical_low_1y_cents")
                for o in offers
                if o.raw_payload and o.raw_payload.get("historical_low_1y_cents") is not None
            ]
            best_hist = min(hist_lows) if hist_lows else None

            async with AsyncSessionLocal() as session:
                market_repo = MarketPriceRepository(session)
                await market_repo.save_snapshot(book_id, best)
                status_to_set = (
                    ComparisonStatus.AVAILABLE
                    if (best.delivered_price_cents > 0 and best.is_available)
                    else ComparisonStatus.UNAVAILABLE
                )
                lowest_price = (
                    best.delivered_price_cents if best.delivered_price_cents > 0 else None
                )
                await market_repo.update_or_create_cache(
                    book_id=book_id,
                    lowest_delivered_price_cents=lowest_price,
                    best_provider_name=best.provider_name if lowest_price else None,
                    best_source_url=best.source_url,
                    confidence=best.confidence,
                    comparison_status=status_to_set,
                    cache_duration_hours=24,
                    historical_low_1y_cents=best_hist,
                )
                await session.commit()
                logger.info(
                    "Cache de prix préchauffé en tâche de fond pour %s (%s).", book_id, title
                )
    except Exception as exc:
        logger.debug("Échec du préchauffage du cache de prix pour %s : %s", book_id, exc)


async def _link_book_from_bnf(title: str, alert_id: uuid.UUID) -> None:
    """Associe un ISBN BNF à l'alerte sans retarder l'envoi Telegram."""
    from app.db.session import AsyncSessionLocal
    from app.services.book_metadata_service import BookMetadataService

    try:
        meta = await BookMetadataService().lookup_by_title_and_author(title)
        if not meta or not meta.isbn13:
            return
        book_id_val: uuid.UUID | None = None
        book_isbn_val: str | None = None
        book_title_val: str = title
        async with AsyncSessionLocal() as session:
            book_repo = BookRepository(session)
            catalog = BookCatalogService(session)
            book = await book_repo.get_by_isbn13(meta.isbn13)
            if not book:
                book = await catalog.add_book(
                    title=title,
                    isbn13=meta.isbn13,
                    author=meta.author,
                    publisher=meta.publisher,
                )
            if book:
                await book_repo.link_alert_to_book(alert_id=alert_id, book_id=book.id)
                book_id_val = book.id
                book_isbn_val = book.isbn13
                book_title_val = book.title
            await session.commit()

        if book_id_val:
            safe_create_task(
                _prefetch_book_market_cache(book_id_val, book_isbn_val, book_title_val),
                name=f"prefetch_market_{book_id_val}",
            )
    except Exception as exc:
        logger.warning("Liaison ISBN BNF ignorée pour l'alerte %s : %s", alert_id, exc)


def comparison_line_from_loaded_books(
    listing: Listing,
    alert: Any,
) -> tuple[str | None, bool]:
    """Cote synchrone depuis les livres déjà chargés. False si le catalogue est vide."""
    links = getattr(alert, "alert_books", None)
    books = []
    if isinstance(links, list):
        books = [link.book for link in links if getattr(link, "book", None) is not None]
    if not books:
        return None, False
    book, series, tome_result = resolve_loaded_books(
        alert_name=alert.name,
        listing_title=listing.title,
        listing_description=listing.description,
        books=books,
    )
    line = MarketPriceService.get_comparison_summary(
        listing=listing,
        book=book,
        books_in_series=series,
        tome_result=tome_result,
    )
    return line, True


async def _link_bd_catalog_background(
    *,
    alert_id: uuid.UUID,
    alert_name: str,
    alert_isbn: str | None,
    listing_title: str,
    listing_description: str | None,
    listing_isbn13: str | None,
) -> None:
    """Lie le catalogue BD après l'alerte, dans une session dédiée."""
    from app.db.session import AsyncSessionLocal

    listing = type(
        "ListingRef",
        (),
        {
            "title": listing_title,
            "description": listing_description,
            "isbn13": listing_isbn13,
            "category_slug": CategorySlug.BD_COMICS.value,
            "item_price_cents": 0,
            "estimated_total_cents": None,
        },
    )()
    alert = type("AlertRef", (), {"id": alert_id, "name": alert_name, "isbn": alert_isbn})()
    try:
        async with AsyncSessionLocal() as session:
            await ProcessListingUseCase(session)._resolve_bd_market_comparison(listing, alert)
            await session.commit()
    except Exception as exc:
        logger.debug("Liaison catalogue BD différée ignorée pour %s : %s", alert_id, exc)


def _alert_has_telegram_chat(alert: Any) -> bool:
    """Vrai si l'alerte a un destinataire Telegram à qui envoyer."""
    user = getattr(alert, "user", None)
    if user is None:
        return False
    return bool(getattr(user, "telegram_user_id", None))


def _get_send_listing_alert() -> Any:
    """Résout dynamiquement la fonction d'envoi d'alerte pour préserver les mocks de tests."""
    ingest_module = sys.modules.get("app.api.routes.ingest")
    if ingest_module and hasattr(ingest_module, "send_listing_alert"):
        return ingest_module.send_listing_alert
    return send_listing_alert


class ProcessListingUseCase:
    """Orchestre la déduplication, le matching, la comparaison de prix et les alertes."""

    def __init__(
        self,
        db: AsyncSession,
        bot: Bot | None = None,
    ) -> None:
        self.db = db
        self.bot = bot or default_bot
        self.listing_repo = ListingRepository(db)

    async def execute(
        self,
        payload: ListingIngestPayload,
        raw_payload: dict[str, Any],
    ) -> ListingIngestResponse:
        """Traite l'annonce avec garantie de latence minimale sur l'envoi d'alerte."""
        # 1. Vérification des doublons
        existing_listing = await self.listing_repo.get_by_source_and_external_id(
            source_name=payload.source_name,
            external_id=payload.external_id,
        )

        notified_ids: set[uuid.UUID] = set()
        pending_ids: set[uuid.UUID] = set()
        if existing_listing:
            listing = existing_listing
            is_duplicate = True
            notified_ids, pending_ids = await self.listing_repo.match_notification_state(listing.id)
        else:
            listing = await self.listing_repo.create_from_payload(payload, raw_dict=raw_payload)
            is_duplicate = False

        active_alerts = await self.listing_repo.get_active_alerts_by_category(payload.category_slug)
        pending_sends: list[tuple[Any, Any, str | None, bool]] = []
        for alert in active_alerts:
            match_result = evaluate_listing_against_alert(
                alert=alert,
                listing_category_slug=listing.category_slug,
                listing_normalized_title=listing.normalized_title,
                listing_normalized_description=listing.description,
                listing_condition=listing.condition,
                listing_item_price_cents=listing.item_price_cents,
                listing_isbn13=listing.isbn13,
                listing_raw_url=listing.url,
            )
            if not match_result.is_match or alert.id in notified_ids:
                continue
            market_line = None
            catalog_ready = False
            if listing.category_slug == CategorySlug.BD_COMICS.value:
                market_line, catalog_ready = comparison_line_from_loaded_books(listing, alert)
                if not catalog_ready:
                    market_line, catalog_ready = await self._comparison_from_catalog_isbn(
                        listing, alert
                    )
            pending_sends.append((alert, match_result, market_line, catalog_ready))

        if is_duplicate and not pending_sends and not pending_ids:
            return ListingIngestResponse(
                listing_id=listing.id,
                is_duplicate=True,
                estimated_total_cents=listing.estimated_total_cents,
                matches=[],
            )

        for alert, match_result, _line, _ready in pending_sends:
            if alert.id in pending_ids:
                continue
            if not _alert_has_telegram_chat(alert):
                continue
            await self.listing_repo.create_match(
                alert_id=alert.id,
                listing_id=listing.id,
                keyword_match_score=match_result.keyword_match_score,
                condition_matches=match_result.condition_matches,
                price_matches=match_result.price_matches,
                notified=False,
            )

        actionable_matches_count = sum(
            1 for alert, _, _, _ in pending_sends if _alert_has_telegram_chat(alert)
        )
        event = BotEvent(
            event_type="listing_ingested",
            listing_id=listing.id,
            metadata_={
                "source_name": payload.source_name,
                "external_id": payload.external_id,
                "matches_count": actionable_matches_count,
                "is_duplicate": is_duplicate,
            },
        )
        self.db.add(event)
        await self.db.commit()

        match_items: list[ListingMatchItem] = []
        needs_retry = False
        send_alert_fn = _get_send_listing_alert()
        for alert, _match_result, market_line, catalog_ready in pending_sends:
            notification_sent = False
            if _alert_has_telegram_chat(alert):
                alert_kws = alert.include_keywords or ([alert.name] if alert.name else None)
                notification_sent = await send_alert_fn(
                    bot=self.bot,
                    chat_id=alert.user.telegram_user_id,
                    listing=listing,
                    market_comparison_line=market_line,
                    alert_keywords=alert_kws,
                    alert=alert,
                )
                if notification_sent:
                    await self.listing_repo.mark_notified([(alert.id, listing.id)])
                    await self.db.commit()
                else:
                    needs_retry = True
            if listing.category_slug == CategorySlug.BD_COMICS.value and not catalog_ready:
                safe_create_task(
                    _link_bd_catalog_background(
                        alert_id=alert.id,
                        alert_name=alert.name,
                        alert_isbn=getattr(alert, "isbn", None),
                        listing_title=listing.title,
                        listing_description=listing.description,
                        listing_isbn13=listing.isbn13,
                    ),
                    name=f"bd_catalog_{alert.id}",
                )
            match_items.append(
                ListingMatchItem(
                    alert_id=alert.id,
                    alert_name=alert.name,
                    notification_sent=notification_sent,
                )
            )

        return ListingIngestResponse(
            listing_id=listing.id,
            is_duplicate=is_duplicate,
            estimated_total_cents=listing.estimated_total_cents,
            matches=match_items,
            needs_retry=needs_retry,
        )

    async def _comparison_from_catalog_isbn(
        self,
        listing: Listing,
        alert: Any,
    ) -> tuple[str | None, bool]:
        """Une lecture ISBN indexée si le livre est déjà au catalogue. Pas de BNF."""
        isbn = listing.isbn13 or getattr(alert, "isbn", None)
        if not isbn:
            return None, False
        book_repo = BookRepository(self.db)
        book = await book_repo.get_by_isbn13(isbn)
        if book is None:
            return None, False
        await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)
        line = MarketPriceService.get_comparison_summary(
            listing=listing,
            book=book,
            books_in_series=[book],
        )
        return line, True

    async def _resolve_bd_market_comparison(self, listing: Listing, alert: Any) -> str | None:
        """Résout le livre en local. La recherche BNF part en tâche de fond."""
        book_repo = BookRepository(self.db)
        catalog_service = BookCatalogService(self.db)

        book = None
        books_in_series = None
        tome_result = None

        if not alert.isbn:
            resolved_book, books_in_series, tome_result = (
                await book_repo.resolve_book_for_listing(
                    alert_id=alert.id,
                    alert_name=alert.name,
                    listing_title=listing.title,
                    listing_description=listing.description,
                )
            )
            book = resolved_book

            if not book:
                discovered_isbn = listing.isbn13
                has_lot = bool(tome_result and tome_result.is_lot and tome_result.lot_tomes)
                if not discovered_isbn and not has_lot:
                    safe_create_task(
                        _link_book_from_bnf(listing.title, alert.id),
                        name=f"bnf_link_{alert.id}",
                    )

                if discovered_isbn:
                    book = await book_repo.get_by_isbn13(discovered_isbn)
                    if not book:
                        book = await catalog_service.add_book(
                            title=listing.title,
                            isbn13=discovered_isbn,
                        )
                    if book:
                        await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)
        else:
            book = await book_repo.get_book_for_alert(alert.id)
            if not book and alert.isbn:
                book = await book_repo.get_by_isbn13(alert.isbn)
                if not book:
                    book = await catalog_service.add_book(
                        title=alert.name,
                        isbn13=alert.isbn,
                    )
                if book:
                    await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)

        return MarketPriceService.get_comparison_summary(
            listing=listing,
            book=book,
            books_in_series=books_in_series,
            tome_result=tome_result,
        )

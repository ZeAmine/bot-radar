"""Route d'ingestion sécurisée d'annonces JSON."""

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_ingest_token
from app.bot.bot_instance import bot
from app.core.constants import CategorySlug, ComparisonStatus
from app.db.models.bot_event import BotEvent
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.listing_repo import ListingRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.schemas.listing import (
    ListingIngestPayload,
    ListingIngestResponse,
    ListingMatchItem,
)
from app.services.book_catalog_service import BookCatalogService
from app.services.market_price_service import MarketPriceService
from app.services.matcher_service import evaluate_listing_against_alert
from app.services.tech_market_service import TechMarketService
from app.services.telegram_service import send_listing_alert

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ingest", tags=["Ingestion"])


@router.post(
    "/listings",
    response_model=ListingIngestResponse,
    dependencies=[Depends(require_ingest_token)],
)
async def ingest_listing(
    payload: ListingIngestPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ListingIngestResponse:
    """Ingère une nouvelle annonce, calcule le total, matche les alertes et notifie."""
    raw_payload: dict[str, Any] = await request.json()
    listing_repo = ListingRepository(db)

    # 1. Vérification des doublons de listings
    existing_listing = await listing_repo.get_by_source_and_external_id(
        source_name=payload.source_name,
        external_id=payload.external_id,
    )

    if existing_listing:
        listing = existing_listing
        is_duplicate = True
    else:
        listing = await listing_repo.create_from_payload(payload, raw_dict=raw_payload)
        is_duplicate = False

    # 2. Recherche des alertes actives de la même catégorie
    active_alerts = await listing_repo.get_active_alerts_by_category(payload.category_slug)
    match_items: list[ListingMatchItem] = []

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

        if not match_result.is_match:
            continue

        # 3. Empêcher les doublons de notification
        already_notified = await listing_repo.is_already_matched(
            alert_id=alert.id,
            listing_id=listing.id,
        )
        if already_notified:
            continue

        # 4. Préparation de la comparaison de marché pour BD & Comics
        market_comparison_line: str | None = None
        if listing.category_slug == CategorySlug.BD_COMICS.value:
            book_repo = BookRepository(db)
            catalog_service = BookCatalogService(db)

            resolved_book = None
            books_in_series = None
            tome_result = None

            # Si l'alerte est une série multi-tomes (isbn is NULL)
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
            else:
                # 4.1 Récupération du livre lié à cette alerte en priorité
                book = await book_repo.get_book_for_alert(alert.id)

            target_isbn = alert.isbn or listing.isbn13 or (book.isbn13 if book else None)

            # 4.2 Si non lié, tenter l'identification par ISBN ou par titre
            if not book and not books_in_series:
                if target_isbn:
                    book = await book_repo.get_by_isbn13(target_isbn)
                if not book:
                    search_title = alert.name or listing.title
                    book, _ = await catalog_service.identify_book(
                        isbn13=target_isbn,
                        title=search_title,
                    )
                if not book and alert.name:
                    book = await catalog_service.add_book(
                        title=alert.name,
                        isbn13=target_isbn,
                    )

                if book:
                    await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)

            # 4.3 Si le livre existe mais sans cache valide, interroger Chasse-aux-livres en direct
            if book:
                now_utc = datetime.now(UTC)
                cache = book.market_cache
                is_cache_valid = (
                    cache is not None
                    and cache.comparison_status == ComparisonStatus.AVAILABLE
                    and cache.lowest_delivered_price_cents is not None
                    and (cache.expires_at is None or cache.expires_at > now_utc)
                )

                if not is_cache_valid:
                    try:
                        from app.providers.chasse_aux_livres import ChasseAuxLivresProvider

                        chasse_provider = ChasseAuxLivresProvider(timeout_seconds=6.0)
                        if target_isbn:
                            offers = await chasse_provider.get_offers(
                                isbn13=target_isbn,
                                title="",
                            )
                        else:
                            offers = await chasse_provider.get_offers(
                                isbn13=None,
                                title=book.title,
                                author=book.author,
                            )
                        if offers:
                            market_repo = MarketPriceRepository(db)
                            best = offers[0]
                            await market_repo.save_snapshot(book.id, best)
                            cache = await market_repo.update_or_create_cache(
                                book_id=book.id,
                                lowest_delivered_price_cents=best.delivered_price_cents,
                                best_provider_name=best.provider_name,
                                best_source_url=best.source_url,
                                confidence=best.confidence,
                                comparison_status=ComparisonStatus.AVAILABLE,
                                cache_duration_hours=24,
                                historical_low_1y_cents=best.raw_payload.get(
                                    "historical_low_1y_cents"
                                )
                                if best.raw_payload
                                else None,
                            )
                            book.market_cache = cache
                    except Exception as exc:
                        logger.debug(
                            "Échec de l'interrogation en direct Chasse-aux-livres: %s", exc
                        )

            market_comparison_line = MarketPriceService.get_comparison_summary(
                listing=listing,
                book=book,
                books_in_series=books_in_series,
                tome_result=tome_result,
            )
        elif listing.category_slug in (
            CategorySlug.TECH.value,
            CategorySlug.CLOTHES.value,
            CategorySlug.GAMING.value,
        ):
            market_comparison_line = TechMarketService.get_comparison_summary(
                listing=listing,
                alert=alert,
            )

        # 5. Envoi de l'alerte Telegram si l'utilisateur possède un chat id valide
        notification_sent = False
        if alert.user and alert.user.telegram_user_id:
            alert_kws = alert.include_keywords or ([alert.name] if alert.name else None)
            notification_sent = await send_listing_alert(
                bot=bot,
                chat_id=alert.user.telegram_user_id,
                listing=listing,
                market_comparison_line=market_comparison_line,
                alert_keywords=alert_kws,
                is_bundle=getattr(match_result, "is_bundle", False),
            )

        # 6. Création de l'enregistrement listing_matches
        await listing_repo.create_match(
            alert_id=alert.id,
            listing_id=listing.id,
            keyword_match_score=match_result.keyword_match_score,
            condition_matches=match_result.condition_matches,
            price_matches=match_result.price_matches,
            notified=notification_sent,
        )

        match_items.append(
            ListingMatchItem(
                alert_id=alert.id,
                alert_name=alert.name,
                notification_sent=notification_sent,
            )
        )

    # 7. Journalisation d'un événement d'ingestion
    event = BotEvent(
        event_type="listing_ingested",
        listing_id=listing.id,
        metadata_={
            "source_name": payload.source_name,
            "external_id": payload.external_id,
            "matches_count": len(match_items),
            "is_duplicate": is_duplicate,
        },
    )
    db.add(event)
    await db.commit()

    return ListingIngestResponse(
        listing_id=listing.id,
        is_duplicate=is_duplicate,
        estimated_total_cents=listing.estimated_total_cents,
        matches=match_items,
    )

"""Cas d'usage : Ingestion, matching déterministe et orchestration d'alerte."""

import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.bot_instance import bot as default_bot
from app.core.constants import CategorySlug, ComparisonStatus
from app.db.models.bot_event import BotEvent
from app.db.models.listing import Listing
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.listing_repo import ListingRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.domain.entities.listing import ListingEntity
from app.domain.enums import CategorySlug as DomainCategorySlug
from app.domain.protocols.matching import ListingMatcherProtocol
from app.domain.rules.matcher import DeterministicListingMatcher
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
from app.use_cases.enrich_listing_ai import AsyncEnrichListingUseCase

logger = logging.getLogger("radar.use_cases.ingest_listing")


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
        matcher: ListingMatcherProtocol | None = None,
        ai_enricher: AsyncEnrichListingUseCase | None = None,
        bot: Bot | None = None,
    ) -> None:
        self.db = db
        self.matcher = matcher or DeterministicListingMatcher()
        self.ai_enricher = ai_enricher or AsyncEnrichListingUseCase(bot=bot)
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

        if existing_listing:
            listing = existing_listing
            is_duplicate = True
        else:
            listing = await self.listing_repo.create_from_payload(payload, raw_dict=raw_payload)
            is_duplicate = False

        # 2. Recherche des alertes actives de la même catégorie
        active_alerts = await self.listing_repo.get_active_alerts_by_category(payload.category_slug)
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
            already_notified = await self.listing_repo.is_already_matched(
                alert_id=alert.id,
                listing_id=listing.id,
            )
            if already_notified:
                continue

            # 4. Résolution locale rapide du livre et comparaison de cotation (< 3ms, zéro appel réseau externe)
            market_comparison_line = None
            if listing.category_slug == CategorySlug.BD_COMICS.value:
                market_comparison_line = await self._resolve_bd_market_comparison(listing, alert)
            elif listing.category_slug in (
                CategorySlug.TECH.value,
                CategorySlug.CLOTHES.value,
                CategorySlug.GAMING.value,
            ):
                market_comparison_line = TechMarketService.get_comparison_summary(
                    listing=listing,
                    alert=alert,
                )

            # 5. Envoi INSTANTANÉ de l'alerte Telegram (Priorité absolue, sans blocage réseau)
            notification_sent = False
            if alert.user and alert.user.telegram_user_id:
                alert_kws = alert.include_keywords or ([alert.name] if alert.name else None)
                send_alert_fn = _get_send_listing_alert()
                notification_sent = await send_alert_fn(
                    bot=self.bot,
                    chat_id=alert.user.telegram_user_id,
                    listing=listing,
                    market_comparison_line=market_comparison_line,
                    alert_keywords=alert_kws,
                    is_bundle=getattr(match_result, "is_bundle", False),
                    alert=alert,
                )

                # 6. Lancement de l'analyse DeepSeek en TÂCHE DE FOND (asynchrone, non bloquante)
                if notification_sent and self.ai_enricher:
                    try:
                        listing_entity = ListingEntity(
                            id=listing.id,
                            source_name=listing.source_name,
                            external_id=listing.external_id,
                            title=listing.title,
                            normalized_title=listing.normalized_title,
                            category_slug=DomainCategorySlug(listing.category_slug),
                            item_price_cents=listing.item_price_cents,
                            total_price_cents=listing.estimated_total_cents,
                            condition=listing.condition,
                            url=listing.url,
                            photos=listing.photos,
                            description=listing.description,
                        )
                        await self.ai_enricher.schedule_enrichment(listing_entity, alert)
                    except Exception as ai_err:
                        logger.warning("Échec déclenchement IA pour listing %s: %s", listing.id, ai_err)

            # 7. Création de l'enregistrement listing_matches
            await self.listing_repo.create_match(
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

        # 8. Journalisation d'un événement d'ingestion
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
        self.db.add(event)
        await self.db.commit()

        return ListingIngestResponse(
            listing_id=listing.id,
            is_duplicate=is_duplicate,
            estimated_total_cents=listing.estimated_total_cents,
            matches=match_items,
        )

    async def _resolve_bd_market_comparison(self, listing: Listing, alert: Any) -> str | None:
        """Résout le livre et calcule l'écart sur prix bas constaté (< 3ms, zéro appel réseau externe)."""
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
                    try:
                        from app.services.book_metadata_service import BookMetadataService

                        meta_service = BookMetadataService()
                        meta = await meta_service.lookup_by_title_and_author(listing.title)
                        if meta and meta.isbn13:
                            discovered_isbn = meta.isbn13
                    except Exception:
                        pass

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

"""Pipeline d'enrichissement asynchrone post-alerte (Phase 2)."""

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.db.models.listing import Listing
from app.services.ai.client import DeepSeekClient, deepseek_client
from app.services.ai.modules.vision_guard import vision_guard_module
from app.services.ai.registry import ai_registry

logger = logging.getLogger(__name__)

COMBINED_SYSTEM_PROMPT = (
    "Tu es l'analyste expert de Radar Bot (achat-revente et alertes d'occasion).\n"
    "Tu reçois les détails d'une annonce Vinted qui vient d'être détectée.\n"
    "Analyse les aspects demandés selon les instructions et retourne un objet JSON "
    "structuré regroupant les clés requises."
)


class AIPostAlertPipeline:
    """Orchestre les analyses IA post-alerte sans ralentir l'envoi initial Telegram."""

    def __init__(self, client: DeepSeekClient | None = None) -> None:
        self.client = client or deepseek_client

    async def enrich_alert(
        self,
        bot: Bot,
        chat_id: int,
        message_id: int,
        listing: Listing,
        alert: Any,
    ) -> None:
        """Exécute l'enrichissement en tâche de fond et met à jour l'alerte."""
        try:
            # Courte respiration pour laisser Telegram délivrer le premier message
            await asyncio.sleep(0.4)

            lines_to_notify: list[str] = []

            # 1. Analyse textuelle combinée (Price Glitch, Hidden Condition, Bundle, Profit)
            active_modules = ai_registry.get_active_post_alert_modules()
            chunks: dict[str, str] = {}
            for module in active_modules:
                chunk = module.build_prompt_chunk(listing, alert)
                if chunk:
                    key, instruction = chunk
                    chunks[key] = instruction

            if chunks and self.client.is_configured:
                user_prompt = (
                    f"Article : '{listing.title}'\n"
                    f"Catégorie : {listing.category_slug}\n"
                    f"Prix : {listing.item_price_cents / 100:.2f} €\n"
                    f"État déclaré : {listing.condition or 'Non précisé'}\n"
                    f"Description : '{listing.description or 'Aucune'}'\n\n"
                    "INSTRUCTIONS D'ANALYSE :\n"
                )
                for key, instruction in chunks.items():
                    user_prompt += f"\n--- {key} ---\n{instruction}\n"

                data = await self.client.generate_json(
                    system_prompt=COMBINED_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    temperature=0.2,
                )

                if data:
                    for module in active_modules:
                        for key in chunks:
                            if key in data:
                                parsed = module.parse_chunk_result(data[key], listing, alert)
                                html_line = module.format_telegram_html(parsed)
                                if html_line:
                                    lines_to_notify.append(html_line)

            # 2. Analyse visuelle multi-photos (VisionGuard)
            if vision_guard_module.is_enabled and listing.photos:
                vision_result = await vision_guard_module.analyze_listing_photos(listing)
                if vision_result:
                    vision_html = vision_guard_module.format_telegram_html(vision_result)
                    if vision_html:
                        lines_to_notify.append(vision_html)

                    # Si un tome précis ou un lot est détecté visuellement sur une alerte de série
                    is_visual_lot = vision_result.is_lot and len(vision_result.identified_tomes) > 1
                    has_visual_tome = bool(vision_result.identified_tome or is_visual_lot)

                    if has_visual_tome and alert and not getattr(alert, "isbn", None):
                        try:
                            import re

                            from app.bot.keyboards.listings import (
                                build_vinted_checkout_url,
                                get_listing_alert_keyboard,
                            )
                            from app.db.repositories.book_repo import BookRepository
                            from app.db.session import AsyncSessionLocal
                            from app.services.market_price_service import MarketPriceService
                            from app.services.telegram_service import build_listing_caption
                            from app.services.tome_extractor import TomeMatchResult

                            async with AsyncSessionLocal() as session:
                                b_repo = BookRepository(session)
                                series_books = await b_repo.get_all_books_for_alert(alert.id)
                                tome_summary = None

                                if is_visual_lot:
                                    lot_tomes = sorted(set(vision_result.identified_tomes))
                                    try:
                                        from app.db.repositories.listing_repo import (
                                            ListingRepository,
                                        )

                                        l_repo = ListingRepository(session)
                                        db_listing = await l_repo.get_by_id(listing.id)
                                        if db_listing and isinstance(db_listing.raw_payload, dict):
                                            db_listing.raw_payload["is_lot"] = True
                                            db_listing.raw_payload["lot_tomes"] = lot_tomes
                                            await session.commit()
                                    except Exception as db_err:
                                        logger.debug(
                                            "Échec enregistrement lot identifié dans listing: %s",
                                            db_err,
                                        )

                                    tome_res = TomeMatchResult(
                                        is_lot=True,
                                        lot_tomes=lot_tomes,
                                        confidence="high",
                                    )
                                    tome_summary = MarketPriceService.get_comparison_summary(
                                        listing=listing,
                                        book=None,
                                        books_in_series=series_books,
                                        tome_result=tome_res,
                                    )
                                else:
                                    matched_book = None
                                    if series_books:
                                        t_num = vision_result.identified_tome
                                        for b in series_books:
                                            if re.search(
                                                rf"\b(?:tome|t|vol|livre)\s*\.?\s*{t_num}\b",
                                                b.title,
                                                re.IGNORECASE,
                                            ):
                                                matched_book = b
                                                break
                                        if not matched_book and vision_result.identified_subtitle:
                                            sub = vision_result.identified_subtitle.lower()
                                            for b in series_books:
                                                if sub in b.title.lower():
                                                    matched_book = b
                                                    break

                                    if matched_book and matched_book.market_cache:
                                        try:
                                            from app.db.repositories.listing_repo import (
                                                ListingRepository,
                                            )

                                            l_repo = ListingRepository(session)
                                            db_listing = await l_repo.get_by_id(listing.id)
                                            if db_listing and isinstance(
                                                db_listing.raw_payload, dict
                                            ):
                                                db_listing.raw_payload["identified_tome"] = (
                                                    vision_result.identified_tome
                                                )
                                                await session.commit()
                                        except Exception as db_err:
                                            logger.debug(
                                                "Échec enregistrement tome dans listing: %s",
                                                db_err,
                                            )

                                        tome_res = TomeMatchResult(
                                            is_lot=False,
                                            tome_number=vision_result.identified_tome,
                                            confidence="high",
                                        )
                                        tome_summary = MarketPriceService.get_comparison_summary(
                                            listing=listing,
                                            book=matched_book,
                                            tome_result=tome_res,
                                        )

                                if tome_summary:
                                    new_caption = build_listing_caption(
                                        listing=listing,
                                        market_comparison_line=tome_summary,
                                        alert_keywords=getattr(alert, "include_keywords", None),
                                    )
                                    checkout_url = build_vinted_checkout_url(listing.external_id)
                                    keyboard = get_listing_alert_keyboard(
                                        listing_id=listing.id,
                                        listing_url=listing.url,
                                        category_slug=listing.category_slug,
                                        checkout_url=checkout_url,
                                    )
                                    import contextlib

                                    try:
                                        await bot.edit_message_caption(
                                            chat_id=chat_id,
                                            message_id=message_id,
                                            caption=new_caption,
                                            reply_markup=keyboard,
                                        )
                                    except TelegramAPIError:
                                        with contextlib.suppress(TelegramAPIError):
                                            await bot.edit_message_text(
                                                chat_id=chat_id,
                                                message_id=message_id,
                                                text=new_caption,
                                                reply_markup=keyboard,
                                            )
                        except Exception as edit_err:
                            logger.debug(
                                "Échec mise à jour de la cotation précise du tome/lot: %s", edit_err
                            )

            # 3. Envoi de la note d'enrichissement si des éléments notables ont été trouvés
            if lines_to_notify:
                full_text = "\n\n".join(lines_to_notify)
                await bot.send_message(
                    chat_id=chat_id,
                    text=full_text,
                    reply_to_message_id=message_id,
                )
                logger.info(
                    "Note d'enrichissement IA envoyée pour l'annonce %s (chat_id: %s)",
                    listing.id,
                    chat_id,
                )

        except TelegramAPIError as exc:
            logger.debug("Échec envoi note d'enrichissement IA: %s", exc)
        except Exception as exc:
            logger.exception("Erreur dans AIPostAlertPipeline: %s", exc)


ai_post_alert_pipeline = AIPostAlertPipeline()

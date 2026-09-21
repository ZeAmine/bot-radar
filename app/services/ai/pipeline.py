"""Pipeline d'enrichissement asynchrone post-alerte (Phase 2)."""

import asyncio
import logging
import re
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

            data: dict[str, Any] | None = None
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
            vision_result = None
            if vision_guard_module.is_enabled and listing.photos:
                vision_result = await vision_guard_module.analyze_listing_photos(listing)
                if vision_result:
                    vision_html = vision_guard_module.format_telegram_html(vision_result)
                    if vision_html:
                        lines_to_notify.append(vision_html)

            # 3. Détection des tomes d'un lot ou tome unique via IA (Texte ou Vision)
            detected_lot_tomes: list[int] = []
            detected_single_tome: int | None = None

            if chunks and data and "bundle_decomposition" in data:
                from app.services.ai.modules.bundle_decomposition import bundle_decomposition_module

                bd_res = bundle_decomposition_module.parse_chunk_result(
                    data["bundle_decomposition"], listing, alert
                )
                if bd_res and bd_res.items_detected:
                    extracted = bundle_decomposition_module.extract_tomes(bd_res.items_detected)
                    if len(extracted) > 1:
                        detected_lot_tomes = extracted
                    elif len(extracted) == 1:
                        detected_single_tome = extracted[0]

            if vision_result:
                if vision_result.is_lot and len(vision_result.identified_tomes) > 1:
                    detected_lot_tomes = sorted(set(vision_result.identified_tomes))
                elif vision_result.identified_tome and not detected_lot_tomes:
                    detected_single_tome = vision_result.identified_tome

            # 4. Mise à jour de la légende d'origine si des tomes ont été identifiés
            tomes_found = bool(detected_lot_tomes or detected_single_tome)
            if tomes_found and alert and not getattr(alert, "isbn", None):
                try:
                    from app.bot.keyboards.listings import get_listing_alert_keyboard
                    from app.db.repositories.book_repo import BookRepository
                    from app.db.repositories.listing_repo import ListingRepository
                    from app.db.session import AsyncSessionLocal
                    from app.services.market_price_service import MarketPriceService
                    from app.services.telegram_service import build_listing_caption
                    from app.services.tome_extractor import TomeMatchResult
                    from app.services.vinted_scraper import build_vinted_checkout_url

                    async with AsyncSessionLocal() as session:
                        l_repo = ListingRepository(session)
                        db_listing = await l_repo.get_by_id(listing.id)
                        if db_listing and isinstance(db_listing.raw_payload, dict):
                            if detected_lot_tomes:
                                db_listing.raw_payload["is_lot"] = True
                                db_listing.raw_payload["lot_tomes"] = detected_lot_tomes
                            elif detected_single_tome:
                                db_listing.raw_payload["identified_tome"] = detected_single_tome
                            await session.commit()

                        book_repo = BookRepository(session)
                        books_in_series = await book_repo.get_all_books_for_alert(alert.id)
                        if books_in_series:
                            if detected_lot_tomes:
                                t_res = TomeMatchResult(
                                    is_lot=True,
                                    lot_tomes=detected_lot_tomes,
                                    lot_count=len(detected_lot_tomes),
                                )
                                new_comp = MarketPriceService.get_comparison_summary(
                                    listing=listing,
                                    book=None,
                                    books_in_series=books_in_series,
                                    tome_result=t_res,
                                )
                            else:
                                t_res = TomeMatchResult(
                                    is_lot=False,
                                    tome_number=detected_single_tome,
                                )
                                matched_book = next(
                                    (
                                        b
                                        for b in books_in_series
                                        if re.search(
                                            rf"\b(?:tome|t|vol)\s*\.?\s*{detected_single_tome}\b",
                                            b.title,
                                            re.IGNORECASE,
                                        )
                                    ),
                                    None,
                                )
                                new_comp = MarketPriceService.get_comparison_summary(
                                    listing=listing,
                                    book=matched_book,
                                    books_in_series=books_in_series,
                                    tome_result=t_res,
                                )

                            if new_comp:
                                alert_kws = (
                                    alert.include_keywords
                                    if hasattr(alert, "include_keywords")
                                    else None
                                )
                                new_caption = build_listing_caption(
                                    listing=listing,
                                    market_comparison_line=new_comp,
                                    alert_keywords=alert_kws,
                                )
                                checkout_url = build_vinted_checkout_url(listing.external_id)
                                keyboard = get_listing_alert_keyboard(
                                    listing_id=listing.id,
                                    listing_url=listing.url,
                                    category_slug=listing.category_slug,
                                    checkout_url=checkout_url,
                                )
                                await bot.edit_message_caption(
                                    chat_id=chat_id,
                                    message_id=message_id,
                                    caption=new_caption,
                                    reply_markup=keyboard,
                                )
                                logger.info(
                                    "Légende d'alerte mise à jour pour l'annonce %s (tomes: %s)",
                                    listing.id,
                                    detected_lot_tomes or detected_single_tome,
                                )
                except TelegramAPIError as tg_err:
                    logger.debug("Mise à jour légende Telegram ignorée: %s", tg_err)
                except Exception as upd_err:
                    logger.warning(
                        "Erreur lors de la mise à jour de la légende d'alerte: %s",
                        upd_err,
                    )

            # 5. Note d'enrichissement si des alertes critiques existent
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

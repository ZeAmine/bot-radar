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

                    # Si un tome précis ou un lot est détecté visuellement sur une alerte de série,
                    # enregistrer l'information en base pour le bouton [ 📊 Prix du marché ], sans altérer la légende Telegram.
                    is_visual_lot = vision_result.is_lot and len(vision_result.identified_tomes) > 1
                    has_visual_tome = bool(vision_result.identified_tome or is_visual_lot)

                    if has_visual_tome and alert and not getattr(alert, "isbn", None):
                        try:
                            from app.db.repositories.listing_repo import ListingRepository
                            from app.db.session import AsyncSessionLocal

                            async with AsyncSessionLocal() as session:
                                l_repo = ListingRepository(session)
                                db_listing = await l_repo.get_by_id(listing.id)
                                if db_listing and isinstance(db_listing.raw_payload, dict):
                                    if is_visual_lot:
                                        db_listing.raw_payload["is_lot"] = True
                                        db_listing.raw_payload["lot_tomes"] = sorted(set(vision_result.identified_tomes))
                                    elif vision_result.identified_tome:
                                        db_listing.raw_payload["identified_tome"] = vision_result.identified_tome
                                    await session.commit()
                        except Exception as db_err:
                            logger.debug("Échec enregistrement tome/lot visuel dans listing: %s", db_err)

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

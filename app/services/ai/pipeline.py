"""Pipeline d'enrichissement asynchrone post-alerte (Phase 2)."""

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.core.alert_latency import observe_background
from app.db.models.listing import Listing
from app.providers.llm.base import LLMClient
from app.providers.llm.registry import llm_client
from app.services.ai.registry import ai_registry

logger = logging.getLogger(__name__)

COMBINED_SYSTEM_PROMPT = (
    "Tu es l'analyste expert de BotRadar (achat-revente et alertes d'occasion).\n"
    "Tu reçois les détails d'une annonce Vinted qui vient d'être détectée.\n"
    "Analyse les aspects demandés selon les instructions et retourne un objet JSON "
    "structuré regroupant les clés requises."
)


class AIPostAlertPipeline:
    """Orchestre les analyses IA post-alerte sans ralentir l'envoi initial Telegram."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self.client: LLMClient = client or llm_client

    @observe_background("ai_post_alert")
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
            # Laisser Telegram délivrer le message initial avant la note.
            await asyncio.sleep(0.4)

            lines_to_notify: list[str] = []

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

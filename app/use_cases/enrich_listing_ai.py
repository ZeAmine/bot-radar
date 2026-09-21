"""Cas d'usage : Enrichissement asynchrone d'une annonce via DeepSeek (post-alerte)."""

import html
import logging
from typing import Any

from aiogram import Bot

from app.bot.bot_instance import bot as default_bot
from app.domain.entities.analysis import ListingAnalysisResult
from app.domain.entities.listing import ListingEntity
from app.domain.protocols.llm import LLMAnalysisProviderProtocol
from app.providers.llm.deepseek_provider import DeepSeekProvider

logger = logging.getLogger("radar.use_cases.enrich_listing_ai")


class AsyncEnrichListingUseCase:
    """Orchestre l'analyse IA sémantique en tâche de fond sans ralentir la détection."""

    def __init__(
        self,
        llm_provider: LLMAnalysisProviderProtocol | None = None,
        bot: Bot | None = None,
    ) -> None:
        self._llm = llm_provider or DeepSeekProvider()
        self._bot = bot or default_bot

    async def execute(
        self,
        chat_id: int,
        message_id: int,
        listing: ListingEntity,
    ) -> ListingAnalysisResult | None:
        """Exécute l'analyse DeepSeek et notifie en cas de risque détecté."""
        try:
            analysis = await self._llm.analyze_listing(
                title=listing.title,
                description=listing.description or "",
                price_cents=listing.item_price_cents,
                declared_condition=listing.condition,
                category=listing.category_slug,
                image_urls=listing.photos,
            )
            if not analysis:
                return None

            # Détection de signaux nécessitant un avertissement utilisateur
            has_warning = (
                analysis.is_scam_or_trap
                or analysis.is_lot_incomplete
                or analysis.risk_score >= 60
            )

            if has_warning and self._bot:
                warning_lines: list[str] = ["<b>⚠️ Analyse IA de vigilance :</b>"]

                if analysis.is_scam_or_trap:
                    warning_lines.append(
                        "⛔️ <b>Alerte suspicion :</b> Risque d'arnaque, boîte vide ou piège."
                    )
                elif analysis.is_lot_incomplete:
                    warning_lines.append(
                        "📦 <b>Lot potentiellement incomplet :</b> Pièces ou tomes manquants."
                    )

                if analysis.analysis_summary:
                    warning_lines.append(f"💬 <i>{html.escape(analysis.analysis_summary)}</i>")

                warning_msg = "\n".join(warning_lines)

                # Envoi d'un message complémentaire discret en réponse au message initial
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=warning_msg,
                    reply_to_message_id=message_id,
                    parse_mode="HTML",
                )

            return analysis

        except Exception as exc:
            logger.warning(
                "Échec de l'enrichissement IA post-alerte pour l'annonce %s : %s",
                listing.external_id,
                exc,
            )
            return None

    async def schedule_enrichment(
        self,
        listing: ListingEntity,
        alert: Any,
    ) -> None:
        """Planifie l'analyse IA sémantique en tâche de fond si un destinataire est configuré."""
        user_id = getattr(getattr(alert, "user", None), "telegram_user_id", None)
        if not user_id:
            return
        import asyncio
        asyncio.create_task(
            self.execute(
                chat_id=user_id,
                message_id=0,
                listing=listing,
            )
        )


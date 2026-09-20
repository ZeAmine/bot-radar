"""Module IA pour la détection des erreurs de prix flagrantes (Price Glitch - Use Case A)."""

import html
import logging
from typing import Any

from app.core.config import settings
from app.db.models.listing import Listing
from app.services.ai.modules.base import BasePostAlertModule
from app.services.ai.schemas import PriceGlitchResult

logger = logging.getLogger(__name__)


class PriceGlitchModule(BasePostAlertModule):
    """Identifie les articles vendus à un prix anormalement bas par rapport à leur valeur réelle."""

    @property
    def name(self) -> str:
        return "price_glitch"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_PRICE_GLITCH)

    def build_prompt_chunk(self, listing: Listing, _alert: Any) -> tuple[str, str] | None:
        if not self.is_enabled or not listing.item_price_cents:
            return None

        price_eur = listing.item_price_cents / 100.0
        instruction = (
            f"Analyse si le prix de {price_eur:.2f} € pour l'article '{listing.title}' "
            f"(catégorie: {listing.category_slug}) est une anomalie flagrante (Price Glitch), "
            f"vendu à au moins 50% sous sa cote normale d'occasion.\n"
            f"Retourne sous la clé 'price_glitch' un objet JSON avec :\n"
            f"- 'is_glitch': bool (true si anomalie flagrante de sous-évaluation)\n"
            f"- 'estimated_market_value_cents': int (cote de revente en centimes)\n"
            f"- 'discount_percent': int (pourcentage de réduction estimé)\n"
            f"- 'reason': string (courte phrase explicative)"
        )
        return "price_glitch", instruction

    def parse_chunk_result(
        self, raw_chunk: Any, _listing: Listing, _alert: Any
    ) -> PriceGlitchResult | None:
        if not isinstance(raw_chunk, dict):
            return None

        try:
            return PriceGlitchResult(
                is_glitch=bool(raw_chunk.get("is_glitch", False)),
                discount_percent=raw_chunk.get("discount_percent"),
                estimated_market_value_cents=raw_chunk.get("estimated_market_value_cents"),
                reason=raw_chunk.get("reason"),
            )
        except Exception as exc:
            logger.warning("Erreur parsing PriceGlitchResult: %s", exc)
            return None

    def format_telegram_html(self, result: PriceGlitchResult | None) -> str | None:
        if not result or not result.is_glitch:
            return None

        cote_text = ""
        if result.estimated_market_value_cents:
            cote_text = f" (Cote : ~{result.estimated_market_value_cents / 100:.2f} €)"

        discount_text = ""
        if result.discount_percent and result.discount_percent > 0:
            discount_text = (
                f"\n⚡ <i>L'article est vendu ~{result.discount_percent}% sous sa valeur !</i>"
            )

        reason = f"\n💡 {html.escape(result.reason)}" if result.reason else ""

        return (
            f"🚨 <b>ANOMALIE DE PRIX DÉTECTÉE</b>{cote_text}"
            f"{discount_text}"
            f"{reason}"
        )


price_glitch_module = PriceGlitchModule()

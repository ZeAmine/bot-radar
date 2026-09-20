"""Module IA pour le scoring de rentabilité et calcul de marge (Use Case H)."""

import html
import logging
from typing import Any

from app.core.config import settings
from app.db.models.listing import Listing
from app.services.ai.modules.base import BasePostAlertModule
from app.services.ai.schemas import ProfitScoringResult

logger = logging.getLogger(__name__)


class ProfitScoringModule(BasePostAlertModule):
    """Calcule la marge prévisionnelle et attribue un score pépite de 1 à 10."""

    @property
    def name(self) -> str:
        return "profit_scoring"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_PROFIT_SCORING)

    def build_prompt_chunk(self, listing: Listing, _alert: Any) -> tuple[str, str] | None:
        if not self.is_enabled or not listing.item_price_cents:
            return None

        total_cents = listing.estimated_total_cents or (listing.item_price_cents + 380)
        total_eur = total_cents / 100.0

        instruction = (
            f"Évalue la rentabilité d'achat/revente pour '{listing.title}' "
            f"au coût d'achat total (avec frais de port et protection) de {total_eur:.2f} €.\n"
            f"Retourne sous la clé 'profit_scoring' un objet JSON avec :\n"
            f"- 'estimated_resale_cents': int (prix de revente estimé en centimes)\n"
            f"- 'net_profit_cents': int (marge nette estimée en centimes = revente - coût total)\n"
            f"- 'pepite_score': float (note de rentabilité de 1.0 à 10.0)\n"
            f"- 'verdict': string (conseil court, ex: 'Très bonne affaire pour revente')"
        )
        return "profit_scoring", instruction

    def parse_chunk_result(
        self, raw_chunk: Any, listing: Listing, _alert: Any
    ) -> ProfitScoringResult | None:
        if not isinstance(raw_chunk, dict):
            return None

        total_cents = listing.estimated_total_cents or (
            (listing.item_price_cents + 380) if listing.item_price_cents else None
        )

        try:
            return ProfitScoringResult(
                purchase_cost_cents=total_cents,
                estimated_resale_cents=raw_chunk.get("estimated_resale_cents"),
                net_profit_cents=raw_chunk.get("net_profit_cents"),
                pepite_score=raw_chunk.get("pepite_score"),
                verdict=raw_chunk.get("verdict"),
            )
        except Exception as exc:
            logger.warning("Erreur parsing ProfitScoringResult: %s", exc)
            return None

    def format_telegram_html(self, result: ProfitScoringResult | None) -> str | None:
        if not result or result.pepite_score is None or result.pepite_score < 6.5:
            # Ne pas encombrer l'utilisateur si la rentabilité est médiocre (< 6.5/10)
            return None

        score_val = result.pepite_score
        score_str = (
            f"{int(score_val)}"
            if score_val == int(score_val)
            else f"{score_val:.1f}".replace(".", ",")
        )

        lines = []

        if result.purchase_cost_cents:
            purchase_eur = result.purchase_cost_cents / 100.0
            purchase_str = f"{purchase_eur:.2f} €".replace(".", ",")
            lines.append(f"💰 Prix d'achat TTC : {purchase_str}")

        if result.estimated_resale_cents:
            resale_eur = result.estimated_resale_cents / 100.0
            resale_str = f"{resale_eur:.2f} €".replace(".", ",")
            lines.append(f"📈 Revente estimée : ~{resale_str}")

        if result.net_profit_cents is not None:
            profit_eur = result.net_profit_cents / 100.0
            sign = "+" if profit_eur > 0 else ""
            profit_str = f"{sign}{profit_eur:.2f} €".replace(".", ",")
            lines.append(f"🚀 Marge nette : <b>{profit_str}</b>")

        lines.append(f"⭐ Indice pépite : {score_str}/10")

        if result.verdict:
            lines.append(f"\n💡 {html.escape(result.verdict)}")

        return "\n".join(lines)


profit_scoring_module = ProfitScoringModule()

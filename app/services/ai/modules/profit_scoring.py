"""Module IA pour le scoring de rentabilité et calcul de marge (Use Case H)."""

import html
import logging
from typing import Any

from pydantic import ValidationError

from app.core.config import settings
from app.core.constants import LEBONCOIN_SOURCE_NAME, CategorySlug
from app.db.models.listing import Listing
from app.services.ai.modules.base import BasePostAlertModule
from app.services.ai.schemas import ProfitScoringPayload, ProfitScoringResult

logger = logging.getLogger(__name__)


class ProfitScoringModule(BasePostAlertModule):
    """Calcule la marge prévisionnelle et attribue un score pépite de 1 à 10."""

    @property
    def name(self) -> str:
        return "profit_scoring"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_PROFIT_SCORING)

    def build_prompt_chunk(self, listing: Listing, alert: Any) -> tuple[str, str] | None:
        if not self.is_enabled or not listing.item_price_cents:
            return None

        # Livres/BD/Mangas exclus : Chasse-aux-Livres fournit déjà la cote.
        if listing.category_slug in (CategorySlug.BD_COMICS.value, "livres", "mangas"):
            return None

        from app.services.price_calculator import calculate_estimated_total

        total_cents = listing.estimated_total_cents
        if total_cents is None and listing.source_name != LEBONCOIN_SOURCE_NAME:
            total_cents = calculate_estimated_total(
                listing.item_price_cents or 0,
                fallback_defaults=True,
            )
        total_cents = total_cents or 0
        total_eur = total_cents / 100.0
        alert_name = getattr(alert, "name", None)
        alert_ctx = f" (recherche ciblée : '{alert_name}')" if alert_name else ""

        instruction = (
            "Évalue le potentiel d'achat-revente pour cet article "
            f"d'occasion : '{listing.title}'{alert_ctx}.\n"
            f"Catégorie : {listing.category_slug or 'Non spécifiée'}\n"
            "Coût d'achat total (frais de port et protection acheteur "
            f"inclus) : {total_eur:.2f} €.\n"
            f"État déclaré : {listing.condition or 'Non précisé'}.\n"
            f"Description : '{listing.description or 'Aucune'}'\n\n"
            "Analyse le marché de l'occasion en France "
            "(Vinted, Leboncoin, eBay) pour ce modèle exact et cet état.\n"
            "Retourne sous la clé 'profit_scoring' un objet JSON avec :\n"
            "- 'estimated_resale_cents': int "
            "(prix de revente réaliste en centimes)\n"
            "- 'net_profit_cents': int "
            "(marge nette estimée en centimes = revente - coût total)\n"
            "- 'pepite_score': float "
            "(note d'opportunité d'arbitrage de 1.0 à 10.0)\n"
            "- 'verdict': string (phrase concise, ex: "
            "'Très recherché, revente rapide avec +35€ de marge')"
        )
        return "profit_scoring", instruction

    def parse_chunk_result(
        self, raw_chunk: Any, listing: Listing, _alert: Any
    ) -> ProfitScoringResult | None:
        if not isinstance(raw_chunk, dict):
            return None

        from app.services.price_calculator import calculate_estimated_total

        total_cents = listing.estimated_total_cents
        if (
            total_cents is None
            and listing.item_price_cents
            and listing.source_name != LEBONCOIN_SOURCE_NAME
        ):
            total_cents = calculate_estimated_total(
                listing.item_price_cents,
                fallback_defaults=True,
            )

        try:
            chunk = ProfitScoringPayload.model_validate(raw_chunk)
            return ProfitScoringResult(
                purchase_cost_cents=total_cents,
                estimated_resale_cents=chunk.estimated_resale_cents,
                net_profit_cents=chunk.net_profit_cents,
                pepite_score=chunk.pepite_score,
                verdict=chunk.verdict,
            )
        except ValidationError:
            logger.warning(
                "Score IA refusé par la validation métier.",
                extra={
                    "event": "ai_validation",
                    "error_category": "business_validation",
                    "ai_module": self.name,
                },
            )
            return None

    def format_telegram_html(self, result: ProfitScoringResult | None) -> str | None:
        if not result or result.pepite_score is None or result.pepite_score < 6.5:
            # Seuil 6.5/10 : ne pas encombrer l'utilisateur pour une rentabilité médiocre.
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
            lines.append(f"💰 <b>Prix d'achat TTC :</b> {purchase_str}")

        if result.estimated_resale_cents:
            resale_eur = result.estimated_resale_cents / 100.0
            resale_str = f"{resale_eur:.2f} €".replace(".", ",")
            lines.append(f"📈 <b>Revente estimée :</b> ~{resale_str}")

        if result.net_profit_cents is not None:
            profit_eur = result.net_profit_cents / 100.0
            sign = "+" if profit_eur > 0 else ""
            profit_str = f"{sign}{profit_eur:.2f} €".replace(".", ",")
            lines.append(f"🚀 <b>Marge nette :</b> {profit_str}")

        lines.append(f"⭐ <b>Indice pépite :</b> {score_str}/10")

        if result.verdict:
            lines.append(f"\n💡 {html.escape(result.verdict)}")

        return "\n".join(lines)


profit_scoring_module = ProfitScoringModule()

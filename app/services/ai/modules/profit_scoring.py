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

        total_cents = self._purchase_cost(listing)
        if total_cents is None:
            return None
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
            "Formule un avis consultatif sur le marché de l'occasion en France, "
            "sans prétendre consulter Vinted, Leboncoin ou eBay en direct.\n"
            "Avant de chiffrer, vérifie que le modèle et les caractéristiques nécessaires "
            "à une estimation sont identifiables dans les données fournies. "
            "Une catégorie, un prix et un titre générique ne suffisent pas. "
            "Si le modèle ou l'état exploitable est insuffisamment identifié, abstiens-toi : "
            "estimated_resale_cents=null, net_profit_cents=null, pepite_score=null, "
            "et verdict explique brièvement ce qui manque. Ne remplace pas l'inconnu par zéro.\n"
            "Même si l'article est identifiable, n'invente pas de cote "
            "faute d'éléments suffisants. "
            "Le calcul revente moins achat est une marge brute estimée avant frais de revente, "
            "pas un bénéfice net. Ne promets ni revente ni rentabilité.\n"
            "Retourne sous la clé 'profit_scoring' un objet JSON avec :\n"
            "- 'estimated_resale_cents': int "
            "(prix de revente estimé en centimes, ou null)\n"
            "- 'net_profit_cents': int "
            "(marge brute estimée en centimes = revente - coût total, ou null)\n"
            "- 'pepite_score': float "
            "(note consultative de 1.0 à 10.0, ou null en cas d'abstention)\n"
            "- 'verdict': string (phrase concise, ex: "
            "'Estimation incertaine : vérifier le modèle et les frais avant achat')"
        )
        return "profit_scoring", instruction

    def parse_chunk_result(
        self, raw_chunk: Any, listing: Listing, _alert: Any
    ) -> ProfitScoringResult | None:
        if not isinstance(raw_chunk, dict):
            return None

        total_cents = self._purchase_cost(listing)

        try:
            chunk = ProfitScoringPayload.model_validate(raw_chunk)
            amounts = (chunk.estimated_resale_cents, chunk.net_profit_cents, chunk.pepite_score)
            if total_cents is None and any(value is not None for value in amounts):
                logger.warning(
                    "Score refusé : coût d'achat inconnu.",
                    extra={
                        "event": "ai_validation",
                        "ai_module": self.name,
                        "error_category": "unknown_purchase_cost",
                    },
                )
                return None
            profit = None
            if chunk.estimated_resale_cents is not None and total_cents is not None:
                profit = chunk.estimated_resale_cents - total_cents
            if chunk.net_profit_cents is not None and chunk.net_profit_cents != profit:
                logger.warning(
                    "Score refusé : marge incohérente.",
                    extra={
                        "event": "ai_validation",
                        "ai_module": self.name,
                        "error_category": "inconsistent_profit",
                    },
                )
                return None
            result = ProfitScoringResult(
                purchase_cost_cents=total_cents,
                estimated_resale_cents=chunk.estimated_resale_cents,
                net_profit_cents=profit,
                pepite_score=chunk.pepite_score,
                verdict=chunk.verdict,
            )
            status = "estimated"
            if result.is_abstention:
                status = "abstained"
            elif any(
                value is None
                for value in (
                    result.estimated_resale_cents,
                    result.net_profit_cents,
                    result.pepite_score,
                )
            ):
                status = "incomplete"
            logger.info(
                "Résultat du scoring IA.",
                extra={"event": "ai_scoring", "ai_module": self.name, "status": status},
            )
            return result
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

    @staticmethod
    def _purchase_cost(listing: Listing) -> int | None:
        if listing.estimated_total_cents is not None:
            return listing.estimated_total_cents
        if listing.item_price_cents is None:
            return None
        if listing.source_name == LEBONCOIN_SOURCE_NAME:
            return listing.item_price_cents
        from app.services.price_calculator import calculate_estimated_total

        return calculate_estimated_total(listing.item_price_cents, fallback_defaults=True)

    def format_telegram_html(self, result: ProfitScoringResult | None) -> str | None:
        if result is None:
            return None
        if result.is_abstention:
            return "💡 <b>Estimation IA non chiffrée :</b> " + html.escape(result.verdict or "")
        if (
            result.pepite_score is None
            or result.pepite_score < 6.5
            or result.purchase_cost_cents is None
            or result.estimated_resale_cents is None
            or result.net_profit_cents is None
        ):
            # Seuil 6.5/10 : ne pas encombrer l'utilisateur pour une rentabilité médiocre.
            return None

        score_val = result.pepite_score
        score_str = (
            f"{int(score_val)}"
            if score_val == int(score_val)
            else f"{score_val:.1f}".replace(".", ",")
        )

        lines = []

        if result.purchase_cost_cents is not None:
            purchase_eur = result.purchase_cost_cents / 100.0
            purchase_str = f"{purchase_eur:.2f} €".replace(".", ",")
            lines.append(f"💰 <b>Prix d'achat TTC :</b> {purchase_str}")

        if result.estimated_resale_cents is not None:
            resale_eur = result.estimated_resale_cents / 100.0
            resale_str = f"{resale_eur:.2f} €".replace(".", ",")
            lines.append(f"📈 <b>Revente estimée :</b> ~{resale_str}")

        if result.net_profit_cents is not None:
            profit_eur = result.net_profit_cents / 100.0
            sign = "+" if profit_eur > 0 else ""
            profit_str = f"{sign}{profit_eur:.2f} €".replace(".", ",")
            lines.append(f"🚀 <b>Marge brute estimée (hors frais de revente) :</b> {profit_str}")

        lines.append(f"⭐ <b>Indice pépite :</b> {score_str}/10")

        if result.verdict:
            lines.append(f"\n💡 {html.escape(result.verdict)}")

        return "\n".join(lines)


profit_scoring_module = ProfitScoringModule()

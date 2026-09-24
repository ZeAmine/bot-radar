"""Module IA pour la génération de message de négociation en 1 clic (Use Case F)."""

import html
import logging

from app.core.config import settings
from app.db.models.listing import Listing
from app.services.ai.client import DeepSeekClient, deepseek_client
from app.services.ai.modules.base import BaseAIModule
from app.services.ai.schemas import NegotiationResult

logger = logging.getLogger(__name__)

NEGOTIATION_SYSTEM_PROMPT = """Tu es un expert en négociation courtoise et efficace sur Vinted.
Tu reçois les détails d'une annonce (titre, prix vendeur, état).
Ta mission est de calculer une offre de prix raisonnable et de générer un message prêt à l'emploi.

Règles de négociation :
- Si prix <= 8 € : rabais symbolique de 1 € max, ou pas de rabais si le prix est déjà très bas.
- Si prix entre 9 € et 30 € : proposer environ 15% à 20% de réduction (arrondi à l'euro).
- Si prix > 30 € : proposer environ 15% à 25% de réduction.
- Le message doit être poli, direct, chaleureux et engageant (promesse d'achat immédiat).

Format JSON attendu :
{
  "suggested_offer_cents": int,
  "message": "Bonjour ! Votre article m'intéresse..."
}"""


class NegotiationModule(BaseAIModule):
    """Génère un message d'offre calibré et prêt à être copié-collé sur Vinted."""

    def __init__(self, client: DeepSeekClient | None = None) -> None:
        self.client = client or deepseek_client

    @property
    def name(self) -> str:
        return "negotiation"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_NEGOTIATION)

    async def generate_offer(self, listing: Listing) -> NegotiationResult | None:
        """Génère une proposition de prix et le message associé."""
        if not self.is_enabled:
            return None

        from app.services.price_calculator import calculate_estimated_total

        price_cents = listing.estimated_total_cents or calculate_estimated_total(
            listing.item_price_cents or 0,
            fallback_defaults=True,
        ) or 0
        if price_cents <= 0:
            return None

        # Si le client DeepSeek est configuré, générer via l'IA
        if self.client.is_configured:
            price_eur = price_cents / 100.0
            user_prompt = (
                f"Article : '{listing.title}'\n"
                f"Prix affiché : {price_eur:.2f} €\n"
                f"État : {listing.condition or 'Non précisé'}\n"
                "Propose le montant de l'offre et rédige le message d'offre."
            )
            data = await self.client.generate_json(
                system_prompt=NEGOTIATION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=0.3,
            )
            if data and data.get("suggested_offer_cents") and data.get("message"):
                offer_cents = int(data["suggested_offer_cents"])
                return NegotiationResult(
                    suggested_offer_cents=offer_cents,
                    suggested_offer_euros=offer_cents / 100.0,
                    message=str(data["message"]).strip(),
                )

        # Fallback algorithmique déterministe (si clé DeepSeek non configurée ou indisponible)
        price_eur = price_cents / 100.0
        if price_eur <= 8:
            discount = 1.0 if price_eur >= 5 else 0.0
        elif price_eur <= 30:
            discount = round(price_eur * 0.18)
        else:
            discount = round(price_eur * 0.20)

        offer_eur = max(1.0, round(price_eur - discount))
        offer_cents = int(offer_eur * 100)
        msg = (
            f"Bonjour ! Votre article m'intéresse beaucoup. "
            f"Seriez-vous d'accord pour {int(offer_eur)}€ avec un achat immédiat aujourd'hui ? "
            f"Merci d'avance et bonne journée !"
        )
        return NegotiationResult(
            suggested_offer_cents=offer_cents,
            suggested_offer_euros=offer_eur,
            message=msg,
        )

    def format_telegram_html(self, result: NegotiationResult) -> str:
        """Formate le message avec balise <code> pour copie en 1 tap."""
        safe_msg = html.escape(result.message)
        return (
            f"💬 <b>Offre suggérée : {result.suggested_offer_euros:.0f} €</b>\n\n"
            f"<i>Touchez le texte ci-dessous pour le copier :</i>\n"
            f"<code>{safe_msg}</code>"
        )


negotiation_module = NegotiationModule()

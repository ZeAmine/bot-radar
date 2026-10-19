"""Module IA pour la génération de message de négociation en 1 clic (Use Case F)."""

import logging

from pydantic import ValidationError

from app.bot.labels import TITLE_OFFER, escape_html, format_price_cents
from app.core.config import settings
from app.core.constants import LEBONCOIN_SOURCE_NAME
from app.db.models.listing import Listing
from app.services.ai.modules.base import BaseAIModule
from app.services.ai.schemas import NegotiationPayload, NegotiationResult

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

        price_cents = listing.estimated_total_cents
        if price_cents is None:
            if listing.source_name == LEBONCOIN_SOURCE_NAME:
                # Leboncoin n'a pas de frais acheteur : le prix affiché est le prix vendeur.
                price_cents = listing.item_price_cents
            else:
                price_cents = calculate_estimated_total(
                    listing.item_price_cents or 0,
                    fallback_defaults=True,
                )
        if not price_cents:
            return None

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
            if isinstance(data, dict):
                try:
                    payload = NegotiationPayload.model_validate(data)
                except ValidationError:
                    logger.warning(
                        "Offre IA refusée par la validation métier.",
                        extra={
                            "event": "ai_validation",
                            "error_category": "business_validation",
                            "ai_module": self.name,
                        },
                    )
                    payload = None
                if payload and payload.suggested_offer_cents and payload.message:
                    offer_cents = int(payload.suggested_offer_cents)
                    return NegotiationResult(
                        suggested_offer_cents=offer_cents,
                        suggested_offer_euros=offer_cents / 100.0,
                        message=payload.message.strip(),
                    )

        # Repli déterministe si le moteur est indisponible ou non configuré.
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
        """Formate le message avec balise <code> pour copie en un toucher.

        Le montant passe par le helper de la charte : « 25,00 € », pas « 25 € ».
        """
        safe_msg = escape_html(result.message)
        return (
            f"{TITLE_OFFER}\n"
            f"{format_price_cents(result.suggested_offer_cents)}\n\n"
            f"<i>Touche le texte ci-dessous pour le copier</i>\n"
            f"<code>{safe_msg}</code>"
        )


negotiation_module = NegotiationModule()

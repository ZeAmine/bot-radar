"""Service d'analyse d'images par IA et détection de boîtes vides / arnaques (Vision Guard)."""

import asyncio
import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.db.models.listing import Listing

logger = logging.getLogger(__name__)

# Mots-clés suspects indiquant une boîte vide ou un objet incomplet (Tech)
EMPTY_BOX_PATTERNS = [
    r"\bboite\s+vide\b",
    r"\bbo[iî]t[eé]\s+seul[e]?\b",
    r"\bboitier\s+seul\b",
    r"\bboitier\s+vide\b",
    r"\bbo[iî]tier\s+sans\s+jeu\b",
    r"\bjaquette\s+seule\b",
    r"\bsans\s+cartouche\b",
    r"\bsans\s+le\s+jeu\b",
    r"\bcarton\s+vide\b",
    r"\bpour\s+pi[eè]ces\b",
    r"\bhs\b",
    r"\bnetzteil\b",
    r"\bcaja\s+vac[ií]a\b",
    r"\bsolo\s+caja\b",
    r"\bsin\s+juego\b",
    r"\bsin\s+cartucho\b",
    r"\bnur\s+(?:ovp|h[uü]lle)\b",
    r"\bohne\s+spiel\b",
    r"\bsolo\s+scatola\b",
    r"\bsenza\s+gioco\b",
]

# Mots-clés suspects indiquant un objet dérivé ou livre incomplet/abîmé (Livres / BD)
BOOK_TRAP_PATTERNS = [
    r"\bfourreau\s+(?:seul|vide)\b",
    r"\bcoffret\s+(?:seul|vide|sans\s+livre)\b",
    r"\bex-?libris\s+seul\b",
    r"\btirage\s+[aà]\s+part\b",
    r"\baffiche\s+seule\b",
    r"\bposter\s+seul\b",
    r"\bmarquepage\s+seul\b",
    r"\bmarque-?page\s+seul\b",
    r"\bextrait\s+gratuit\b",
    r"\bdossier\s+de\s+presse\b",
    r"\bsp[eé]cimen\s+gratuit\b",
    r"\bnon\s+commercialisable\b",
    r"\bpages?\s+(?:d[eé]chir[eé]es?|manquantes?|arrach[eé]es?)\b",
]


class VisionGuardResult:
    def __init__(
        self,
        is_suspicious: bool,
        reason: str | None = None,
        confidence: float = 0.0,
    ) -> None:
        self.is_suspicious = is_suspicious
        self.reason = reason
        self.confidence = confidence


class VisionGuardService:
    """Analyse asynchrone non-bloquante pour la détection d'arnaques (Tech & Livres)."""

    @classmethod
    def analyze_listing(cls, listing: Listing) -> VisionGuardResult:
        """Analyse rapide des indices textuels et métadonnées d'annonce."""
        full_text = f"{listing.title} {listing.description or ''}".lower()

        patterns = EMPTY_BOX_PATTERNS if listing.category_slug != "bd_comics" else BOOK_TRAP_PATTERNS
        for pattern in patterns:
            match = re.search(pattern, full_text)
            if match:
                return VisionGuardResult(
                    is_suspicious=True,
                    reason=f"Mention suspecte détectée dans l'annonce : « {match.group(0)} »",
                    confidence=0.95,
                )

        # Si le prix est anormalement bas (< 6 € pour un jeu ou console) avec mention de boîte
        if listing.category_slug != "bd_comics" and listing.item_price_cents and listing.item_price_cents <= 600:
            if "boite" in full_text or "boîtier" in full_text or "case" in full_text:
                return VisionGuardResult(
                    is_suspicious=True,
                    reason="Prix anormalement bas (≤ 6 €) avec mention de boîte (probable boîtier sans jeu)",
                    confidence=0.85,
                )

        return VisionGuardResult(is_suspicious=False)

    @classmethod
    async def check_and_notify(
        cls,
        bot: Bot,
        chat_id: int,
        message_id: int,
        listing: Listing,
    ) -> None:
        """Exécute l'inspection asynchrone en arrière-plan sans ralentir l'envoi initial."""
        try:
            # Laisser une courte respiration pour que Telegram délivre le premier message
            await asyncio.sleep(0.5)

            result = cls.analyze_listing(listing)
            if result.is_suspicious and result.reason:
                if listing.category_slug == "bd_comics":
                    warning_text = (
                        "⚠️ <b>ATTENTION OBJET DÉRIVÉ OU ÉTAT CRITIQUE</b> ⚠️\n"
                        f"<i>{result.reason}</i>\n\n"
                        "👉 Vérifiez impérativement les photos : l'annonce semble être un objet dérivé (fourreau/ex-libris) ou comporter des défauts majeurs !"
                    )
                else:
                    warning_text = (
                        "⚠️ <b>ATTENTION ARNAQUE SUSPECTÉE</b> ⚠️\n"
                        f"<i>{result.reason}</i>\n\n"
                        "👉 Vérifiez impérativement les photos avant d'acheter : l'annonce pourrait ne contenir que la boîte sans le produit !"
                    )
                await bot.send_message(
                    chat_id=chat_id,
                    text=warning_text,
                    reply_to_message_id=message_id,
                )
                logger.warning(
                    "Avertissement anti-arnaque envoyé pour l'annonce %s (%s)",
                    listing.id,
                    result.reason,
                )
        except TelegramAPIError as exc:
            logger.debug("Impossible d'envoyer l'avertissement vision_guard: %s", exc)
        except Exception as exc:
            logger.exception("Erreur inattendue dans VisionGuardService: %s", exc)

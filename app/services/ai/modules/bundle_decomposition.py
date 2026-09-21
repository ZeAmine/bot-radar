"""Module IA pour la décomposition intelligente des lots et bundles (Use Case D bis)."""

import logging
import re
from typing import Any

from app.core.config import settings
from app.db.models.listing import Listing
from app.services.ai.modules.base import BasePostAlertModule
from app.services.ai.schemas import BundleDecompositionResult

logger = logging.getLogger(__name__)


class BundleDecompositionModule(BasePostAlertModule):
    """Décompose les annonces de lots pour identifier chaque élément et le coût unitaire."""

    @property
    def name(self) -> str:
        return "bundle_decomposition"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_BUNDLE_DECOMPOSITION)

    def build_prompt_chunk(self, listing: Listing, alert: Any) -> tuple[str, str] | None:
        if not self.is_enabled:
            return None

        text = f"{listing.title} {listing.description or ''}".lower()
        candidate_words = (
            "lot", "pack", "intégrale", "ensemble", "collection", "tomes", "jeux"
        )
        if not any(w in text for w in candidate_words):
            return None

        target_name = alert.name if alert else ""
        instruction = (
            f"Analyse si l'annonce correspond à un lot de plusieurs articles/tomes/jeux.\n"
            f"Si oui, liste les tomes identifiés. Vérifie si le produit recherché "
            f"('{target_name}') est explicitement présent dans ce lot.\n"
            f"Retourne sous la clé 'bundle_decomposition' un objet JSON avec :\n"
            f"- 'is_bundle': bool\n"
            f"- 'items_detected': liste de strings (ex: ['Berserk tomes 1 à 3'])\n"
            f"- 'target_item_present': bool (true si l'article ciblé est présent)\n"
            f"- 'summary': string (synthèse en 1 phrase)"
        )
        return "bundle_decomposition", instruction

    def parse_chunk_result(
        self, raw_chunk: Any, listing: Listing, _alert: Any
    ) -> BundleDecompositionResult | None:
        if not isinstance(raw_chunk, dict):
            return None

        try:
            is_bundle = bool(raw_chunk.get("is_bundle", False))
            items = [str(i) for i in raw_chunk.get("items_detected", []) if i]
            count = len(items)

            price_per_item = None
            if is_bundle and count > 1 and listing.item_price_cents:
                price_per_item = listing.item_price_cents // count

            return BundleDecompositionResult(
                is_bundle=is_bundle,
                items_detected=items,
                target_item_present=raw_chunk.get("target_item_present"),
                estimated_price_per_item_cents=price_per_item,
                summary=raw_chunk.get("summary"),
            )
        except Exception as exc:
            logger.warning("Erreur parsing BundleDecompositionResult: %s", exc)
            return None

    @staticmethod
    def extract_tomes(items: list[str]) -> list[int]:
        """Extrait les numéros de tomes détectés par l'IA (ex: ['Blacksad tome 1', 'Blacksad tome 2'] -> [1, 2])."""
        tomes: set[int] = set()
        for item in items:
            # 1. Plage de tomes (ex: "tomes 1 à 3", "1-3")
            range_match = re.search(r"\b([0-9]{1,2})\s*(?:[aà\-]|\sau\s)\s*([0-9]{1,2})\b", item)
            if range_match:
                s, e = int(range_match.group(1)), int(range_match.group(2))
                if 1 <= s < e <= 50 and (e - s) <= 15:
                    tomes.update(range(s, e + 1))
                    continue

            # 2. Numéros individuels
            nums = re.findall(r"\b(?:tome|t|vol|volume|n°|#)?\s*([0-9]{1,2})\b", item, re.IGNORECASE)
            for n in nums:
                val = int(n)
                if 1 <= val <= 50:
                    tomes.add(val)
        return sorted(tomes)

    def format_telegram_html(self, result: BundleDecompositionResult | None) -> str | None:
        """Ne renvoie plus de message séparé.

        L'alerte principale est désormais directement mise à jour en place via
        bot.edit_message_caption.
        """
        return None


bundle_decomposition_module = BundleDecompositionModule()

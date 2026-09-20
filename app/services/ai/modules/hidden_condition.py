"""Module IA pour la détection des défauts cachés dans la description (Use Case D)."""

import html
import logging
from typing import Any

from app.core.config import settings
from app.db.models.listing import Listing
from app.services.ai.modules.base import BasePostAlertModule
from app.services.ai.schemas import HiddenConditionResult

logger = logging.getLogger(__name__)


class HiddenConditionModule(BasePostAlertModule):
    """Analyse la sémantique de la description pour déceler des incohérences avec l'état affiché."""

    @property
    def name(self) -> str:
        return "hidden_condition"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_HIDDEN_CONDITION)

    def build_prompt_chunk(self, listing: Listing, _alert: Any) -> tuple[str, str] | None:
        if not self.is_enabled or not listing.description:
            return None

        declared = listing.condition or "Non précisé"
        instruction = (
            f"Vérifie si la description signale des défauts, usures ou manques "
            f"qui contredisent ou atténuent l'état déclaré ('{declared}').\n"
            f"Retourne sous la clé 'hidden_condition' un objet JSON avec :\n"
            f"- 'has_hidden_defects': bool (true si défauts notables révélés)\n"
            f"- 'real_condition_summary': string (synthèse en 1 phrase courte)\n"
            f"- 'defects_found': liste de strings (ex: ['pages jaunies', 'déchirure'])\n"
            f"- 'severity': 'faible', 'moyen' ou 'critique'"
        )
        return "hidden_condition", instruction

    def parse_chunk_result(
        self, raw_chunk: Any, listing: Listing, _alert: Any
    ) -> HiddenConditionResult | None:
        if not isinstance(raw_chunk, dict):
            return None

        try:
            return HiddenConditionResult(
                has_hidden_defects=bool(raw_chunk.get("has_hidden_defects", False)),
                declared_condition=listing.condition,
                real_condition_summary=raw_chunk.get("real_condition_summary"),
                defects_found=[str(d) for d in raw_chunk.get("defects_found", [])],
                severity=str(raw_chunk.get("severity", "faible")).lower(),
            )
        except Exception as exc:
            logger.warning("Erreur parsing HiddenConditionResult: %s", exc)
            return None

    def format_telegram_html(self, result: HiddenConditionResult | None) -> str | None:
        if not result or not result.has_hidden_defects:
            return None

        icon = "🚨" if result.severity == "critique" else "⚠️"
        defects_str = ", ".join(html.escape(d) for d in result.defects_found)
        summary = html.escape(result.real_condition_summary or defects_str)

        lines = [
            f"{icon} <b>Attention état réel :</b> {summary}",
        ]
        if defects_str and defects_str != summary:
            lines.append(f"<i>Défauts signalés : {defects_str}</i>")

        return "\n".join(lines)


hidden_condition_module = HiddenConditionModule()

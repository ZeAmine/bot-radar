"""Module IA VisionGuard Multi-Photos (Vision Native DeepSeek-V4.1-Flash)."""

import html
import logging

from app.core.config import settings
from app.db.models.listing import Listing
from app.services.ai.client import DeepSeekClient, deepseek_client
from app.services.ai.modules.base import BaseAIModule
from app.services.ai.schemas import VisionGuardAnalysisResult, VisionPhotoInspection

logger = logging.getLogger(__name__)

VISION_SYSTEM_PROMPT = """Tu es l'inspecteur visuel de Radar Bot (détection de défauts Vinted et identification d'articles).
Tu reçois jusqu'à 5 photos d'une annonce avec l'état déclaré et la description.

Examine CHAQUE photo attentivement :
1. Défauts physiques visibles : accrocs, déchirures, coins tassés, plis, pliures, rayures, etc.
2. Incohérences : Défauts contredisant un état déclaré comme 'Neuf' ou 'Très bon état'.
3. Livres, BD, mangas, jeux : identifier le numéro de tome (ex: 1, 2...) ou sous-titre visible sur la couverture ou la tranche (spine).
4. Boîtes vides / arnaques : jaquette seule, fourreau sans livre, cartouche manquante.

Retourne STRICTEMENT un objet JSON :
{
  "has_visual_defects": bool,
  "is_suspicious": bool,
  "summary": "synthèse en 1 phrase claire",
  "identified_tome": int ou null,
  "identified_subtitle": "sous-titre exact ou null",
  "inspections": [
    {
      "photo_index": 1,
      "defects": ["accroc sur le coin"],
      "spines_detected": ["One Piece Tome 1"],
      "is_suspicious_or_empty": false,
      "confidence": 0.95
    }
  ]
}"""


class VisionGuardModule(BaseAIModule):
    """Inspecte l'ensemble des photos d'une annonce via la vision native DeepSeek-V4.1-Flash."""

    def __init__(self, client: DeepSeekClient | None = None) -> None:
        self.client = client or deepseek_client

    @property
    def name(self) -> str:
        return "vision_guard"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_VISION_GUARD)

    async def analyze_listing_photos(
        self, listing: Listing, max_photos: int = 5
    ) -> VisionGuardAnalysisResult | None:
        """Inspecte les photos de l'annonce."""
        if not self.is_enabled or not self.client.is_configured:
            return None

        photos = listing.photos or []
        if not photos:
            return None

        candidate_photos = [p for p in photos if p and p.startswith("http")][:max_photos]
        if not candidate_photos:
            return None

        declared = listing.condition or "Non précisé"
        user_prompt = (
            f"Annonce : '{listing.title}'\n"
            f"Catégorie : {listing.category_slug}\n"
            f"État déclaré par le vendeur : '{declared}'\n"
            f"Description du vendeur : '{listing.description or 'Aucune'}'\n"
            f"Nombre de photos fournies : {len(candidate_photos)}\n"
            "Examine chaque photo dans l'ordre (Photo 1, 2...) et retourne le diagnostic JSON."
        )

        data = await self.client.generate_json(
            system_prompt=VISION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            image_urls=candidate_photos,
            temperature=0.1,
        )
        if not data:
            return None

        try:
            inspections: list[VisionPhotoInspection] = []
            for item in data.get("inspections", []):
                idx = int(item.get("photo_index", 1))
                photo_url = candidate_photos[idx - 1] if 0 < idx <= len(candidate_photos) else ""
                inspections.append(
                    VisionPhotoInspection(
                        photo_index=idx,
                        photo_url=photo_url,
                        defects=[str(d) for d in item.get("defects", []) if d],
                        spines_detected=[str(s) for s in item.get("spines_detected", []) if s],
                        is_suspicious_or_empty=bool(item.get("is_suspicious_or_empty", False)),
                        confidence=float(item.get("confidence", 0.0)),
                    )
                )

            tome_raw = data.get("identified_tome")
            identified_tome: int | None = None
            if isinstance(tome_raw, int):
                identified_tome = tome_raw
            elif isinstance(tome_raw, str) and tome_raw.isdigit():
                identified_tome = int(tome_raw)

            sub_raw = data.get("identified_subtitle")
            identified_subtitle = str(sub_raw).strip() if sub_raw and str(sub_raw).lower() != "null" else None

            return VisionGuardAnalysisResult(
                has_visual_defects=bool(data.get("has_visual_defects", False)),
                is_suspicious=bool(data.get("is_suspicious", False)),
                summary=str(data.get("summary", "")).strip(),
                identified_tome=identified_tome,
                identified_subtitle=identified_subtitle,
                inspections=inspections,
            )
        except Exception as exc:
            logger.warning("Erreur parsing VisionGuardAnalysisResult: %s", exc)
            return None

    def format_telegram_html(self, result: VisionGuardAnalysisResult | None) -> str | None:
        """Formate le diagnostic visuel pour Telegram."""
        if not result or (not result.has_visual_defects and not result.is_suspicious):
            return None

        lines: list[str] = []

        if result.is_suspicious:
            lines.append("🚨 <b>ALERTE OBJET SUSPECT / BOÎTE VIDE</b> 🚨")
            if result.summary:
                lines.append(f"<i>{html.escape(result.summary)}</i>")
            return "\n".join(lines)

        # Si défauts visuels détectés
        lines.append("👁️ <b>Défauts visuels repérés sur les photos :</b>")
        for insp in result.inspections:
            if insp.defects:
                defects_str = ", ".join(html.escape(d) for d in insp.defects)
                lines.append(f"  • <b>Photo {insp.photo_index} :</b> {defects_str}")

        if result.summary:
            lines.append(f"💡 <i>{html.escape(result.summary)}</i>")

        return "\n".join(lines)


vision_guard_module = VisionGuardModule()

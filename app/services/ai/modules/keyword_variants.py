"""Module IA pour la recherche de pépites cachées et fautes de frappe (Use Case E)."""

import logging

from pydantic import ValidationError

from app.core.config import settings
from app.services.ai.client import DeepSeekClient, deepseek_client
from app.services.ai.modules.base import BaseAIModule
from app.services.ai.schemas import KeywordVariantsPayload

logger = logging.getLogger(__name__)

OPTIMIZE_SYSTEM_PROMPT = (
    "Tu es un expert des annonces Vinted.\n"
    "Pour un titre de recherche donné et sa catégorie :\n"
    "1. Génère les variantes orthographiques fréquentes que les vendeurs "
    "écrivent par erreur :\n"
    "   - Fautes de frappe courantes (ex: 'Nintedo', 'Playstasion', 'Mangats')\n"
    "   - Variantes phonétiques ou oublis d'espace/apostrophe "
    "(ex: 'onepiece', 'dbz', 'larabe du futur')\n"
    "   - Mots mal orthographiés fréquents\n"
    "2. Propose 2 à 5 mots-clés d'exclusion spécifiquement adaptés à "
    "l'œuvre/produit ciblé et à sa catégorie pour bloquer les faux positifs "
    "(ex: produits dérivés spécifiques, goodies, affiches, boîtes vides, "
    "figurines, accessoires non souhaités).\n"
    "\n"
    "Retourne STRICTEMENT un objet JSON :\n"
    "{\n"
    '  "variants": ["variante1", "variante2"],\n'
    '  "exclusions": ["exclusion1", "exclusion2"]\n'
    "}"
)


class KeywordVariantsModule(BaseAIModule):
    """Génère des variantes orthographiques et des exclusions intelligentes pour les alertes."""

    def __init__(self, client: DeepSeekClient | None = None) -> None:
        self.client = client or deepseek_client

    @property
    def name(self) -> str:
        return "keyword_variants"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_KEYWORD_VARIANTS)

    async def generate_variants_and_exclusions(
        self, title: str, category_slug: str = "bd_comics"
    ) -> tuple[list[str], list[str]]:
        """Génère à la fois les variantes orthographiques et les exclusions
        intelligentes adaptées au produit."""
        if not self.is_enabled or not self.client.is_configured:
            return [], []

        user_prompt = f"Produit recherché : '{title.strip()}' (Catégorie: {category_slug})"
        data = await self.client.generate_json(
            system_prompt=OPTIMIZE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
        )
        if not data or not isinstance(data, dict):
            return [], []

        try:
            payload = KeywordVariantsPayload.model_validate(data)
        except ValidationError:
            return [], []
        raw_variants = payload.variants
        raw_exclusions = payload.exclusions

        variants = [
            str(v).strip().lower()
            for v in raw_variants
            if v and str(v).strip()
        ]
        exclusions = [
            str(e).strip().lower()
            for e in raw_exclusions
            if e and str(e).strip()
        ]
        return variants, exclusions

    async def generate_variants(self, keyword: str) -> list[str]:
        """Génère les variantes pour un mot-clé donné (rétrocompatibilité)."""
        variants, _ = await self.generate_variants_and_exclusions(keyword)
        return variants


keyword_variants_module = KeywordVariantsModule()

"""Module IA pour la recherche de pépites cachées et fautes de frappe (Use Case E)."""

import logging
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.core.config import settings
from app.services.ai.modules.base import BaseAIModule
from app.services.ai.schemas import BookContext, KeywordVariantsPayload
from app.utils.text import normalize_text

logger = logging.getLogger(__name__)

# Nombre maximal d'exclusions conservées sur une alerte.
MAX_EXCLUSIONS = 6

OPTIMIZE_SYSTEM_PROMPT = (
    "Tu es un expert des annonces Vinted et de la recherche d'occasion.\n"
    "Tu conçois les mots-clés d'une alerte de surveillance pour UN produit donné.\n"
    "\n"
    "Deux tâches :\n"
    "1. « variants » : les orthographes alternatives avec lesquelles un vendeur "
    "saisit réellement ce même produit (fautes de frappe, oublis d'espace, "
    "d'accent ou d'apostrophe, variantes phonétiques, abréviations courantes).\n"
    "2. « exclusions » : 3 à 6 mots-clés présents dans le TITRE d'une annonce "
    "Vinted qui doivent faire rejeter cette annonce.\n"
    "\n"
    "Règle essentielle pour « exclusions » : une exclusion vaut uniquement si elle "
    "correspond à une confusion RÉELLE pour CETTE œuvre précise. Raisonne à partir de "
    "ce que tu sais de l'ouvrage : ce qui porte le même titre sans être ce livre "
    "(homonymes, autres auteurs, adaptation, tome voisin), ce qui l'accompagne "
    "(goodies, produits dérivés, matérialisation d'un objet de l'histoire), ce qui "
    "signale une annonce sans le livre (coffret ou boîte vide, pièce seule, "
    "photo d'un autre tome), et ce qui contredit la recherche (autre édition "
    "imprimée quand l'utilisateur vise un exemplaire précis).\n"
    "Si tu ne trouves pas de confusion spécifique plausible, propose moins "
    "d'exclusions : une exclusion générique sans rapport avec l'ouvrage est pire "
    "qu'une exclusion justifiée.\n"
    "INTERDIT : rendre une liste générique valable pour n'importe quelle œuvre "
    "(par exemple des mots de goodies qui n'ont aucun rapport avec l'ouvrage).\n"
    "\n"
    "Retourne STRICTEMENT un objet JSON :\n"
    "{\n"
    '  "variants": ["variante1"],\n'
    '  "exclusions": ["exclusion1"],\n'
    '  "rationale": "une phrase expliquant les confusions visées"\n'
    "}"
)

CATEGORY_CONTEXT = {
    "bd_comics": "BD, mangas, comics, romans illustrés : surveille les doublons d'éditeur, "
    "les tomes incohérents, les langues et les tirages.",
    "tech": "Matériel électronique : surveille les accessoires et pièces vendus seuls, "
    "les modèles voisins, les états défectueux.",
    "gaming": "Jeux et consoles : surveille les jeux sans console, les pièces détachées, "
    "les réimpressions douteuses.",
    "clothes": "Vêtements : surveille le badging ou la contrefaçon, les tailles divergentes, "
    "les pièces détachées.",
}


def _clean_terms(raw_terms: list[object], title: str) -> list[str]:
    """Normalise, déduplique et filtre des mots-clés face au titre recherché."""
    norm_title = normalize_text(title)
    cleaned: list[str] = []
    for term in raw_terms:
        value = str(term).strip().lower()
        if not value or len(value) > 40:
            continue
        norm_term = normalize_text(value)
        # Un mot-clé contenu dans le titre bloquerait l'annonce cible elle-même.
        if not norm_term or norm_term in norm_title:
            continue
        if any(existing == value for existing in cleaned):
            continue
        cleaned.append(value)
    return cleaned


@dataclass(frozen=True)
class KeywordVariantsOutcome:
    """Ce que la recherche a produit, et si elle aabouti.

    Distinguer deux situations que l'ancien retour `(list, list)` confondait :

    - le moteur a répondu et n'a trouvé ni variante ni exclusion pertinente — il a
      tranché, et c'est une information utile ;
    - l'appel a échoué : timeout, erreur HTTP, ou JSON que la validation refuse.

    Dans le second cas la liste vide est un mensonge : elle se lit dans le récap
    comme un avis du moteur, alors que personne n'a rien demandé à personne. Le
    chemin `/add` s'appuie sur `answered` pour dire ce qui s'est réellement passé.
    """

    variants: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    answered: bool = False

    @property
    def is_empty(self) -> bool:
        """Aucune variante et aucune exclusion : le moteur a tranché pour « rien »."""
        return not self.variants and not self.exclusions

    @classmethod
    def unanswered(cls) -> "KeywordVariantsOutcome":
        return cls()


class KeywordVariantsModule(BaseAIModule):
    """Génère des variantes orthographiques et des exclusions propres à chaque alerte."""


    @property
    def name(self) -> str:
        return "keyword_variants"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_KEYWORD_VARIANTS)

    async def generate_variants_and_exclusions(
        self,
        title: str,
        category_slug: str = "bd_comics",
        context: BookContext | None = None,
    ) -> KeywordVariantsOutcome:
        """Génère à la fois les variantes orthographiques et les exclusions
        intelligentes adaptées au produit, à partir de sa fiche réelle quand elle existe.

        Ne lève jamais : l'IA reste consultative. Le résultat dit en plus si le moteur
        a réellement répondu, pour que l'appelant puisse distinguer une absence de
        résultat d'une panne.
        """
        if not self.is_enabled or not self.client.is_configured:
            logger.info(
                "Recherche de mots-clés ignorée pour « %s » : module désactivé ou "
                "moteur non configuré.",
                title,
            )
            return KeywordVariantsOutcome.unanswered()

        user_prompt = self._build_user_prompt(title, category_slug, context)
        data = await self.client.generate_json(
            system_prompt=OPTIMIZE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
        )
        if not data or not isinstance(data, dict):
            return KeywordVariantsOutcome.unanswered()

        try:
            payload = KeywordVariantsPayload.model_validate(data)
        except ValidationError as exc:
            logger.warning(
                "Réponse du moteur illisible pour « %s » : %s",
                title,
                str(exc)[:200],
            )
            return KeywordVariantsOutcome.unanswered()

        variants = _clean_terms(payload.variants or [], title)
        exclusions = _clean_terms(payload.exclusions or [], title)[:MAX_EXCLUSIONS]

        if payload.rationale:
            logger.info(
                "Mots-clés IA pour « %s » : %s",
                title,
                str(payload.rationale)[:200],
            )
        return KeywordVariantsOutcome(
            variants=variants,
            exclusions=exclusions,
            answered=True,
        )

    @staticmethod
    def _build_user_prompt(
        title: str,
        category_slug: str,
        context: BookContext | None,
    ) -> str:
        """Assemble le contexte transmis au modèle, faits bibliographiques en tête."""
        category_hint = CATEGORY_CONTEXT.get(category_slug, "produit d'occasion courant")
        lines = [
            f"Produit recherché : '{title.strip()}'",
            f"Catégorie : {category_hint}",
        ]
        if context:
            lines.append("Fiche vérifiée de l'ouvrage :")
            lines.extend(f"- {fact}" for fact in context.as_facts())
            lines.append(
                "Appuie-toi sur cette fiche pour viser les confusions propres à cet "
                "ouvrage, pas sur des produits dérivés génériques."
            )
        else:
            lines.append(
                "Aucune fiche vérifiée n'est disponible : déduis les exclusions de ce que "
                "tu sais du produit et des usages du marché de l'occasion."
            )
        return "\n".join(lines)

    async def generate_variants(self, keyword: str) -> list[str]:
        """Génère les variantes pour un mot-clé donné (rétrocompatibilité)."""
        outcome = await self.generate_variants_and_exclusions(keyword)
        return outcome.variants


keyword_variants_module = KeywordVariantsModule()
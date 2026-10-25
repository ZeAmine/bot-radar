"""Module IA pour la recherche de pépites cachées et fautes de frappe (Use Case E)."""

import logging
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.core.config import settings
from app.domain.rules.keyword_hygiene import (
    SanitizedValues,
    assess_variants,
    sanitize_exclusions,
)
from app.services.ai.modules.base import BaseAIModule
from app.services.ai.schemas import BookContext, KeywordVariantsPayload

logger = logging.getLogger(__name__)

OPTIMIZE_SYSTEM_PROMPT = (
    "Tu es un expert des annonces Vinted et de la recherche d'occasion.\n"
    "Tu conçois les mots-clés d'une alerte de surveillance pour UN produit donné.\n"
    "\n"
    "Deux tâches :\n"
    "1. « variants » : les orthographes alternatives avec lesquelles un vendeur "
    "saisit réellement ce même produit (fautes de frappe, oublis d'espace, "
    "d'accent ou d'apostrophe, variantes phonétiques, abréviations courantes).\n"
    "Zéro variante est une réponse valide : si aucune autre écriture utile ne vient, "
    "réponds variants=[]. Ne cherche jamais à remplir un quota. "
    "Écarte les doublons et les écritures qui ne changent que la casse, les accents, "
    "la ponctuation ou les espaces superflus. Le code applique ensuite sa normalisation "
    "et ses filtres ; tu n'as pas à en reproduire l'algorithme.\n"
    "Une variante est une autre écriture du MÊME produit, jamais un produit voisin : "
    "ni un autre modèle (« s24 » pour « S23 »), ni un autre tome, ni une autre édition, "
    "ni un accessoire. L'application vérifie cette identité de façon déterministe — une "
    "variante qui change un nombre du titre, ou qui s'en écarte trop, est refusée. "
    "Inutile donc de la proposer : elle coûte un aller-retour et ne sera pas retenue.\n"
    "2. « exclusions » : 0 à 3 mots-clés présents dans le TITRE d'une annonce "
    "Vinted qui doivent faire rejeter cette annonce.\n"
    "\n"
    "Règle essentielle pour « exclusions » : une exclusion vaut uniquement si elle "
    "correspond à une confusion RÉELLE pour CETTE œuvre précise. Raisonne à partir de "
    "ce que tu sais de l'ouvrage : ce qui porte le même titre sans être ce livre "
    "(homonymes, autres auteurs, adaptation), ce qui l'accompagne "
    "(goodies, produits dérivés, matérialisation d'un objet de l'histoire), ce qui "
    "signale une annonce sans le livre (coffret ou boîte vide, pièce seule, "
    "photo d'un autre tome), et ce qui contredit la recherche (autre édition "
    "imprimée quand l'utilisateur vise un exemplaire précis).\n"
    "Une liste VIDE est une réponse attendue : elle dit qu'aucune confusion n'est "
    "nommable pour cet ouvrage, et c'est mieux qu'une exclusion de remplissage. "
    "N'invente jamais une exclusion pour atteindre un nombre.\n"
    "INTERDIT : une exclusion obtenue en collant un numéro au titre "
    "(« … #2 », « … 2 »). Le numéro d'un tome n'est pas un mot du titre : ce n'est "
    "pas une confusion, et l'alerte se tairait alors sur ce tome. Si le titre ne "
    "porte aucun numéro, ne nomme aucun numéro de tome.\n"
    "INTERDIT : rendre une liste générique valable pour n'importe quelle œuvre "
    "(par exemple des mots de goodies qui n'ont aucun rapport avec l'ouvrage).\n"
    "INTERDIT : tout ISBN, code-barres, éditeur ou année dans les variantes : une "
    "variante est une façon d'écrire le TITRE, pas une référence d'un autre ouvrage. "
    "Une variante ne répète jamais le titre, elle en est une autre écriture.\n"
    "BORNE « variants » : au plus 6, toutes réellement distinctes. Quand le titre "
    "porte un numéro (« Vol. 2 », « Tome 2 »), n'énumère JAMAIS ses écritures : ni "
    "ordinaux (« 2nd », « deuxième »), ni chiffres romains (« II »), ni suffixes de "
    "lettres (« 2a », « 2b », ... jusqu'à « 2z »). Une variante change "
    "l'ORTHOGRAPHE du titre, jamais le numéro qui le suit.\n"
    "INTERDIT dans « exclusions » : un terme purement numérique (« 1 », « 2 ») ou "
    "d'un seul caractère — une exclusion est un rejet, et « 1 » rejetterait toute "
    "annonce contenant ce chiffre.\n"
    "\n"
    "Retourne STRICTEMENT un objet JSON :\n"
    "{\n"
    '  "variants": [],\n'
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
    proposed_variants: tuple[str, ...] | None = None
    variant_assessment: SanitizedValues | None = None

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
        payload = await self._request_payload(title, user_prompt)
        if payload is None:
            return KeywordVariantsOutcome.unanswered()

        variant_assessment = assess_variants(payload.variants or [], title)
        exclusions = sanitize_exclusions(payload.exclusions or [], title)

        return KeywordVariantsOutcome(
            variants=variant_assessment.values,
            exclusions=exclusions,
            answered=True,
            proposed_variants=tuple(payload.variants or []),
            variant_assessment=variant_assessment,
        )

    async def _request_payload(
        self, title: str, user_prompt: str
    ) -> KeywordVariantsPayload | None:
        """Interroge le moteur une seule fois et valide sa réponse."""
        data = await self.client.generate_json(
            system_prompt=OPTIMIZE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            response_model=KeywordVariantsPayload,
            ai_module=self.name,
        )
        if not isinstance(data, dict):
            logger.info("Recherche de mots-clés pour « %s » : aucune réponse exploitable.", title)
            return None
        try:
            return KeywordVariantsPayload.model_validate(data)
        except ValidationError:
            logger.warning(
                "Réponse du moteur invalide pour la recherche de mots-clés.",
                extra={
                    "event": "ai_validation",
                    "ai_module": self.name,
                    "error_category": "business_validation",
                },
            )
            return None

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

"""Module IA pour la création d'alertes en langage naturel (/add magique)."""

import logging
import re

from app.core.config import settings
from app.core.constants import STANDARD_CONDITIONS, CategorySlug
from app.services.ai.client import DeepSeekClient, deepseek_client
from app.services.ai.modules.base import BaseAIModule
from app.services.ai.schemas import NaturalAddResult

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Tu es l'assistant de configuration de BotRadar (bot d'alertes Vinted).
L'utilisateur formule une recherche en langage naturel.
Extrais les paramètres d'alerte sous un format JSON strict :
- "name": Titre épuré et propre de l'objet/œuvre (ex: "L'Arabe du futur", "One Piece", "iPhone 13").
  IMPORTANT : Ne JAMAIS inclure de prix (ex: "max 7€", "15€"), de mention d'état ou de filtre dans "name" !
- "category_slug": Une seule valeur parmi ["bd_comics", "tech", "gaming", "clothes"].
  * bd_comics: livres, mangas, BD, comics, romans.
  * gaming: consoles, jeux vidéo, manettes.
  * tech: smartphones, PC, électronique.
  * clothes: vêtements, baskets.
- "is_series": bool (true s'il s'agit d'une série multi-tomes sans numéro de tome précisé ex: "L'Arabe du futur", "One Piece", "Blacksad" ; false s'il s'agit d'un livre unique / one-shot ex: "Le Monde sans fin", "Sapiens" ou d'un tome spécifique ex: "L'Arabe du futur tome 2", "Daredevil Deluxe 1").
- "isbn13": string ou null (si c'est un livre unique ou un tome spécifique, fournis son ISBN-13 officiel à 13 chiffres commençant par 978 ou 979 sans tirets ; null s'il s'agit d'une série globale ou si incertain).
- "author": string ou null (nom de l'auteur principal si identifiable).
- "publisher": string ou null (éditeur ou collection spécifique ex: "Dargaud", "Panini Comics", "Marvel Deluxe").
- "include_keywords": Liste de mots-clés obligatoires pour la recherche (ex: ["L'Arabe du futur"]).
  IMPORTANT : Ne jamais inclure de prix ou de filtre de prix dans les mots-clés !
- "exclude_keywords": Liste de mots-clés à exclure pour bloquer les faux positifs.
  IMPORTANT : Propose TOUJOURS 2 à 5 exclusions intelligentes spécifiquement adaptées à l'objet/livre et à sa catégorie (ex: pour "L'Arabe du futur" ou des BD/mangas: ["figurine", "coffret vide", "poster", "ex-libris", "tote bag"] ; pour "iPhone 13" ou tech: ["coque", "boite vide", "vitre", "pour pieces"] ; pour "Nintendo Switch" ou gaming: ["boite vide", "housse", "support", "stickers"]), en plus de celles éventuellement demandées par l'utilisateur.
- "max_price_cents": Prix max en centimes (ex: 35€ -> 3500, 7€ -> 700, null si non spécifié).
- "conditions": Liste des états parmi : ["Neuf avec étiquette", "Neuf sans étiquette",
  "Très bon état", "Bon état", "Satisfaisant"]. Mettre null si non spécifié.
- "explanation": Courte phrase expliquant ce qui a été compris.

Réponds TOUJOURS avec un objet JSON valide contenant exactement ces champs."""


class NaturalAddModule(BaseAIModule):
    """Parse une recherche en langage naturel pour configurer une alerte instantanément."""

    def __init__(self, client: DeepSeekClient | None = None) -> None:
        self.client = client or deepseek_client

    @property
    def name(self) -> str:
        return "natural_add"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_NATURAL_ADD)

    async def parse_prompt(self, user_text: str) -> NaturalAddResult | None:
        """Parse le texte utilisateur et retourne les paramètres de l'alerte."""
        if not self.is_enabled or not self.client.is_configured:
            return None

        prompt = f"Requête utilisateur : {user_text.strip()}"
        data = await self.client.generate_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            temperature=0.1,
        )
        if not data:
            return None

        try:
            category = data.get("category_slug", "bd_comics")
            valid_cats = {c.value for c in CategorySlug}
            if category not in valid_cats:
                category = "bd_comics"

            # Nettoyer et valider les états
            raw_conditions = data.get("conditions")
            valid_conditions = None
            if isinstance(raw_conditions, list):
                valid_conditions = [c for c in raw_conditions if c in STANDARD_CONDITIONS]
                if not valid_conditions:
                    valid_conditions = None

            PRICE_SUFFIX_REGEX = re.compile(
                r"\s*[-–—:]?\s*(?:(?:max|maximum|sous|moins\s+de|<|<=)\s*\d+(?:[.,]\d+)?(?:\s*(?:€|eur|euros))?|\d+(?:[.,]\d+)?\s*(?:€|eur|euros))\s*$",
                re.IGNORECASE,
            )

            raw_name = str(data.get("name", user_text[:40])).strip()
            # Nettoyage de sécurité : retirer tout suffixe de prix ou de filtre résiduel (ex: "- max 7€", "max 7 €", "7€")
            clean_name = PRICE_SUFFIX_REGEX.sub("", raw_name).strip() or raw_name

            # Nettoyer include_keywords pour retirer d'éventuels suffixes de prix
            raw_includes = data.get("include_keywords") or [clean_name]
            clean_includes = []
            for k in raw_includes:
                k_clean = PRICE_SUFFIX_REGEX.sub("", str(k).strip()).strip()
                if k_clean and k_clean not in clean_includes:
                    clean_includes.append(k_clean)

            # Nettoyer exclude_keywords
            clean_excludes = [
                str(k).strip() for k in data.get("exclude_keywords", []) if k and str(k).strip()
            ]

            is_series = bool(data.get("is_series", False))
            raw_isbn = str(data.get("isbn13") or "").strip()
            isbn13 = re.sub(r"[^0-9X]", "", raw_isbn.upper()) if raw_isbn else None
            if isbn13 and len(isbn13) not in (10, 13):
                isbn13 = None

            author = str(data.get("author")).strip() if data.get("author") else None
            publisher = str(data.get("publisher")).strip() if data.get("publisher") else None

            # Si c'est un livre/BD (pas une série globale) et qu'aucun ISBN n'a été fourni par l'IA
            if category == "bd_comics" and not is_series and not isbn13:
                try:
                    from app.services.book_metadata_service import BookMetadataService
                    bnf_service = BookMetadataService()
                    meta = await bnf_service.lookup_by_title_and_author(clean_name)
                    if meta and meta.isbn13:
                        isbn13 = meta.isbn13
                        author = author or meta.author
                        publisher = publisher or meta.publisher
                except Exception as bnf_err:
                    logger.debug("Échec lookup BNF pour '%s': %s", clean_name, bnf_err)

            return NaturalAddResult(
                name=clean_name,
                category_slug=category,
                isbn13=isbn13,
                is_series=is_series,
                author=author,
                publisher=publisher,
                include_keywords=clean_includes or [clean_name],
                exclude_keywords=clean_excludes,
                max_price_cents=data.get("max_price_cents"),
                conditions=valid_conditions,
                explanation=data.get("explanation"),
            )
        except Exception as exc:
            logger.warning("Erreur lors de la construction de NaturalAddResult: %s", exc)
            return None


natural_add_module = NaturalAddModule()

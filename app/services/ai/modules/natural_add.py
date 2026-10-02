"""Module IA pour la création d'alertes en langage naturel (/add magique)."""

import logging
import re

from pydantic import ValidationError

from app.core.config import settings
from app.core.constants import STANDARD_CONDITIONS, CategorySlug
from app.services.ai.client import DeepSeekClient, deepseek_client
from app.services.ai.modules.base import BaseAIModule
from app.services.ai.schemas import BookContext, NaturalAddPayload, NaturalAddResult
from app.services.book_metadata_service import (
    BookMetadata,
    BookMetadataService,
    EditionAlternative,
    format_edition_details,
)
from app.utils.isbn import extract_clean_isbn, is_valid_isbn, normalize_isbn

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Tu es l'assistant de configuration de BotRadar (bot d'alertes Vinted).\n"
    "L'utilisateur formule une recherche en langage naturel.\n"
    "Extrais les paramètres d'alerte sous un format JSON strict :\n"
    '- "name": Titre épuré et propre de l\'objet/œuvre '
    '(ex: "L\'Arabe du futur", "One Piece", "iPhone 13").\n'
    '  IMPORTANT : Ne JAMAIS inclure de prix (ex: "max 7€", "15€"), '
    'de mention d\'état ou de filtre dans "name" !\n'
    '- "category_slug": Une seule valeur parmi '
    '["bd_comics", "tech", "gaming", "clothes"].\n'
    "  * bd_comics: livres, mangas, BD, comics, romans.\n"
    "  * gaming: consoles, jeux vidéo, manettes.\n"
    "  * tech: smartphones, PC, électronique.\n"
    "  * clothes: vêtements, baskets.\n"
    '- "is_series": bool (true s\'il s\'agit d\'une série multi-tomes sans '
    'numéro de tome précisé ex: "L\'Arabe du futur", "One Piece", "Blacksad" ; '
    'false s\'il s\'agit d\'un livre unique / one-shot ex: "Le Monde sans fin", '
    '"Sapiens" ou d\'un tome spécifique ex: "L\'Arabe du futur tome 2", '
    '"Daredevil Deluxe 1").\n'
    '- "isbn13": string ou null. Règle stricte : un ISBN est un code de 13 '
    "chiffres identifiant un ouvrage précis. N'invente jamais un ISBN et ne le "
    'devine pas à partir d\'un éditeur ou d\'une année : réponds null sauf si '
    "l'utilisateur l'a lui-même écrit dans sa demande (tu le recopies alors "
    "tel quel). L'ISBN définitif est de toute façon confirmé par la BNF.\n"
    '- "author": string ou null (nom de l\'auteur principal si identifiable).\n'
    '- "publisher": string ou null (éditeur ou collection spécifique '
    'ex: "Dargaud", "Panini Comics", "Marvel Deluxe").\n'
    '- "include_keywords": Liste de mots-clés obligatoires pour la recherche '
    '(ex: ["L\'Arabe du futur"]).\n'
    "  IMPORTANT : Ne jamais inclure de prix ou de filtre de prix dans "
    "les mots-clés !\n"
    '- "exclude_keywords": Liste de mots-clés à exclure pour bloquer les faux '
    "positifs.\n"
    "  IMPORTANT : ces exclusions se raisonnent pour CETTE œuvre précise, à partir "
    "de ce que tu sais d'elle (ce qui porte le même titre sans être cet ouvrage, "
    "ce qui l'accompagne, ce qui contredit la recherche). N'écris pas une liste "
    "de goodies génériques qui conviendrait à n'importe quel livre : si tu n'as "
    "rien de spécifique en tête, réponds une liste vide.\n"
    '- "max_price_cents": Prix max en centimes '
    "(ex: 35€ -> 3500, 7€ -> 700, null si non spécifié).\n"
    '- "conditions": Liste des états parmi : ["Neuf avec étiquette", '
    '"Neuf sans étiquette", "Très bon état", "Bon état", "Satisfaisant"]. '
    "Mettre null si non spécifié.\n"
    '- "explanation": Courte phrase expliquant ce qui a été compris.\n'
    "\n"
    "Réponds TOUJOURS avec un objet JSON valide contenant exactement "
    "ces champs."
)


OPENLIBRARY_SOURCE_NOTE = "Source OpenLibrary (repli BNF)"


def _source_note(meta: BookMetadata) -> str | None:
    """Signale au récap une donnée d'identité issue d'une autorité secondaire."""
    return OPENLIBRARY_SOURCE_NOTE if meta.source == "openlibrary" else None


class NaturalAddModule(BaseAIModule):
    """Parse une recherche en langage naturel pour configurer une alerte instantanément.

    L'ISBN n'est jamais celui du modèle : il est resolvé puis vérifié par la BNF,
    seule autorité bibliographique du projet.
    """

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
            payload = NaturalAddPayload.model_validate(data)
            category = payload.category_slug
            valid_cats = {c.value for c in CategorySlug}
            if category not in valid_cats:
                category = "bd_comics"

            # Nettoyer et valider les états
            raw_conditions = payload.conditions
            valid_conditions = None
            if isinstance(raw_conditions, list):
                valid_conditions = [c for c in raw_conditions if c in STANDARD_CONDITIONS]
                if not valid_conditions:
                    valid_conditions = None

            price_suffix_regex = re.compile(
                r"\s*[-–—:]?\s*(?:(?:max|maximum|sous|moins\s+de|<|<=)\s*\d+(?:[.,]\d+)?(?:\s*(?:€|eur|euros))?|\d+(?:[.,]\d+)?\s*(?:€|eur|euros))\s*$",
                re.IGNORECASE,
            )

            raw_name = str(payload.name or user_text[:40]).strip()
            # Nettoyage de sécurité : retirer tout suffixe de prix ou de filtre résiduel (ex: "- max
            # 7€", "max 7 €", "7€")
            clean_name = price_suffix_regex.sub("", raw_name).strip() or raw_name

            # Nettoyer include_keywords pour retirer d'éventuels suffixes de prix
            raw_includes = payload.include_keywords or [clean_name]
            clean_includes = []
            for k in raw_includes:
                k_clean = price_suffix_regex.sub("", str(k).strip()).strip()
                if k_clean and k_clean not in clean_includes:
                    clean_includes.append(k_clean)

            # Nettoyer exclude_keywords
            clean_excludes = [
                str(k).strip()
                for k in (payload.exclude_keywords or [])
                if k and str(k).strip()
            ]

            is_series = payload.is_series
            author = payload.author.strip() if payload.author else None
            publisher = payload.publisher.strip() if payload.publisher else None

            isbn13, book, isbn_note, edition_alternatives = await self._resolve_isbn(
                title=clean_name,
                category=category,
                is_series=is_series,
                author_hint=author,
                candidates=(
                    extract_clean_isbn(user_text),
                    self._sanitize_ai_isbn(payload.isbn13),
                ),
            )
            if book:
                # La BNF fait autorité sur l'auteur et l'éditeur quand elle a répondu.
                author = author or book.author
                publisher = publisher or book.publisher

            return NaturalAddResult(
                name=clean_name,
                category_slug=category,
                isbn13=isbn13,
                is_series=is_series,
                author=author,
                publisher=publisher,
                include_keywords=clean_includes or [clean_name],
                exclude_keywords=clean_excludes,
                max_price_cents=payload.max_price_cents,
                conditions=valid_conditions,
                explanation=payload.explanation,
                isbn_note=isbn_note,
                edition_alternatives=edition_alternatives,
                book_context=(
                    BookContext(
                        title=book.title,
                        author=book.author,
                        publisher=book.publisher,
                        publication_year=book.publication_year,
                        format_detail=book.format_detail,
                        isbn13=book.isbn13,
                        is_series=is_series,
                    )
                    if book
                    else None
                ),
            )
        except (ValidationError, Exception) as exc:
            logger.warning("Erreur lors de la construction de NaturalAddResult: %s", exc)
            return None

    @staticmethod
    def _sanitize_ai_isbn(raw_isbn: str | None) -> str | None:
        """Garde la proposition du modèle seulement si elle est un ISBN bien formé."""
        if not raw_isbn:
            return None
        candidate = normalize_isbn(str(raw_isbn))
        if candidate and not is_valid_isbn(candidate):
            logger.info("Proposition ISBN du modèle ignorée (clé de contrôle invalide) : %s",
                        candidate)
            return None
        return candidate

    async def _resolve_isbn(
        self,
        title: str,
        category: str,
        is_series: bool,
        author_hint: str | None,
        candidates: tuple[str | None, ...],
    ) -> tuple[str | None, BookMetadata | None, str | None, list[EditionAlternative]]:
        """Détermine l'ISBN d'une alerte, la notice qui l'atteste et les éditions concurrentes.

        Ordre de priorité : ISBN saisi explicitement par l'utilisateur, puis ISBN
        proposé par le modèle, puis ISBN de la meilleure notice BNF du titre. Chaque
        candidat doit être confirmé par une notice BNF portant réellement sur l'œuvre.
        """
        if category != CategorySlug.BD_COMICS.value or is_series or not title:
            return None, None, None, []

        typed_isbn, ai_isbn = candidates
        service = BookMetadataService()

        resolution = None
        try:
            resolution = await service.resolve_for_alert(title, author_hint)
        except Exception as exc:
            logger.warning("Résolution BNF impossible pour « %s » : %s", title, exc)

        for candidate in (typed_isbn, ai_isbn):
            if not candidate or (resolution and resolution.best.isbn13 == candidate):
                continue
            try:
                verified = await service.verify_isbn(candidate, title)
            except Exception as exc:
                logger.warning("Vérification BNF impossible pour l'ISBN %s : %s", candidate, exc)
                continue
            if verified:
                return (
                    candidate,
                    verified,
                    _source_note(verified),
                    resolution.editions() if resolution else [],
                )

        if resolution:
            notes = [note for note in (_source_note(resolution.best),) if note]
            alternatives = ", ".join(
                f"{alt.isbn13} ({format_edition_details(alt.publication_year, alt.format_detail)})"
                for alt in resolution.alternatives
                if format_edition_details(alt.publication_year, alt.format_detail)
            )
            if alternatives:
                notes.append(f"Autre édition connue : {alternatives}")
            note = " • ".join(notes) or None
            return (resolution.best.isbn13, resolution.best, note, resolution.editions())

        if typed_isbn:
            return (
                None,
                None,
                f"ISBN saisi non confirmé par la BNF : surveillance par titre sur « {title} »",
                [],
            )
        if ai_isbn:
            return (
                None,
                None,
                f"Aucun ISBN confirmé par la BNF pour « {title} » : surveillance par titre",
                [],
            )
        return None, None, None, []


natural_add_module = NaturalAddModule()
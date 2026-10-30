"""Module IA pour la création d'alertes en langage naturel (/add magique)."""

import logging
import re
from dataclasses import dataclass, field, replace

from pydantic import ValidationError

from app.core.config import settings
from app.core.constants import STANDARD_CONDITIONS, CategorySlug
from app.domain.enums import BookSearchStatus
from app.domain.rules.book_volume import requested_volume, volume_title
from app.domain.rules.series_detection import SeriesSignal
from app.services.ai.modules.base import BaseAIModule
from app.services.ai.schemas import (
    NaturalAddPayload,
    NaturalAddResult,
    book_context_from,
)
from app.services.book_metadata_service import (
    BookMetadata,
    EditionAlternative,
    WorkAlternative,
    generate_recommended_keywords,
    parse_query_title_and_author,
)
from app.use_cases.book_search import (
    BookSearchResult,
    BookSearchUseCase,
    creation_bibliographic_identity,
    metadata_from_result,
)
from app.utils.isbn import extract_clean_isbn
from app.utils.text import normalize_text

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
    "  Ce champ décrit le périmètre de surveillance, pas une identité bibliographique "
    "confirmée. Ne déduis jamais une série de la seule absence de numéro. "
    "Une demande explicite de suivre toute une série se distingue d'un tome précis. "
    "Si tu ne reconnais pas l'œuvre et que la demande ne tranche pas, n'affirme pas "
    "qu'il s'agit d'un one-shot : explique l'incertitude dans explanation. "
    "La résolution catalogue peut établir une série à partir de notices numérotées ; "
    "son silence n'est pas une preuve d'œuvre unique.\n"
    '- "isbn13": string ou null. Règle stricte : un ISBN est un code de 13 '
    "chiffres identifiant un ouvrage précis. N'invente jamais un ISBN et ne le "
    'devine pas à partir d\'un éditeur ou d\'une année : réponds null sauf si '
    "l'utilisateur l'a lui-même écrit dans sa demande (tu le recopies alors "
    "tel quel). L'ISBN définitif est de toute façon confirmé par la BNF.\n"
    '- "author": string ou null (nom de l\'auteur principal si identifiable).\n'
    '- "publisher": string ou null (éditeur ou collection spécifique '
    'ex: "Dargaud", "Panini Comics", "Marvel Deluxe").\n'
    '- "include_keywords": Minimum de termes nécessaires pour identifier la recherche, '
    'en minuscules (ex: ["arabe du futur"]). Chaque élément est OBLIGATOIRE (ET), '
    'une expression exige ses mots consécutifs dans cet ordre. Privilégie le titre seul '
    'quand il suffit. Ne rends jamais deux formulations alternatives comme inclusions. '
    'N\'ajoute pas systématiquement auteur, éditeur ou marque : les vendeurs les omettent. '
    'Pour un produit, conserve le modèle, la génération ou la plateforme nécessaires '
    'à la demande. Sépare les éléments uniquement s\'ils doivent pouvoir apparaître '
    'à des endroits différents (ex: ["veste", "dickies"]). Pas de découpage automatique '
    'en mots ni de nombre minimum de critères : une alerte démarre peu restrictive.\n'
    "  IMPORTANT : Ne jamais inclure de prix ou de filtre de prix dans "
    "les mots-clés !\n"
    '- "exclude_keywords": Liste de mots-clés à exclure pour bloquer les faux '
    "positifs.\n"
    "  IMPORTANT : ces exclusions se raisonnent pour CETTE œuvre précise, à partir "
    "de ce que tu sais d'elle (ce qui porte le même titre sans être cet ouvrage, "
    "ce qui l'accompagne, ce qui contredit la recherche). N'écris pas une liste "
    "de goodies génériques qui conviendrait à n'importe quel livre : si tu n'as "
    "rien de spécifique en tête, réponds une liste vide.\n"
    "  Au plus 3 exclusions. Une liste vide est une réponse attendue : mieux vaut "
    "aucune exclusion qu'une exclusion de remplissage.\n"
    "  INTERDIT : une exclusion obtenue en collant un numéro au titre "
    "(« … #2 », « … 2 »). Le numéro d'un tome n'est pas un mot du titre : "
    "l'exclusion ne désigne alors aucune confusion. Si le titre que tu rends ne "
    "porte aucun numéro, ne nomme aucun numéro de tome.\n"
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


def _editions_of(result: BookSearchResult) -> list[EditionAlternative]:
    """Éditions du contrat converties pour le brouillon `natural_add`."""
    return [
        EditionAlternative(
            isbn13=edition.isbn13,
            publisher=edition.publisher,
            publication_year=edition.publication_year,
            format_detail=edition.format_detail,
            volume_number=edition.volume_number,
            language=edition.language,
        )
        for edition in result.editions
    ]


def _works_of(result: BookSearchResult) -> list[WorkAlternative]:
    """Œuvres homonymes du contrat converties pour le brouillon `natural_add`."""
    return [
        WorkAlternative(
            isbn13=work.isbn13,
            title=work.title,
            author=work.author,
            publisher=work.publisher,
            publication_year=work.publication_year,
        )
        for work in result.works[:3]
        if work.isbn13
    ]


@dataclass(frozen=True)
class _ResolvedWork:
    """Ce que la recherche bibliographique a permis d'établir sur l'œuvre demandée.

    Regroupe ce qui doit être dit sur l'œuvre (ISBN, notice, provenance) et ce qui doit
    être proposé à l'utilisateur (éditions concurrentes, œuvres homonymes). Tout vide,
    c'est une surveillance par titre : c'est le récap qui l'explique, jamais le silence.
    """

    isbn13: str | None = None
    book: BookMetadata | None = None
    isbn_note: str | None = None
    edition_alternatives: list[EditionAlternative] = field(default_factory=list)
    work_choices: list[WorkAlternative] = field(default_factory=list)
    # Ce que la résolution a pu prouver sur la nature de l'œuvre. Vide quand aucune
    series: SeriesSignal = field(default_factory=SeriesSignal)
    # Une recherche de titre a été menée et **aucune** source n'a rien rendu : ni
    unidentified: bool = False
    search_result: BookSearchResult | None = None


class NaturalAddModule(BaseAIModule):
    """Parse une recherche en langage naturel pour configurer une alerte instantanément.

    L'ISBN n'est jamais celui du modèle : il est resolvé puis vérifié par la BNF,
    seule autorité bibliographique du projet.
    """


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
            response_model=NaturalAddPayload,
            ai_module=self.name,
        )
        if not data:
            return None

        try:
            payload = NaturalAddPayload.model_validate(data)
            category = payload.category_slug
            valid_cats = {c.value for c in CategorySlug}
            if category not in valid_cats:
                category = "bd_comics"

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
            clean_name = price_suffix_regex.sub("", raw_name).strip() or raw_name

            raw_includes = payload.include_keywords or [clean_name]
            clean_includes = []
            for k in raw_includes:
                k_clean = normalize_text(price_suffix_regex.sub("", k.strip()))
                if k_clean and k_clean not in clean_includes:
                    clean_includes.append(k_clean)

            clean_excludes = [
                k.strip()
                for k in (payload.exclude_keywords or [])
                if k and k.strip()
            ]

            number = (
                requested_volume(user_text) if category == CategorySlug.BD_COMICS.value else None
            )
            if number is not None:
                clean_name = volume_title(clean_name, number)
            is_series = (False if number is not None or extract_clean_isbn(user_text)
                         else payload.is_series)
            author = payload.author.strip() if payload.author else None
            publisher = payload.publisher.strip() if payload.publisher else None

            # prix de fin de saisie n'est pas un mot d'auteur.
            typed_query = price_suffix_regex.sub("", user_text).strip()
            author_hint = author or parse_query_title_and_author(typed_query)[1]
            if author_hint and normalize_text(author_hint) == normalize_text(clean_name):
                author_hint = None
            search_title = clean_name
            resolved = await self._resolve_isbn(
                title=clean_name,
                category=category,
                is_series=is_series,
                author_hint=author_hint,
                typed_isbn=extract_clean_isbn(user_text),
            )
            if resolved.book:
                clean_name = (volume_title(resolved.book.title, number)
                              if number else resolved.book.title)
                # La BNF fait autorité sur l'auteur et l'éditeur dès qu'elle a
                author = resolved.book.author or author
                publisher = resolved.book.publisher or publisher

            book = resolved.book
            if extract_clean_isbn(user_text) and book and book.volume_number:
                number = book.volume_number
                clean_name = volume_title(clean_name, number)

            # La résolution peut révéler une œuvre numérotée que le moteur n'avait pas
            series = resolved.series
            if series.is_series:
                is_series = True

            if category == CategorySlug.BD_COMICS.value:
                clean_includes, _, _ = generate_recommended_keywords(clean_name)

            return NaturalAddResult(
                original_input=user_text,
                search_queries=list(dict.fromkeys([typed_query, search_title])),
                bibliographic_identity=(creation_bibliographic_identity(resolved.search_result)
                                        if resolved.search_result else (
                                            {"author": None, "publisher": None, "language": None,
                                             "source": None, "publication_year": None,
                                             "format_detail": None}
                                            if category == CategorySlug.BD_COMICS.value else {})),
                name=clean_name,
                requested_volume=number,
                category_slug=category,
                isbn13=None if series.is_series else resolved.isbn13,
                is_series=is_series,
                author=author,
                publisher=publisher,
                include_keywords=clean_includes or [normalize_text(clean_name)],
                exclude_keywords=clean_excludes,
                max_price_cents=payload.max_price_cents,
                conditions=valid_conditions,
                explanation=payload.explanation,
                isbn_note=None if series.is_series else resolved.isbn_note,
                book_unidentified=False if series.is_series else resolved.unidentified,
                edition_alternatives=(
                    [] if series.is_series else resolved.edition_alternatives
                ),
                work_choices=resolved.work_choices,
                book_context=(
                    book_context_from(book, is_series=is_series) if book else None
                ),
            )
        except ValidationError:
            logger.warning(
                "Réponse du moteur invalide pour la création d’alerte.",
                extra={
                    "event": "ai_validation",
                    "ai_module": self.name,
                    "error_category": "business_validation",
                },
            )
            return None
        except Exception as exc:
            logger.warning(
                "Erreur lors de la construction de NaturalAddResult (%s).",
                type(exc).__name__,
            )
            return None

    async def _resolve_isbn(
        self,
        title: str,
        category: str,
        is_series: bool,
        author_hint: str | None,
        typed_isbn: str | None,
    ) -> _ResolvedWork:
        """Détermine l'ISBN d'une alerte, la notice qui l'atteste et les œuvres homonymes.

        Un ISBN saisi et les notices de titre sont confirmés par les autorités.

        Un titre homonyme (« Les Enfants perdus », roman de 1989 et BD de 2025) ne
        rattache rien : ni ISBN, ni auteur, ni éditeur. Renvoyer la fiche du roman
        erreur comme confirmée contaminerait le matching, l'URL de recherche et la cote
        de marché, et survivrait à une correction d'ISBN.

        Un ISBN explicitement saisi par l'utilisateur nomme un tome : la détection de
        série ne le remplace pas, elle ne s'applique qu'à une résolution par titre.
        """
        if category != CategorySlug.BD_COMICS.value or not title:
            return _ResolvedWork()

        # Une suggestion de série du moteur ne confirme jamais l'identité.
        is_series = is_series and not typed_isbn
        use_case = BookSearchUseCase(declared_source=True)

        result = None
        try:
            result = await use_case.search_by_title(
                title, author_hint, include_series_catalogue=not typed_isbn or is_series,
            )
        except Exception as exc:
            logger.warning("Résolution BNF impossible pour « %s » : %s", title, exc)

        ambiguous = result is not None and result.status == BookSearchStatus.AMBIGUOUS
        work_choices = _works_of(result) if ambiguous and result else []
        confirmed = metadata_from_result(result) if result else None

        if typed_isbn and (confirmed is None or confirmed.isbn13 != typed_isbn):
            candidate = typed_isbn
            try:
                isbn_result = await use_case.search_by_isbn(candidate, title)
            except Exception as exc:
                logger.warning("Vérification BNF impossible pour l'ISBN %s : %s", candidate, exc)
                isbn_result = None
            verified = metadata_from_result(isbn_result) if isbn_result else None
            if verified is not None and isbn_result is not None:
                # Les éditions concurrentes ne valent que si le titre désigne une œuvre
                return _ResolvedWork(
                    isbn13=candidate,
                    book=verified,
                    isbn_note=isbn_result.note,
                    search_result=isbn_result,
                    edition_alternatives=(
                        [] if ambiguous or result is None else _editions_of(result)
                    ),
                    work_choices=work_choices,
                )

        if ambiguous and result is not None:
            note = result.note or ""
            if typed_isbn:
                # L'utilisateur a lui-même donné cet ISBN : le garder vaut mieux que de
                return _ResolvedWork(
                    isbn13=typed_isbn,
                    isbn_note=f"{note}. ISBN saisi, non confirmé par la BNF",
                    work_choices=work_choices,
                )
            return _ResolvedWork(isbn_note=note, work_choices=work_choices, search_result=result)

        if confirmed is not None and result is not None:
            # Les éditions concurrentes sont portées par `edition_alternatives`, que le
            return _ResolvedWork(
                isbn13=result.reference.isbn13 if result.reference else confirmed.isbn13,
                book=confirmed,
                isbn_note=result.note,
                edition_alternatives=_editions_of(result),
                # La nature de l'œuvre se lit sur toute la résolution, notices voisines
                series=SeriesSignal(is_series=result.is_series and not typed_isbn),
                search_result=(replace(result, is_series=False, series_volumes=())
                               if typed_isbn else result),
            )

        if result is not None and result.is_series and not typed_isbn:
            return _ResolvedWork(
                series=SeriesSignal(is_series=True), search_result=result, isbn_note=result.note,
            )
        if typed_isbn:
            return _ResolvedWork(
                search_result=result,
                isbn_note=(
                    f"ISBN saisi non confirmé par la BNF : surveillance par titre sur « {title} »"
                )
            )
        # Ni notice, ni ISBN d'aucune sorte : l'ouvrage n'est pas identifié, et c'est
        return _ResolvedWork(unidentified=True, search_result=result)


natural_add_module = NaturalAddModule()

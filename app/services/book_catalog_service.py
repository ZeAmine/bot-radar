"""Service de gestion du catalogue interne BD & Comics et d'identification de livres."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import MatchConfidence
from app.db.models.book import Book
from app.db.repositories.book_repo import BookRepository
from app.utils.isbn import isbn10_to_isbn13
from app.utils.text import normalize_text


class BookCatalogService:
    """Service d'identification des BD et d'association aux alertes."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.book_repo = BookRepository(session)

    async def add_book(
        self,
        title: str,
        isbn13: str | None = None,
        isbn10: str | None = None,
        author: str | None = None,
        publisher: str | None = None,
        publication_year: int | None = None,
        edition: str | None = None,
        format_book: str | None = None,
        language: str = "fr",
        cover_url: str | None = None,
    ) -> Book:
        """Ajoute un livre au catalogue de référence."""
        normalized_title = normalize_text(title)
        return await self.book_repo.create(
            title=title,
            normalized_title=normalized_title,
            isbn13=isbn13,
            isbn10=isbn10,
            author=author,
            publisher=publisher,
            publication_year=publication_year,
            edition=edition,
            format_book=format_book,
            language=language,
            cover_url=cover_url,
        )

    async def identify_book(
        self,
        *,
        isbn13: str | None = None,
        isbn10: str | None = None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
    ) -> tuple[Book | None, MatchConfidence | None]:
        """Identifie un livre selon les règles de confiance strictes.

        Règles :
        1. ISBN-13 exact : confiance high.
        2. ISBN-10 converti ou équivalent : confiance high.
        3. Titre + auteur + édition : confiance medium.
        4. Titre seul : confiance low (non utilisé pour comparaison automatique).
        5. Aucun match : (None, None).
        """
        # 1. Recherche par ISBN-13
        if isbn13 and len(isbn13.strip()) == 13:
            book = await self.book_repo.get_by_isbn13(isbn13.strip())
            if book:
                return book, MatchConfidence.HIGH

        # 2. Recherche par ISBN-10 converti
        if isbn10 and len(isbn10.strip()) == 10:
            converted = isbn10_to_isbn13(isbn10.strip())
            if converted:
                book = await self.book_repo.get_by_isbn13(converted)
                if book:
                    return book, MatchConfidence.HIGH

        # 3. Recherche par titre normalisé
        normalized_query_title = normalize_text(title)
        candidate_books = await self.book_repo.get_by_normalized_title(normalized_query_title)

        if not candidate_books:
            return None, None

        # Si auteur et édition correspondent
        if author:
            norm_author = normalize_text(author)
            for b in candidate_books:
                if b.author and normalize_text(b.author) == norm_author:
                    if (
                        edition
                        and b.edition
                        and normalize_text(b.edition) == normalize_text(edition)
                    ):
                        return b, MatchConfidence.MEDIUM
                    return b, MatchConfidence.MEDIUM

        # 4. Titre seul (confiance low)
        return candidate_books[0], MatchConfidence.LOW

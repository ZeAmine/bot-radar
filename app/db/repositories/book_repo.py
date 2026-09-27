"""Dépôt d'accès aux données pour le catalogue de livres BD & Comics."""

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import AlertStatus, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.services.tome_extractor import TomeExtractor, TomeMatchResult


class BookRepository:
    """Gestion des opérations en base de données pour les livres et liaisons d'alertes."""

    _BOOK_CACHE_LOAD = (selectinload(Book.market_cache),)
    _BOOK_EAGER_LOAD = (
        selectinload(Book.market_cache),
        selectinload(Book.snapshots),
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, book_id: uuid.UUID) -> Book | None:
        """Récupère un livre par son UUID avec son cache de marché."""
        stmt = (
            select(Book)
            .options(*self._BOOK_EAGER_LOAD)
            .where(Book.id == book_id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_isbn13(self, isbn13: str) -> Book | None:
        """Recherche un livre par son ISBN-13 exact."""
        stmt = (
            select(Book)
            .options(*self._BOOK_CACHE_LOAD)
            .where(Book.isbn13 == isbn13.strip())
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_normalized_title(self, normalized_title: str) -> list[Book]:
        """Recherche des livres par titre normalisé."""
        stmt = (
            select(Book)
            .options(*self._BOOK_EAGER_LOAD)
            .where(Book.normalized_title == normalized_title.strip())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create(
        self,
        title: str,
        normalized_title: str,
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
        """Crée et persiste un livre dans le catalogue interne."""
        book = Book(
            isbn13=isbn13.strip() if isbn13 else None,
            isbn10=isbn10.strip() if isbn10 else None,
            title=title,
            normalized_title=normalized_title,
            author=author,
            publisher=publisher,
            publication_year=publication_year,
            edition=edition,
            format=format_book,
            language=language,
            cover_url=cover_url,
        )
        self.session.add(book)
        await self.session.flush()
        return book

    async def clear_alert_books(self, alert_id: uuid.UUID) -> None:
        """Retire toutes les liaisons livre d'une alerte avant d'en poser une nouvelle."""
        await self.session.execute(delete(AlertBook).where(AlertBook.alert_id == alert_id))

    async def link_alert_to_book(
        self,
        alert_id: uuid.UUID,
        book_id: uuid.UUID,
        confidence: MatchConfidence = MatchConfidence.HIGH,
    ) -> AlertBook:
        """Associe une alerte à un livre avec son niveau de confiance."""
        # Vérifier si l'association existe déjà
        stmt = select(AlertBook).where(
            AlertBook.alert_id == alert_id,
            AlertBook.book_id == book_id,
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing:
            existing.match_confidence = confidence
            return existing

        alert_book = AlertBook(
            alert_id=alert_id,
            book_id=book_id,
            match_confidence=confidence,
        )
        self.session.add(alert_book)
        await self.session.flush()
        return alert_book

    async def get_books_for_active_bd_alerts(self) -> list[Book]:
        """Charge tous les livres uniques associés à des alertes BD actives."""
        stmt = (
            select(Book)
            .join(AlertBook, Book.id == AlertBook.book_id)
            .join(Alert, AlertBook.alert_id == Alert.id)
            .join(Category, Alert.category_id == Category.id)
            .options(selectinload(Book.market_cache))
            .where(
                Category.slug == "bd_comics",
                Alert.status == AlertStatus.ACTIVE,
            )
            .distinct()
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_books_needing_price_refresh(self, limit: int = 8) -> list[Book]:
        """Charge un lot prioritaire de livres BD dont le cache est expiré, absent ou ancien.
        Évite tout pic de charge en étalant les rafraîchissements en tâche de fond.
        """
        now = datetime.now(UTC)
        subq = (
            select(Book.id)
            .join(AlertBook, Book.id == AlertBook.book_id)
            .join(Alert, AlertBook.alert_id == Alert.id)
            .join(Category, Alert.category_id == Category.id)
            .outerjoin(BookMarketCache, Book.id == BookMarketCache.book_id)
            .where(
                Category.slug == "bd_comics",
                Alert.status == AlertStatus.ACTIVE,
                BookMarketCache.expires_at.is_(None)
                | (BookMarketCache.expires_at <= now + timedelta(hours=2))
                | BookMarketCache.checked_at.is_(None),
            )
            .group_by(Book.id, BookMarketCache.checked_at)
            .order_by(BookMarketCache.checked_at.asc().nulls_first())
            .limit(limit)
        )
        book_ids = list((await self.session.execute(subq)).scalars().all())
        if not book_ids:
            return []
        stmt = (
            select(Book)
            .options(selectinload(Book.market_cache))
            .where(Book.id.in_(book_ids))
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_book_for_alert(self, alert_id: uuid.UUID) -> Book | None:
        """Récupère le livre associé à une alerte avec son cache de marché préchargé."""
        books = await self.get_all_books_for_alert(alert_id)
        if not books:
            return None
        # Privilégier un livre qui possède déjà un cache de prix valide (live ou historique 1 an)
        for b in books:
            if b.market_cache and (
                b.market_cache.lowest_delivered_price_cents is not None
                or b.market_cache.historical_low_1y_cents is not None
            ):
                return b
        return books[0]

    async def get_all_books_for_alert(self, alert_id: uuid.UUID) -> list[Book]:
        """Charge tous les livres associés à une alerte avec leurs caches de marché préchargés."""
        stmt = (
            select(Book)
            .join(AlertBook, Book.id == AlertBook.book_id)
            .options(*self._BOOK_CACHE_LOAD)
            .where(AlertBook.alert_id == alert_id)
            .order_by(Book.title.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def resolve_book_for_listing(
        self,
        alert_id: uuid.UUID,
        alert_name: str,
        listing_title: str,
        listing_description: str | None = None,
    ) -> tuple[Book | None, list[Book], TomeMatchResult]:
        """Résout le livre le plus approprié pour une annonce Vinted
        en détectant le tome ou le lot.
        """
        books = await self.get_all_books_for_alert(alert_id)
        return resolve_loaded_books(
            alert_name=alert_name,
            listing_title=listing_title,
            listing_description=listing_description,
            books=books,
        )


def resolve_loaded_books(
    *,
    alert_name: str,
    listing_title: str,
    listing_description: str | None,
    books: list[Book],
) -> tuple[Book | None, list[Book], TomeMatchResult]:
    """Choisit le tome en CPU à partir de livres déjà chargés (zéro SQL)."""
    tome_res = TomeExtractor.extract(
        title=listing_title,
        description=listing_description,
        series_key=alert_name,
    )
    if not books:
        return None, [], tome_res

    if tome_res.is_lot:
        if tome_res.lot_tomes:
            matching_lot = [
                b
                for b in books
                if any(
                    TomeExtractor.extract(b.title, series_key=alert_name).tome_number == t
                    or re.search(
                        rf"\b(?:tome|t|vol|livre)\s*\.?\s*{t}\b",
                        b.title,
                        re.IGNORECASE,
                    )
                    for t in tome_res.lot_tomes
                )
            ]
            return None, matching_lot if matching_lot else books, tome_res
        return None, books, tome_res

    if tome_res.tome_number is not None:
        t_num = tome_res.tome_number
        for b in books:
            b_tome = TomeExtractor.extract(b.title, series_key=alert_name)
            if b_tome.tome_number == t_num:
                return b, books, tome_res

        for b in books:
            if re.search(rf"\b(?:tome|t|vol|livre)\s*\.?\s*{t_num}\b", b.title, re.IGNORECASE):
                return b, books, tome_res

        if tome_res.matched_subtitle:
            sub = tome_res.matched_subtitle.lower()
            for b in books:
                if sub in b.title.lower():
                    return b, books, tome_res

        return None, books, tome_res

    if len(books) == 1:
        return books[0], books, tome_res

    return None, books, tome_res

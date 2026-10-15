"""Dépôt d'accès aux données pour le catalogue de livres BD & Comics."""

import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import AlertStatus, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.services.tome_extractor import TomeExtractor, TomeMatchResult
from app.utils.text import normalize_text

# Un livre dont la collecte vient d'échouer ne revient pas en tête du lot suivant :
# le job glissant passe toutes les trente minutes, et ``checked_at`` vide le
# replaçait devant tous les autres. On le retente après ce repos.
FAILED_COLLECTION_REST_HOURS = 6


class BookRepository:
    """Gestion des opérations en base de données pour les livres et liaisons d'alertes."""

    _BOOK_CACHE_LOAD = (selectinload(Book.market_cache),)
    _BOOK_EAGER_LOAD = (
        selectinload(Book.market_cache),
        selectinload(Book.snapshots),
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _refresh_annual(self, books: list[Book]) -> None:
        """Projection de lecture groupée : ni nouvelle observation, ni écriture."""
        from app.db.repositories.market_price_repo import MarketPriceRepository

        await MarketPriceRepository(self.session).refresh_annual_references(
            [book.id for book in books],
            persist=False,
        )

    async def get_by_id(self, book_id: uuid.UUID, *, refresh: bool = False) -> Book | None:
        """Récupère un livre par son UUID avec son cache de marché."""
        stmt = (
            select(Book)
            .options(*self._BOOK_EAGER_LOAD)
            .where(Book.id == book_id)
            .execution_options(populate_existing=refresh)
        )
        result = await self.session.execute(stmt)
        book = result.scalar_one_or_none()
        await self._refresh_annual([book] if book else [])
        return book

    async def get_by_isbn13(self, isbn13: str) -> Book | None:
        """Recherche un livre par son ISBN-13 exact."""
        stmt = select(Book).options(*self._BOOK_CACHE_LOAD).where(Book.isbn13 == isbn13.strip())
        result = await self.session.execute(stmt)
        book = result.scalar_one_or_none()
        await self._refresh_annual([book] if book else [])
        return book

    async def get_by_normalized_title(self, normalized_title: str) -> list[Book]:
        """Recherche des livres par titre normalisé."""
        stmt = (
            select(Book)
            .options(*self._BOOK_EAGER_LOAD)
            .where(Book.normalized_title == normalized_title.strip())
        )
        result = await self.session.execute(stmt)
        books = list(result.scalars().all())
        await self._refresh_annual(books)
        return books

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
        await self._lock_membership(alert_id)
        removed = await self.session.execute(
            delete(AlertBook).where(AlertBook.alert_id == alert_id).returning(AlertBook.book_id)
        )
        if removed.scalars().first() is not None:
            await self._invalidate_series_total(alert_id)

    async def _lock_membership(self, alert_id: uuid.UUID) -> None:
        """Même verrou d'alerte que les écritures de cote et d'identité."""
        await self.session.execute(select(Alert.id).where(Alert.id == alert_id).with_for_update())

    async def _invalidate_series_total(self, alert_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Alert)
            .where(
                Alert.id == alert_id,
                Alert.series_total_cents.is_not(None),
            )
            .values(series_total_cents=None)
        )
        changed: set[uuid.UUID] = self.session.info.setdefault("series_totals_invalidated", set())
        changed.add(alert_id)

    def take_invalidated_series_ids(self) -> set[uuid.UUID]:
        """Identifiants à relire par le calcul central, jamais un ensemble de livres capturé."""
        return cast(set[uuid.UUID], self.session.info.pop("series_totals_invalidated", set()))

    async def ensure_confirmed_book(
        self, *, isbn13: str, title: str, author: str | None, publisher: str | None,
        publication_year: int | None, format_book: str | None,
    ) -> Book:
        """Insère une notice confirmée sans écraser une fiche concurrente ou préexistante."""
        insert = (
            pg_insert if self.session.get_bind().dialect.name == "postgresql" else sqlite_insert
        )
        await self.session.execute(insert(Book).values(
            isbn13=isbn13, title=title, normalized_title=normalize_text(title), author=author,
            publisher=publisher, publication_year=publication_year, format=format_book,
        ).on_conflict_do_nothing(index_elements=["isbn13"]))
        book = await self.get_by_isbn13(isbn13)
        if book is None:
            raise RuntimeError("Notice confirmée non relue après insertion.")
        return book

    async def link_alert_to_book(
        self,
        alert_id: uuid.UUID,
        book_id: uuid.UUID,
        confidence: MatchConfidence = MatchConfidence.HIGH,
    ) -> AlertBook:
        """Associe une alerte à un livre avec son niveau de confiance."""
        await self._lock_membership(alert_id)
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
        await self._invalidate_series_total(alert_id)
        return alert_book

    async def get_books_for_active_bd_alerts(self) -> list[Book]:
        """Charge les livres uniques des alertes BD conservées, actives ou en pause."""
        watched = select(AlertBook.book_id).join(Alert).join(Category).where(
            Category.slug == "bd_comics", Alert.deleted_at.is_(None),
            Alert.status.in_([AlertStatus.ACTIVE, AlertStatus.PAUSED]),
        )
        stmt = (
            select(Book).outerjoin(BookMarketCache, BookMarketCache.book_id == Book.id)
            .where(Book.id.in_(watched)).options(selectinload(Book.market_cache))
            .order_by(BookMarketCache.last_attempt_at.asc().nulls_first(), Book.id)
        )
        result = await self.session.execute(stmt)
        books = list(result.scalars().all())
        await self._refresh_annual(books)
        return books

    async def get_books_needing_price_refresh(self, limit: int = 8) -> list[Book]:
        """Charge un lot prioritaire de livres BD dont le cache est expiré, absent ou ancien.
        Évite tout pic de charge en étalant les rafraîchissements en tâche de fond.
        """
        now = datetime.now(UTC)
        rest_cutoff = now - timedelta(hours=FAILED_COLLECTION_REST_HOURS)
        # ``IS NULL`` est explicite : un NOT sur une comparaison fausse en SQL
        # vaut NULL, et la clause WHERE écarterait alors les livres sans cache.
        not_in_rest = or_(
            BookMarketCache.collection_status.is_(None),
            BookMarketCache.collection_status.not_in(["failed", "partial"]),
            BookMarketCache.last_attempt_at.is_(None),
            BookMarketCache.last_attempt_at <= rest_cutoff,
        )
        watched = select(AlertBook.book_id).join(Alert).join(Category).where(
            Category.slug == "bd_comics", Alert.deleted_at.is_(None),
            Alert.status.in_([AlertStatus.ACTIVE, AlertStatus.PAUSED]),
        )
        population = await self.session.scalar(
            select(func.count()).select_from(Book).where(Book.id.in_(watched))
        )
        # 12 lots couvrent six heures : même les reprises en échec ont assez de places.
        limit = max(limit, ceil((population or 0) / 12))
        successful = BookMarketCache.last_successful_collection_at
        subq = (
            select(Book.id)
            .join(AlertBook, Book.id == AlertBook.book_id)
            .join(Alert, AlertBook.alert_id == Alert.id)
            .join(Category, Alert.category_id == Category.id)
            .outerjoin(BookMarketCache, Book.id == BookMarketCache.book_id)
            .where(
                Category.slug == "bd_comics",
                Alert.status.in_([AlertStatus.ACTIVE, AlertStatus.PAUSED]),
                Alert.deleted_at.is_(None),
                or_(successful.is_(None), successful <= now - timedelta(hours=24),
                    BookMarketCache.collection_status.in_(["failed", "partial"])),
                not_in_rest,
            )
            .group_by(Book.id, BookMarketCache.last_attempt_at, BookMarketCache.checked_at)
            .order_by(BookMarketCache.last_attempt_at.asc().nulls_first(),
                      BookMarketCache.checked_at.asc().nulls_first(), Book.id)
            .limit(limit)
        )
        book_ids = list((await self.session.execute(subq)).scalars().all())
        if not book_ids:
            return []
        stmt = select(Book).options(selectinload(Book.market_cache)).where(Book.id.in_(book_ids))
        result = await self.session.execute(stmt)
        books = list(result.scalars().all())
        priority = {book_id: index for index, book_id in enumerate(book_ids)}
        books.sort(key=lambda book: priority[book.id])
        await self._refresh_annual(books)
        return books

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
        """Charge tous les livres associés à une alerte avec leurs caches de marché préchargés.

        ``populate_existing`` force la relecture des objets déjà présents dans
        l'identity map : après une écriture de cote dans la même session, leur
        relation ``market_cache`` serait sinon périmée, et un total de série
        calculé dessus ignorerait la cote qui vient d'être écrite.
        """
        stmt = (
            select(Book)
            .join(AlertBook, Book.id == AlertBook.book_id)
            .options(*self._BOOK_CACHE_LOAD)
            .where(AlertBook.alert_id == alert_id)
            .order_by(Book.title.asc())
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        books = list(result.scalars().all())
        await self._refresh_annual(books)
        return books

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


def _catalogue_tome_numbers(title: str, series_key: str) -> set[int]:
    """Tous les numéros de tome qu'une fiche du catalogue peut porter.

    Deux sources, dans le même ordre que la résolution d'un tome unitaire : le
    sous-titre reconnu par l'extracteur, puis le numéro imprimé dans le titre.
    Ensemble vide pour une fiche sans tome identifiable (intégrale, hors-série) :
    elle ne peut ni composer un lot, ni y'appartenir.
    """
    numbers: set[int] = set()
    extractor_tome = TomeExtractor.extract(title, series_key=series_key).tome_number
    if extractor_tome is not None:
        numbers.add(extractor_tome)
    printed = re.search(
        r"\b(?:tome|t|vol|livre)\s*\.?\s*([0-9]{1,2})\b",
        title,
        re.IGNORECASE,
    )
    if printed:
        numbers.add(int(printed.group(1)))
    return numbers


def _resolve_counted_lot(
    books: list[Book],
    series_key: str,
    announced_count: int,
) -> list[int] | None:
    """Composition d'un lot dénombré (« lot de 4 tomes ») : la série, ou rien.

    Le vendeur donne un nombre, pas des numéros : c'est le catalogue qui apporte
    les tomes, et c'est donc nous qui déduisons la composition. On ne s'y risque
    que si ce nombre ne laisse qu'un seul jeu possible, c'est-à-dire si la série
    compte exactement autant de tomes identifiables, sans doublon.

    Dans le cas contraire on renvoie ``None`` et l'appelant garde son message
    honnête : composer un lot à partir d'un compte ferait passer une hypothèse
    pour un inventaire, et une cote fausse fait perdre de l'argent (ADR-016).
    """
    per_number: dict[int, int] = {}
    for book in books:
        for number in _catalogue_tome_numbers(book.title, series_key):
            per_number[number] = per_number.get(number, 0) + 1
    # Un tome porté par deux fiches (éditions concurrentes) ne dit pas lequel le
    # vendeur a mis dans le lot : sans ce refus, la cote cumulerait le même tome.
    if any(count > 1 for count in per_number.values()):
        return None
    if len(per_number) != announced_count:
        return None
    return sorted(per_number)


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
        # « Intégrale » peut désigner une édition en un volume. Seule une fiche
        # explicitement intégrale, sans indices de plusieurs livres, le prouve.
        normalized_listing = normalize_text(f"{listing_title} {listing_description or ''}")
        explicit_bundle = any(
            re.search(rf"\b{re.escape(keyword)}\b", normalized_listing)
            for keyword in TomeExtractor.LOT_KEYWORDS
            if keyword != "integrale"
        )
        if (
            len(books) == 1
            and not tome_res.lot_tomes
            and tome_res.lot_count is None
            and not explicit_bundle
            and re.search(r"\bintegrale\b", normalize_text(books[0].title))
        ):
            return books[0], books, replace(tome_res, is_lot=False)
        if tome_res.lot_tomes:
            wanted = set(tome_res.lot_tomes)
            matching_lot = [
                b for b in books if _catalogue_tome_numbers(b.title, alert_name) & wanted
            ]
            return None, matching_lot if matching_lot else books, tome_res
        if tome_res.lot_count:
            composed = _resolve_counted_lot(books, alert_name, tome_res.lot_count)
            if composed is not None:
                return (
                    None,
                    books,
                    replace(
                        tome_res,
                        lot_tomes=composed,
                        composition_inferred=True,
                    ),
                )
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

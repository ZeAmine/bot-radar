"""Confirmation différée d'une déclaration ISBN, avant toute liaison de référence."""

import logging
import uuid
from typing import Any

from app.core.constants import AlertStatus, BookSearchStatus
from app.db.repositories.alert_repo import AlertRepository, BibliographicSnapshot
from app.db.repositories.book_repo import BookRepository, resolve_loaded_books
from app.db.session import AsyncSessionLocal
from app.domain.entities.alert_preparation import preparation_request
from app.domain.rules.bibliographic_identity import series_association
from app.services.book_market_pricing import recalculate_invalidated_series
from app.services.book_metadata_service import (
    TITLE_SIMILARITY_MIN,
    normalize_edition_language,
    same_author,
    title_similarity,
)
from app.services.tome_extractor import TomeExtractor
from app.use_cases.book_search import BookSearchUseCase
from app.utils.isbn import normalize_listing_isbn
from app.utils.text import normalize_text

logger = logging.getLogger("radar.use_cases.ingest_listing")


async def confirm_listing_book(
    *,
    alert_id: uuid.UUID,
    alert_name: str,
    alert_isbn: str | None,
    expected_preparation: dict[str, Any] | None,
    listing_isbn: str,
    listing_title: str,
    listing_description: str | None,
    expected_snapshot: BibliographicSnapshot | None = None,
) -> None:
    """Consulte sans verrou SQL, puis lie seulement sur l'identité encore courante."""
    async with AsyncSessionLocal() as session:
        current = await AlertRepository(session).get_by_id(alert_id)
        if current is None or (current.name, current.isbn) != (alert_name, alert_isbn):
            return
        snapshot = expected_snapshot or BibliographicSnapshot.capture(current)
        if not snapshot.matches(current):
            return
    result = await BookSearchUseCase().search_by_isbn(listing_isbn, expected_title=listing_title)
    confirmed, reference = result.confirmed, result.reference
    if result.status != BookSearchStatus.FOUND or confirmed is None or reference is None:
        logger.info(
            "ISBN vendeur non confirmé : %s",
            result.status.value,
            extra={"event": "listing_isbn_unconfirmed", "status": result.status.value},
        )
        return
    if (
        normalize_listing_isbn(reference.isbn13) != listing_isbn
        or title_similarity(confirmed.title, alert_name) < TITLE_SIMILARITY_MIN
        or (alert_isbn is not None and normalize_listing_isbn(alert_isbn) != listing_isbn)
    ):
        logger.info(
            "ISBN vendeur incompatible avec l'alerte.", extra={"event": "listing_isbn_conflict"}
        )
        return

    requested_tome = TomeExtractor.extract(alert_name).tome_number
    notice_tome = TomeExtractor.extract(confirmed.title, series_key=alert_name).tome_number
    if requested_tome is not None and notice_tome != requested_tome:
        logger.info(
            "ISBN vendeur d'un autre tome ; liaison abandonnée.",
            extra={"event": "listing_isbn_conflict"},
        )
        return

    async with AsyncSessionLocal() as session:
        repo = AlertRepository(session)
        current = await repo.lock_bibliographic_result(alert_id, snapshot)
        if current is None:
            return
        if (
            current.status != AlertStatus.ACTIVE
            or current.name != alert_name
            or current.isbn != alert_isbn
            or current.preparation != expected_preparation
        ):
            return
        request = preparation_request(current)
        if (
            request
            and request.author_hint
            and (not confirmed.author or not same_author(request.author_hint, confirmed.author))
        ):
            return
        author_hint = request.author_hint if request else None
        if not author_hint and confirmed.author and any(
            normalize_text(term) == normalize_text(confirmed.author)
            for term in (current.include_keywords or [])
        ):
            author_hint = confirmed.author
        identity = series_association(
            requested_title=current.name, title=confirmed.title,
            author=confirmed.author,
            author_hint=confirmed.author if current.isbn else author_hint,
            language=confirmed.language, number=confirmed.volume_number,
        )
        if identity.outcome != "accept":
            logger.info("Association vendeur non retenue", extra={
                "event": "listing_isbn_identity", "outcome": identity.outcome,
                "reason_code": identity.reason,
            })
            return
        # Objet transitoire pour les règles de tome/lot existantes, sans SQL.
        from app.db.models.book import Book

        candidate = Book(title=confirmed.title, isbn13=listing_isbn)
        selected, _books, _tome = resolve_loaded_books(
            alert_name=current.name,
            listing_title=listing_title,
            listing_description=listing_description,
            books=[candidate],
        )
        if selected is None:
            return
        book_repo = BookRepository(session)
        book = await book_repo.get_by_isbn13(listing_isbn)
        if book is None:
            book = await book_repo.ensure_confirmed_book(
                title=confirmed.title,
                isbn13=listing_isbn,
                author=confirmed.author,
                publisher=confirmed.publisher,
                publication_year=confirmed.publication_year,
                format_book=reference.format_detail,
            )
        stored_tome = TomeExtractor.extract(book.title, series_key=alert_name).tome_number
        if (
            normalize_edition_language(book.language) != "fr"
            or (stored_tome is not None and notice_tome is not None and stored_tome != notice_tome)
            or title_similarity(book.title, confirmed.title) < TITLE_SIMILARITY_MIN
            or (book.author and confirmed.author and not same_author(book.author, confirmed.author))
        ):
            logger.warning(
                "Fiche existante incompatible avec la notice ; liaison abandonnée.",
                extra={"event": "listing_isbn_catalog_conflict"},
            )
            return
        selected, _books, _tome = resolve_loaded_books(
            alert_name=current.name,
            listing_title=listing_title,
            listing_description=listing_description,
            books=[book],
        )
        if selected is None:
            logger.warning(
                "Fiche existante incohérente avec le tome ou lot annoncé.",
                extra={"event": "listing_isbn_catalog_conflict"},
            )
            return
        await book_repo.link_alert_to_book(alert_id=current.id, book_id=book.id)
        await repo.set_book_search_exhausted(current.id, None)
        await recalculate_invalidated_series(session)
        await session.commit()

"""Tests unitaires pour le service de catalogue BD et identification de livres."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.core.constants import MatchConfidence
from app.db.models.book import Book
from app.services.book_catalog_service import (
    BookCatalogService,
    isbn10_to_isbn13,
)


def test_isbn10_to_isbn13_conversion():
    assert isbn10_to_isbn13("220507805X") == "9782205078053"
    assert isbn10_to_isbn13("2-205-07805-X") == "9782205078053"
    assert isbn10_to_isbn13("123") is None


@pytest.mark.asyncio
async def test_identify_book_by_isbn13():
    session = AsyncMock()
    service = BookCatalogService(session)

    mock_book = Book(
        id=uuid.uuid4(), title="Blacksad", isbn13="9782205078054", normalized_title="blacksad"
    )
    service.book_repo.get_by_isbn13 = AsyncMock(return_value=mock_book)

    book, confidence = await service.identify_book(
        isbn13="9782205078054",
        title="Blacksad",
    )
    assert book == mock_book
    assert confidence == MatchConfidence.HIGH


@pytest.mark.asyncio
async def test_identify_book_by_isbn10():
    session = AsyncMock()
    service = BookCatalogService(session)

    mock_book = Book(
        id=uuid.uuid4(), title="Blacksad", isbn13="9782205078054", normalized_title="blacksad"
    )
    service.book_repo.get_by_isbn13 = AsyncMock(return_value=mock_book)

    book, confidence = await service.identify_book(
        isbn10="220507805X",
        title="Blacksad",
    )
    assert book == mock_book
    assert confidence == MatchConfidence.HIGH


@pytest.mark.asyncio
async def test_identify_book_by_title_and_author():
    session = AsyncMock()
    service = BookCatalogService(session)

    mock_book = Book(
        id=uuid.uuid4(),
        title="Blacksad",
        normalized_title="blacksad",
        author="Juanjo Guarnido",
    )
    service.book_repo.get_by_isbn13 = AsyncMock(return_value=None)
    service.book_repo.get_by_normalized_title = AsyncMock(return_value=[mock_book])

    book, confidence = await service.identify_book(
        title="Blacksad",
        author="Juanjo Guarnido",
    )
    assert book == mock_book
    assert confidence == MatchConfidence.MEDIUM


@pytest.mark.asyncio
async def test_identify_book_title_only_low_confidence():
    session = AsyncMock()
    service = BookCatalogService(session)

    mock_book = Book(id=uuid.uuid4(), title="Blacksad", normalized_title="blacksad")
    service.book_repo.get_by_isbn13 = AsyncMock(return_value=None)
    service.book_repo.get_by_normalized_title = AsyncMock(return_value=[mock_book])

    book, confidence = await service.identify_book(title="Blacksad")
    assert book == mock_book
    assert confidence == MatchConfidence.LOW


@pytest.mark.asyncio
async def test_identify_book_not_found():
    session = AsyncMock()
    service = BookCatalogService(session)

    service.book_repo.get_by_isbn13 = AsyncMock(return_value=None)
    service.book_repo.get_by_normalized_title = AsyncMock(return_value=[])

    book, confidence = await service.identify_book(title="Titre Totalement Inconnu")
    assert book is None
    assert confidence is None

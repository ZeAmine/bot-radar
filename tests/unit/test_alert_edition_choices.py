"""Tests de la remontée des éditions concurrentes dans la saisie manuelle /add."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message


@pytest.mark.asyncio
async def test_process_bd_query_lookup_stores_competing_editions():
    """La saisie manuelle remonte aussi les éditions concurrentes de l'œuvre."""
    from app.bot.handlers.alerts_helpers import process_bd_query_lookup
    from app.services.book_metadata_service import BookMetadata, BookResolution

    main_meta = BookMetadata(
        title="La couleur des choses",
        author="Martin Panchaud",
        publisher="Çà et là",
        isbn13="9782369903086",
        publication_year=2022,
        recommended_includes=["la couleur des choses"],
        recommended_excludes=[],
        recommended_variants=[],
    )
    other_edition = BookMetadata(
        title="La couleur des choses",
        author="Martin Panchaud",
        publisher="Futuropolis",
        isbn13="9782369904151",
        publication_year=2018,
    )

    class _Service:
        async def lookup(self, _query):
            return main_meta

        async def resolve_for_alert(self, _title, _author_hint=None):
            return BookResolution(best=main_meta, alternatives=[other_edition])

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()
    message = MagicMock(spec=Message)
    message.answer = AsyncMock(return_value=MagicMock(spec=Message))
    message.answer.return_value.edit_text = AsyncMock()

    category_repo = MagicMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    with (
        patch(
            "app.bot.handlers.alerts_helpers.BookMetadataService", return_value=_Service()
        ),
        patch(
            "app.bot.handlers.alerts.CategoryRepository",
            return_value=category_repo,
        ),
    ):
        await process_bd_query_lookup(message, state, "la couleur des choses", AsyncMock())

    stored = state.update_data.call_args.kwargs
    assert [edition["isbn13"] for edition in stored["edition_alternatives"]] == [
        "9782369903086",
        "9782369904151",
    ]

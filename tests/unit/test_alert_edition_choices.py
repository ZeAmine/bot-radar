"""Tests de la remontée des éditions concurrentes dans la saisie manuelle /add."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

from app.domain.enums import BookSearchStatus
from app.use_cases.book_search import BookSearchResult, BookWorkChoice


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

    resolution = BookResolution(best=main_meta, alternatives=[other_edition])

    from tests.add_fakes import fake_book_search

    class _Service:
        async def resolve_for_alert(self, _title, _author_hint=None):
            return resolution

    state = AsyncMock()
    persisted: dict = {}

    async def _get() -> dict:
        return dict(persisted)

    async def _update(payload=None, **kwargs):
        persisted.update(payload or {})
        persisted.update(kwargs)

    state.get_data = AsyncMock(side_effect=_get)
    state.update_data = AsyncMock(side_effect=_update)
    state.set_state = AsyncMock()
    message = MagicMock(spec=Message)
    message.answer = AsyncMock(return_value=MagicMock(spec=Message))
    message.answer.return_value.edit_text = AsyncMock()

    category_repo = MagicMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    with (
        patch(
            "app.bot.handlers.alerts_helpers.BookSearchUseCase",
            fake_book_search(_Service()),
        ),
        patch(
            "app.bot.handlers.alerts.CategoryRepository",
            return_value=category_repo,
        ),
    ):
        await process_bd_query_lookup(message, state, "la couleur des choses", AsyncMock())

    stored = dict(persisted)
    assert [edition["isbn13"] for edition in stored["edition_alternatives"]] == [
        "9782369903086",
        "9782369904151",
    ]


@pytest.mark.asyncio
async def test_process_bd_query_lookup_refuses_to_attach_a_homonym():
    """La saisie manuelle applique la même règle que le `/add` en langage naturel.

    Saisir « Les Enfants perdus » ne doit pas produire une fiche quieter sur le roman de
    1989 : aucune donnée n'est rattachée, et les deux œuvres sont proposées au choix.
    """
    from app.bot.handlers.alerts_helpers import process_bd_query_lookup
    ambiguous = BookSearchResult(
        status=BookSearchStatus.AMBIGUOUS,
        works=(
            BookWorkChoice(
                title="Les Enfants perdus",
                author="Rosemonde Ahou de Saintange",
                isbn13="9782214080627",
                provenance="bnf",
            ),
            BookWorkChoice(
                title="Les Enfants perdus",
                author="Nef",
                isbn13="9782810206087",
                provenance="bnf",
            ),
        ),
        note="⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre.",
    )

    class _UseCase:
        async def search_by_title(self, _title, _author=None):
            return ambiguous

    stored: dict = {}

    async def _update_data(**kwargs):
        stored.update(kwargs)

    state = AsyncMock()
    state.get_data = AsyncMock(side_effect=lambda: dict(stored))
    state.update_data = _update_data
    state.set_state = AsyncMock()
    message = MagicMock(spec=Message)
    message.answer = AsyncMock(return_value=MagicMock(spec=Message))
    message.answer.return_value.edit_text = AsyncMock()

    category_repo = MagicMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    with (
        patch(
            "app.bot.handlers.alerts_helpers.BookSearchUseCase", return_value=_UseCase()
        ),
        patch(
            "app.bot.handlers.alerts.CategoryRepository",
            return_value=category_repo,
        ),
    ):
        await process_bd_query_lookup(message, state, "Les Enfants perdus", AsyncMock())
    assert stored["isbn"] is None
    assert stored["author"] is None
    assert stored["publisher"] is None
    # Les mots-clés et les exclusions de l'homonyme ne sont pas repris non plus.
    assert stored["variants"] == []
    assert stored["exclude_keywords"] == []
    assert stored["edition_alternatives"] == []
    assert "⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre." in stored["isbn_note"]
    assert [work["author"] for work in stored["work_choices"]] == [
        "Rosemonde Ahou de Saintange",
        "Nef",
    ]
    # Les mots-clés d'inclusion dérivent du titre demandé, sans article redondant :
    # une seule inclusion, qui accepte les deux formulations (« La couleur… », « Couleur… »).
    assert stored["include_keywords"] == ["enfants perdus"]


@pytest.mark.asyncio
async def test_process_bd_query_lookup_opens_the_card_when_nothing_is_known():
    """Aucune source ne connaît le titre : la carte s'ouvre sur l'avertissement.

    Le cul-de-sac « ❌ Aucune œuvre trouvée » obligeait à deviner un titre que les
    catalogues connaissent — c'est-à-dire à renoncer — alors qu'un livre sorti la
    semaine dernière n'est dans aucun d'eux. L'alerte reste créable sur le titre saisi,
    et la carte dit ce qui manque avant de le montrer.
    """
    from app.bot.handlers.alerts_helpers import (
        UNIDENTIFIED_WORK_NOTE,
        process_bd_query_lookup,
    )


    not_found = BookSearchResult(status=BookSearchStatus.NOT_FOUND)

    class _UseCase:
        async def search_by_title(self, _title, _author=None):
            return not_found

    stored: dict = {}

    async def _update_data(**kwargs):
        stored.update(kwargs)

    async def _get_data():
        return dict(stored)

    state = AsyncMock()
    state.update_data = _update_data
    state.get_data = _get_data
    state.set_state = AsyncMock()
    message = MagicMock(spec=Message)
    status = MagicMock(spec=Message)
    status.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status)

    category_repo = MagicMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    with (
        patch(
            "app.bot.handlers.alerts_helpers.BookSearchUseCase", return_value=_UseCase()
        ),
        patch(
            "app.bot.handlers.alerts.CategoryRepository",
            return_value=category_repo,
        ),
    ):
        await process_bd_query_lookup(message, state, "whoogys dailycieux", AsyncMock())

    assert stored["book_unidentified"] is True
    assert stored["name"] == "whoogys dailycieux"
    assert stored["isbn"] is None
    assert stored["author"] is None
    assert stored["category_slug"] == "bd_comics"
    assert stored["include_keywords"] == ["whoogys dailycieux"]

    # L'écran ouvert est celui du prix, et il porte l'avertissement en tête.
    rendered = status.edit_text.call_args[0][0]
    assert f"<i>{UNIDENTIFIED_WORK_NOTE}</i>" in rendered
    assert "<b>ISBN :</b> introuvable" in rendered
    assert "<b>Auteur :</b> Non renseigné" in rendered
    assert "<b>Éditeur :</b> Non renseigné" in rendered
    assert "<b>Titre :</b> whoogys dailycieux" in rendered

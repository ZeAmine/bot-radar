"""Schémas de réponse des endpoints bibliographiques internes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.domain.enums import BookSearchStatus, ProviderQueryStatus

if TYPE_CHECKING:  # pragma: no cover - import statique seul (cycle providers)
    from app.use_cases.book_search import (
        BookEditionChoice,
        BookSearchResult,
        BookWorkChoice,
    )


class BookWorkDTO(BaseModel):
    """Une œuvre candidate, champs inconnus à `None`, sans rôle ni langue inventés."""

    title: str
    isbn13: str | None = None
    author: str | None = None
    publisher: str | None = None
    publication_year: int | None = None
    provenance: str | None = None


class BookEditionDTO(BaseModel):
    """Une édition d'ISBN donné, jamais fusionnée avec une autre."""

    isbn13: str
    publisher: str | None = None
    publication_year: int | None = None
    format_detail: str | None = None
    provenance: str | None = None


class ProviderAttemptDTO(BaseModel):
    """Une tentative réellement partie : statut et code contrôlé, sans secret."""

    provider: str
    search_type: str
    status: ProviderQueryStatus
    has_results: bool
    error_code: str | None = None


class BookSearchResponseDTO(BaseModel):
    """Liste blanche du contrat `BookSearchResult` : rien d'autre ne sort."""

    status: BookSearchStatus
    works: list[BookWorkDTO] = []
    editions: list[BookEditionDTO] = []
    reference: BookEditionDTO | None = None
    consulted: list[str] | None = None
    unavailable: list[str] = []
    provenance: str | None = None
    conflict: BookWorkDTO | None = None
    note: str | None = None
    attempts: list[ProviderAttemptDTO] = []


def _work_dto(choice: BookWorkChoice) -> BookWorkDTO:
    return BookWorkDTO(
        title=choice.title,
        isbn13=choice.isbn13,
        author=choice.author,
        publisher=choice.publisher,
        publication_year=choice.publication_year,
        provenance=choice.provenance,
    )


def _edition_dto(choice: BookEditionChoice) -> BookEditionDTO:
    return BookEditionDTO(
        isbn13=choice.isbn13,
        publisher=choice.publisher,
        publication_year=choice.publication_year,
        format_detail=choice.format_detail,
        provenance=choice.provenance,
    )


def book_search_response(result: BookSearchResult) -> BookSearchResponseDTO:
    """Sérialise le contrat en liste blanche : aucun ORM, client, brut ou secret."""
    return BookSearchResponseDTO(
        status=result.status,
        works=[_work_dto(work) for work in result.works],
        editions=[_edition_dto(edition) for edition in result.editions],
        reference=_edition_dto(result.reference) if result.reference is not None else None,
        consulted=list(result.consulted) if result.consulted is not None else None,
        unavailable=list(result.unavailable),
        provenance=result.provenance,
        conflict=_work_dto(result.conflict) if result.conflict is not None else None,
        note=result.note,
        attempts=[
            ProviderAttemptDTO(
                provider=attempt.provider,
                search_type=attempt.search_type,
                status=attempt.status,
                has_results=attempt.has_results,
                error_code=attempt.error_code,
            )
            for attempt in result.attempts
        ],
    )

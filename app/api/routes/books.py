"""Endpoints bibliographiques internes : le cas d'usage partagé exposé en HTTP.

Aucune logique de résolution ici : les routes valident l'entrée, appellent
`BookSearchUseCase` (rapports et statuts des Lots 0 à 2) et sérialisent en
liste blanche (`app/api/schemas/books.py`). Lecture seule : ni `Book`, ni
`Alert`, ni `AlertBook`, ni LLM, ni cache.
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status

from app.api.dependencies import require_internal_token
from app.api.schemas.books import BookSearchResponseDTO, book_search_response
from app.domain.enums import BookSearchStatus
from app.use_cases.book_search import BookSearchResult, BookSearchUseCase, InvalidIsbnError

router = APIRouter(
    prefix="/api/v1/books",
    tags=["Books"],
    dependencies=[Depends(require_internal_token)],
)

TITLE_MAX_LENGTH = 200
AUTHOR_MAX_LENGTH = 120


def _respond(
    result: BookSearchResult, response: Response
) -> BookSearchResponseDTO:
    """Corps whitelisté ; `unavailable` vaut 503, sans `Retry-After` inventé."""
    if result.status == BookSearchStatus.UNAVAILABLE:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return book_search_response(result)


@router.get("/search", response_model=BookSearchResponseDTO)
async def search_books(
    response: Response,
    title: str = Query(min_length=1, max_length=TITLE_MAX_LENGTH),
    author: str | None = Query(default=None, max_length=AUTHOR_MAX_LENGTH),
) -> BookSearchResponseDTO:
    """Recherche par titre (+ auteur facultatif) dans les autorités configurées."""
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Le titre ne peut pas être vide.",
        )
    clean_author: str | None = None
    if author is not None:
        clean_author = author.strip()
        if not clean_author:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="L'auteur, s'il est fourni, ne peut pas être vide.",
            )

    result = await BookSearchUseCase().search_by_title(clean_title, clean_author)
    return _respond(result, response)


@router.get("/isbn/{isbn}", response_model=BookSearchResponseDTO)
async def get_book_by_isbn(
    response: Response, isbn: str = Path(min_length=1)
) -> BookSearchResponseDTO:
    """Recherche d'une édition par ISBN-10 ou ISBN-13 validé et normalisé."""
    try:
        result = await BookSearchUseCase().search_by_isbn(isbn.strip())
    except InvalidIsbnError as exc:
        # Seule l'erreur de validation précise vaut 422 : toute autre
        # `ValueError` remonte en erreur interne générique, sans détails.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _respond(result, response)

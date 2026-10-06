"""Endpoints bibliographiques internes (Lot 3) : auth, validation, mapping HTTP.

Le cas d'usage est moqué : aucun appel réseau, aucune base, aucun LLM.
Seul le test ISBN-invalide exerce le vrai cas d'usage, qui rejette avant
tout appel fournisseur (méthodes du service verrouillées en échec).
"""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import settings
from app.domain.enums import ProviderQueryStatus
from app.main import app
from app.services.book_metadata_service import (
    AUTORITE_BNF,
    AUTORITE_OPENLIBRARY,
    BibliographicReport,
    BookMetadata,
    BookResolution,
    IsbnVerdict,
    ProviderAttempt,
)
from app.use_cases.book_search import (
    BookSearchUseCase,
    result_from_isbn_verdict,
    result_from_resolution,
    result_from_verdict_report,
)


@pytest.fixture
async def books_client() -> AsyncGenerator[AsyncClient, None]:
    """Client authentifié, sans base : les routes livres n'ouvrent pas de session."""
    token = settings.INTERNAL_API_TOKEN.get_secret_value()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@pytest.fixture
async def raw_client() -> AsyncGenerator[AsyncClient, None]:
    """Client laissant passer les 500 pour vérifier la réponse générique."""
    token = settings.INTERNAL_API_TOKEN.get_secret_value()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


def _use_case_moque(
    monkeypatch: pytest.MonkeyPatch, resultat: Any = None, erreur: Exception | None = None
) -> AsyncMock:
    """Le cas d'usage devient un mock : la route ne voit que le contrat."""
    faux = AsyncMock(spec=BookSearchUseCase)
    if erreur is not None:
        faux.search_by_title.side_effect = erreur
        faux.search_by_isbn.side_effect = erreur
    else:
        faux.search_by_title.return_value = resultat
        faux.search_by_isbn.return_value = resultat
    monkeypatch.setattr("app.api.routes.books.BookSearchUseCase", lambda: faux)
    return faux


def _notice(isbn: str = "9780306406157", **champs: Any) -> BookMetadata:
    fields: dict[str, Any] = {
        "title": "Blacksad",
        "author": "Canales",
        "isbn13": isbn,
        "source": AUTORITE_BNF,
    }
    fields.update(champs)
    return BookMetadata(**fields)


def _verdict_confirme() -> IsbnVerdict:
    return IsbnVerdict(
        outcome="confirme", notice=_notice(), consulted=(AUTORITE_BNF,)
    )


@pytest.mark.asyncio
async def test_sans_auth_le_cas_d_usage_n_est_pas_appele(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ni token ni traitement : 401/403 avant toute recherche."""
    faux = _use_case_moque(monkeypatch, resultat=None)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        sans = await client.get("/api/v1/books/search", params={"title": "Blacksad"})
        mauvais = await client.get(
            "/api/v1/books/search",
            params={"title": "Blacksad"},
            headers={"Authorization": "Bearer mauvais-jeton"},
        )
        isbn = await client.get("/api/v1/books/isbn/9780306406157")

    assert sans.status_code in (401, 403)
    assert mauvais.status_code == 401
    assert isbn.status_code in (401, 403)
    faux.search_by_title.assert_not_awaited()
    faux.search_by_isbn.assert_not_awaited()


@pytest.mark.asyncio
async def test_titre_vide_ou_trop_long_vaut_422(
    books_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation d'entrée avant tout appel au cas d'usage."""
    faux = _use_case_moque(monkeypatch, resultat=None)

    assert (await books_client.get("/api/v1/books/search")).status_code == 422
    assert (
        await books_client.get("/api/v1/books/search", params={"title": "   "})
    ).status_code == 422
    assert (
        await books_client.get(
            "/api/v1/books/search", params={"title": "x" * 201}
        )
    ).status_code == 422
    assert (
        await books_client.get(
            "/api/v1/books/search", params={"title": "Blacksad", "author": "   "}
        )
    ).status_code == 422
    assert (
        await books_client.get(
            "/api/v1/books/search", params={"title": "Blacksad", "author": "y" * 121}
        )
    ).status_code == 422

    faux.search_by_title.assert_not_awaited()


@pytest.mark.asyncio
async def test_isbn_invalide_vaut_422_sans_appel_fournisseur(
    books_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le vrai cas d'usage rejette avant le réseau : le service reste muet."""
    async def _interdit(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("aucun appel fournisseur sur ISBN invalide")

    monkeypatch.setattr(
        "app.services.book_metadata_service.BookMetadataService.consult_authorities_report",
        _interdit,
    )
    monkeypatch.setattr(
        "app.services.book_metadata_service.BookMetadataService.resolve_for_alert_report",
        _interdit,
    )

    res = await books_client.get("/api/v1/books/isbn/pas-un-isbn")

    assert res.status_code == 422


@pytest.mark.asyncio
async def test_chaque_statut_a_son_mapping_http(
    books_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """found/ambiguous/partial/not_found → 200 ; unavailable → 503 structuré."""
    cas = {
        "found": result_from_isbn_verdict(_verdict_confirme()),
        "ambiguous": result_from_resolution(
            BookResolution(best=_notice("9780306406157"), other_works=[_notice("9780131103627")])
        ),
        "partial": result_from_isbn_verdict(
            IsbnVerdict(
                outcome="silence",
                notice=_notice(source=AUTORITE_OPENLIBRARY),
                consulted=(AUTORITE_BNF, AUTORITE_OPENLIBRARY),
                panne="TimeoutError: délai dépassé",
            )
        ),
        "not_found": result_from_isbn_verdict(
            IsbnVerdict(outcome="inconnu", consulted=(AUTORITE_BNF,))
        ),
        "unavailable": result_from_isbn_verdict(
            IsbnVerdict(
                outcome="silence",
                consulted=(AUTORITE_BNF,),
                panne="TimeoutError: délai dépassé",
            )
        ),
    }
    for statut_attendu, resultat in cas.items():
        _use_case_moque(monkeypatch, resultat=resultat)
        res = await books_client.get("/api/v1/books/search", params={"title": "T"})

        attendu = 503 if statut_attendu == "unavailable" else 200
        assert res.status_code == attendu, statut_attendu
        assert res.json()["status"] == statut_attendu
        assert "Retry-After" not in res.headers


@pytest.mark.asyncio
async def test_ambiguous_et_partial_sans_reference_ni_contradiction(
    books_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ni identité confirmée ni conflit : des candidats, pas une fiche."""
    ambigu = result_from_resolution(
        BookResolution(best=_notice("9780306406157"), other_works=[_notice("9780131103627")])
    )
    _use_case_moque(monkeypatch, resultat=ambigu)
    res = await books_client.get("/api/v1/books/search", params={"title": "T"})
    corps = res.json()

    assert res.status_code == 200
    assert corps["reference"] is None
    assert len(corps["works"]) == 2
    assert corps["editions"] == []
    assert corps["conflict"] is None


@pytest.mark.asyncio
async def test_contradiction_conservee_avec_conflit(
    books_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'ouvrage réel reste en `conflict`, jamais en `reference`."""
    verdict = IsbnVerdict(
        outcome="contradiction",
        notice=_notice(isbn="9780131103627", title="Autre livre"),
        consulted=(AUTORITE_BNF,),
    )
    _use_case_moque(
        monkeypatch, resultat=result_from_isbn_verdict(verdict, "Livre voulu")
    )
    res = await books_client.get("/api/v1/books/isbn/9780131103627")
    corps = res.json()

    assert res.status_code == 200
    assert corps["status"] == "not_found"
    assert corps["reference"] is None
    assert corps["conflict"]["isbn13"] == "9780131103627"


@pytest.mark.asyncio
async def test_reponse_en_liste_blanche_avec_tentatives(
    books_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seuls les champs du contrat sortent, tentatives et codes contrôlés inclus."""
    rapport = BibliographicReport(
        kind="isbn",
        verdict=_verdict_confirme(),
        attempts=(
            ProviderAttempt(
                provider=AUTORITE_BNF,
                search_type="isbn",
                status=ProviderQueryStatus.SUCCESS,
                has_results=True,
            ),
        ),
    )
    _use_case_moque(monkeypatch, resultat=result_from_verdict_report(rapport))
    res = await books_client.get("/api/v1/books/isbn/9780306406157")
    corps = res.json()

    assert set(corps) == {
        "status", "works", "editions", "reference", "consulted",
        "unavailable", "provenance", "conflict", "note", "attempts",
    }
    assert corps["consulted"] == [AUTORITE_BNF]
    assert corps["attempts"] == [
        {
            "provider": AUTORITE_BNF,
            "search_type": "isbn",
            "status": "success",
            "has_results": True,
            "error_code": None,
        }
    ]
    assert corps["reference"]["isbn13"] == "9780306406157"


@pytest.mark.asyncio
async def test_erreur_interne_generique_sans_details(
    raw_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Une `ValueError` non-ISBN n'est pas masquée en 422 : 500 générique."""
    _use_case_moque(monkeypatch, erreur=ValueError("détail interne sensible"))

    res = await raw_client.get("/api/v1/books/search", params={"title": "Blacksad"})

    assert res.status_code == 500
    assert res.text == "Internal Server Error"
    assert "sensible" not in res.text


@pytest.mark.asyncio
async def test_reponse_sans_cle_ni_brut(
    books_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clé factice configurée : ni elle ni aucune charge brute ne sortent."""
    from app.use_cases.book_search import result_from_isbn_verdict

    monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr("CLE-FACTICE-LOT3"))
    _use_case_moque(
        monkeypatch,
        resultat=result_from_isbn_verdict(
            IsbnVerdict(
                outcome="silence",
                consulted=(AUTORITE_BNF,),
                panne="TimeoutError: délai dépassé",
            )
        ),
    )
    res = await books_client.get("/api/v1/books/isbn/9780306406157")

    assert res.status_code == 503
    assert "CLE-FACTICE-LOT3" not in res.text
    assert "key=" not in res.text
    assert "industryIdentifiers" not in res.text

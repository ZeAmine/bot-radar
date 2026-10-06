"""Repli libre Google Books (Lot 1) : déclenchement, filtrage, non-régression.

Le repli libre n'est tenté qu'après une recherche ciblée réussie mais vide ;
ses candidats passent les filtres métier existants avant toute retenue, et ne
sont jamais auto-confirmés. Fixtures et mocks uniquement : aucun réseau,
aucune base, aucun LLM.
"""

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.providers.google_books import ProviderQueryStatus
from app.services.book_metadata_service import (
    AUTORITE_BNF,
    AUTORITE_GOOGLE_BOOKS,
    AUTORITE_OPENLIBRARY,
    BookMetadata,
    BookMetadataService,
)

ISBN_VALIDE_A = "9780306406157"
ISBN_VALIDE_B = "9780131103627"


@pytest.fixture(autouse=True)
def _cle_google_books_par_defaut(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le `.env` du poste ne doit pas changer les verdicts attendus ici."""
    monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr(""))


def _volume(
    titre: str,
    auteurs: list[str] | None = None,
    isbn: str | None = None,
) -> dict[str, object]:
    info: dict[str, object] = {"title": titre}
    if auteurs is not None:
        info["authors"] = auteurs
    if isbn is not None:
        info["industryIdentifiers"] = [{"type": "ISBN_13", "identifier": isbn}]
    return {"volumeInfo": info}


class _ReponseJson:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, charge: object) -> None:
        self._charge = charge

    def json(self) -> object:
        return self._charge


class _ReponseXmlVide(_ReponseJson):
    def __init__(self) -> None:
        super().__init__({})
        self.content = b'<?xml version="1.0"?><root/>'


class _HttpSimule:
    """BNF vide + OpenLibrary vide + Google scripté par type de requête."""

    def __init__(
        self,
        cible: object = None,
        libre: object = None,
        erreur_cible: Exception | None = None,
        statut_cible: int = 200,
    ) -> None:
        self.cible = cible
        self.libre = libre
        self.erreur_cible = erreur_cible
        self.statut_cible = statut_cible
        self.requetes_google: list[str] = []
        self.parametres: list[dict[str, object]] = []

    async def get(self, url: str, **kwargs: object) -> _ReponseJson:
        params = kwargs.get("params")
        query = params if isinstance(params, dict) else {}
        if "googleapis.com" in url:
            question = str(query.get("q", ""))
            self.requetes_google.append(question)
            self.parametres.append(dict(query))
            if question.startswith("intitle:") or question.startswith("isbn:"):
                if self.erreur_cible is not None:
                    raise self.erreur_cible
                if self.statut_cible != 200:
                    reponse = _ReponseJson({"totalItems": 0})
                    reponse.status_code = self.statut_cible
                    return reponse
                charge = self.cible if self.cible is not None else {"totalItems": 0}
                return _ReponseJson(charge)
            return _ReponseJson(
                self.libre if self.libre is not None else {"totalItems": 0}
            )
        if "openlibrary.org" in url:
            return _ReponseJson({"docs": []})
        return _ReponseXmlVide()


def _service(
    http: _HttpSimule, monkeypatch: pytest.MonkeyPatch, avec_cle: bool = True
) -> BookMetadataService:
    if avec_cle:
        monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr("cle-de-test"))
    return BookMetadataService(http_client=http)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_repli_libre_apres_cible_vide_trouve_un_candidat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ciblée vide + libre pertinente : résolution Google, filtres appliqués."""
    http = _HttpSimule(
        libre={"items": [_volume("Blacksad", ["Juan Díaz Canales"], ISBN_VALIDE_A)]}
    )
    service = _service(http, monkeypatch)

    resolution = await service.resolve_for_alert("Blacksad", "Juan Diaz Canales")

    assert resolution is not None
    assert resolution.best.title == "Blacksad"
    assert resolution.best.isbn13 == ISBN_VALIDE_A
    assert resolution.best.source == AUTORITE_GOOGLE_BOOKS
    assert http.requetes_google == [
        'intitle:"Blacksad" inauthor:"Juan Diaz Canales"',
        "Blacksad Juan Diaz Canales",
    ]
    assert all(params.get("maxResults") == "5" for params in http.parametres)


@pytest.mark.asyncio
async def test_pas_de_repli_quand_la_cible_a_deja_repondu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La ciblée prioritaire suffit : une seule requête Google."""
    http = _HttpSimule(
        cible={"items": [_volume("Blacksad", ["Juan Díaz Canales"], ISBN_VALIDE_A)]},
        libre={"items": [_volume("Autre", ["Quelqu'un"], ISBN_VALIDE_B)]},
    )
    service = _service(http, monkeypatch)

    resolution = await service.resolve_for_alert("Blacksad", "Juan Diaz Canales")

    assert resolution is not None
    assert resolution.best.isbn13 == ISBN_VALIDE_A
    assert len(http.requetes_google) == 1


@pytest.mark.asyncio
async def test_pas_de_repli_sur_panne_cible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout, quota ou refus : pas de repli, `None` comme avant."""
    for erreur, statut in (
        (TimeoutError("délai dépassé"), 200),
        (None, 429),
        (None, 401),
    ):
        http = _HttpSimule(
            libre={"items": [_volume("Blacksad", ["Juan Díaz Canales"], ISBN_VALIDE_A)]},
            erreur_cible=erreur,
            statut_cible=statut,
        )
        service = _service(http, monkeypatch)

        assert await service.resolve_for_alert("Blacksad") is None
        assert len(http.requetes_google) == 1


@pytest.mark.asyncio
async def test_pas_de_repli_sans_cle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans clé, Google n'existe pas : aucune requête, aucune consultation."""
    http = _HttpSimule(
        libre={"items": [_volume("Blacksad", ["Juan Díaz Canales"], ISBN_VALIDE_A)]}
    )
    service = _service(http, monkeypatch, avec_cle=False)

    assert await service.resolve_for_alert("Blacksad") is None
    assert http.requetes_google == []


@pytest.mark.asyncio
async def test_candidat_libre_non_pertinent_rejete_par_les_filtres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le repli fournit des candidats, le métier décide : titre lointain écarté."""
    http = _HttpSimule(
        libre={"items": [_volume("Babar le roi", ["Jean de Brunhoff"], ISBN_VALIDE_A)]}
    )
    service = _service(http, monkeypatch)

    assert await service.resolve_for_alert("Blacksad") is None
    assert len(http.requetes_google) == 2


@pytest.mark.asyncio
async def test_candidat_libre_homonyme_reste_ambigu_jamais_confirme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un homonyme du repli libre ne confirme rien : ambiguïté, pas de fiche."""
    http = _HttpSimule(
        libre={
            "items": [
                _volume("Blacksad", ["Juan Díaz Canales"], ISBN_VALIDE_A),
                _volume("Blacksad", ["Autre Auteur"], ISBN_VALIDE_B),
            ]
        }
    )
    service = _service(http, monkeypatch)

    resolution = await service.resolve_for_alert("Blacksad")

    assert resolution is not None
    assert resolution.is_ambiguous


@pytest.mark.asyncio
async def test_chemin_isbn_sans_repli_libre(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un ISBN seul ne fait pas une requête libre : seule `isbn:` est émise."""
    http = _HttpSimule(
        cible={"items": [_volume("Blacksad", ["Juan Díaz Canales"], ISBN_VALIDE_A)]}
    )
    service = _service(http, monkeypatch)

    verdict = await service.consult_authorities(ISBN_VALIDE_A)

    assert verdict.outcome == "confirme"
    assert verdict.autorite == AUTORITE_GOOGLE_BOOKS
    assert verdict.consulted == (
        AUTORITE_BNF,
        AUTORITE_OPENLIBRARY,
        AUTORITE_GOOGLE_BOOKS,
    )
    assert http.requetes_google == [f"isbn:{ISBN_VALIDE_A}"]


@pytest.mark.asyncio
async def test_panne_google_isbn_laisse_le_verdict_inchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google en panne sur le chemin ISBN : `inconnu`, comme avant le Lot 1."""
    http = _HttpSimule(statut_cible=500)
    service = _service(http, monkeypatch)

    verdict = await service.consult_authorities(ISBN_VALIDE_A)

    assert verdict.outcome == "inconnu"
    assert verdict.consulted == (
        AUTORITE_BNF,
        AUTORITE_OPENLIBRARY,
        AUTORITE_GOOGLE_BOOKS,
    )


@pytest.mark.asyncio
async def test_has_ebook_false_ne_bloque_pas_l_identification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`has_ebook=False` n'est ni un rejet ni une preuve papier : titre/auteur/ISBN décident."""
    http = _HttpSimule(
        libre={
            "items": [
                {
                    "volumeInfo": {
                        "title": "Blacksad",
                        "authors": ["Juan Díaz Canales"],
                        "industryIdentifiers": [
                            {"type": "ISBN_13", "identifier": ISBN_VALIDE_A}
                        ],
                    },
                    "accessInfo": {
                        "epub": {"isAvailable": False},
                        "pdf": {"isAvailable": False},
                    },
                }
            ]
        }
    )
    service = _service(http, monkeypatch)

    resolution = await service.resolve_for_alert("Blacksad", "Juan Diaz Canales")

    assert resolution is not None
    assert resolution.best.isbn13 == ISBN_VALIDE_A
    assert "has_ebook" not in BookMetadata.__dataclass_fields__


def test_statuts_google_exposes_au_service() -> None:
    """Le contrat typé est importable côté orchestration (Lot 2 : propagation)."""
    assert ProviderQueryStatus.SUCCESS.value == "success"
    assert ProviderQueryStatus.DISABLED.value == "disabled"

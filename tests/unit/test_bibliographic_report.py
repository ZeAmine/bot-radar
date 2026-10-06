"""Rapports bibliographiques (Lot 2) : tentatives réelles vers le contrat.

Chaque test verrouille une ligne de la table de décision : statuts dérivés
des tentatives réellement parties, règle T9 (repli pendant panne BNF jamais
confirmé dans le nouveau contrat, retour historique inchangé), brut avant
parsing, rejet ISBN invalide, invariants du rapport, libellés de panne
préservés et absence de fuite de secret. Mocks uniquement.
"""

import json
import logging
from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.domain.enums import BookSearchStatus, ProviderQueryStatus
from app.services.book_metadata_service import (
    AUTORITE_BNF,
    AUTORITE_GOOGLE_BOOKS,
    AUTORITE_OPENLIBRARY,
    BibliographicReport,
    BookMetadataService,
    IsbnVerdict,
)
from app.use_cases.book_search import (
    BookSearchUseCase,
    result_from_report,
    result_from_verdict_report,
)

ISBN_VALIDE = "9780306406157"
ISBN_AUTRE = "9780131103627"
CLE_FACTICE = "CLE-FACTICE-LOT2"


@pytest.fixture(autouse=True)
def _cle_google_absente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr(""))


def _doc_ol(
    titre: str, auteur: str | None = None, isbn: str | None = ISBN_VALIDE
) -> dict[str, Any]:
    doc: dict[str, Any] = {"title": titre, "author_name": [auteur] if auteur else []}
    if isbn is not None:
        doc["isbn"] = [isbn]
    return doc


def _volume_google(
    titre: str, auteurs: list[str] | None = None, isbn: str | None = ISBN_VALIDE
) -> dict[str, Any]:
    info: dict[str, Any] = {"title": titre}
    if auteurs is not None:
        info["authors"] = auteurs
    if isbn is not None:
        info["industryIdentifiers"] = [{"type": "ISBN_13", "identifier": isbn}]
    return {"volumeInfo": info}


_SRU_VIDE = b'<?xml version="1.0"?><root/>'
_SRU_SANS_ISBN = (
    b'<?xml version="1.0"?>'
    b'<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">'
    b"<srw:records><srw:record><srw:recordData>"
    b'<dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">Blacksad</dc:title>'
    b'<dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Canales</dc:creator>'
    b"</srw:recordData></srw:record></srw:records>"
    b"</srw:searchRetrieveResponse>"
)
_SRU_DOCTYPE = b'<?xml version="1.0"?><!DOCTYPE reponse><root/>'


class _Reponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(
        self,
        charge: Any = None,
        contenu: bytes = b"",
        statut: int = 200,
        erreur_json: Exception | None = None,
    ) -> None:
        self._charge = charge
        self.content = contenu
        self.status_code = statut
        self._erreur_json = erreur_json

    def json(self) -> Any:
        if self._erreur_json is not None:
            raise self._erreur_json
        return self._charge


class _HttpSimule:
    """BNF + OpenLibrary + Google scriptés séparément, requêtes retenues."""

    def __init__(
        self,
        bnf_contenu: bytes = _SRU_VIDE,
        bnf_statut: int = 200,
        bnf_erreur: Exception | None = None,
        ol_charge: Any = None,
        ol_statut: int = 200,
        ol_erreur: Exception | None = None,
        ol_erreur_json: Exception | None = None,
        google_cible: Any = None,
        google_libre: Any = None,
        google_statut: int = 200,
        google_erreur_cible: Exception | None = None,
        google_erreur_libre: Exception | None = None,
    ) -> None:
        self.bnf_contenu = bnf_contenu
        self.bnf_statut = bnf_statut
        self.bnf_erreur = bnf_erreur
        self.ol_charge = {"docs": []} if ol_charge is None else ol_charge
        self.ol_statut = ol_statut
        self.ol_erreur = ol_erreur
        self.ol_erreur_json = ol_erreur_json
        self.google_cible = google_cible
        self.google_libre = google_libre
        self.google_statut = google_statut
        self.google_erreur_cible = google_erreur_cible
        self.google_erreur_libre = google_erreur_libre
        self.requetes_google: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> _Reponse:
        params = kwargs.get("params")
        query = params if isinstance(params, dict) else {}
        if "googleapis.com" in url:
            question = str(query.get("q", ""))
            self.requetes_google.append(question)
            libre = not (
                question.startswith("intitle:") or question.startswith("isbn:")
            )
            erreur = self.google_erreur_libre if libre else self.google_erreur_cible
            if erreur is not None:
                raise erreur
            if self.google_statut != 200:
                return _Reponse(statut=self.google_statut)
            charge = self.google_libre if libre else self.google_cible
            if charge is None:
                charge = {"totalItems": 0}
            return _Reponse(charge=charge)
        if "openlibrary.org" in url:
            if self.ol_erreur is not None:
                raise self.ol_erreur
            return _Reponse(
                charge=self.ol_charge,
                statut=self.ol_statut,
                erreur_json=self.ol_erreur_json,
            )
        if self.bnf_erreur is not None:
            raise self.bnf_erreur
        return _Reponse(contenu=self.bnf_contenu, statut=self.bnf_statut)


def _service(http: _HttpSimule, monkeypatch: pytest.MonkeyPatch) -> BookMetadataService:
    monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr("cle-de-test"))
    return BookMetadataService(http_client=http)  # type: ignore[arg-type]


def test_invariants_du_rapport() -> None:
    """Un rapport = un chemin ; un rapport ISBN porte toujours un verdict."""
    with pytest.raises(ValueError, match="pas de verdict"):
        BibliographicReport(
            kind="title",
            resolution=None,
            verdict=IsbnVerdict(outcome="inconnu"),
        )
    with pytest.raises(ValueError, match="jamais de résolution"):
        BibliographicReport(
            kind="isbn",
            resolution=None,
            verdict=None,
        )
    titre_vide = BibliographicReport(kind="title")
    assert titre_vide.resolution is None
    assert titre_vide.attempts == ()


@pytest.mark.asyncio
async def test_brut_bnf_rejete_compte_comme_recu() -> None:
    """Notices brutes sans ISBN : `has_results=True`,items vides, `not_found`."""
    service = BookMetadataService(http_client=_HttpSimule(bnf_contenu=_SRU_SANS_ISBN))  # type: ignore[arg-type]

    rapport = await service.resolve_for_alert_report("Blacksad")

    assert [a.provider for a in rapport.attempts] == [AUTORITE_BNF, AUTORITE_OPENLIBRARY]
    bnf = rapport.attempts[0]
    assert bnf.status == ProviderQueryStatus.SUCCESS
    assert bnf.has_results is True
    resultat = result_from_report(rapport)
    assert resultat.status == BookSearchStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_volume_sans_titre_compte_comme_recu_puis_repli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un volume sans titre est brut reçu ; le repli libre part quand même."""
    http = _HttpSimule(
        google_cible={"items": [{"volumeInfo": {"authors": ["Nemo"]}}]},
        google_libre={"items": [_volume_google("Blacksad", ["Canales"])]},
    )
    service = _service(http, monkeypatch)

    rapport = await service.resolve_for_alert_report("Blacksad")

    ciblee, libre = rapport.attempts[-2], rapport.attempts[-1]
    assert (ciblee.search_type, ciblee.status) == ("title", ProviderQueryStatus.SUCCESS)
    assert ciblee.has_results is True
    assert libre.search_type == "free"


@pytest.mark.asyncio
async def test_ciblee_vide_puis_libre_en_timeout_est_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`[success, timeout]` n'équivaut pas à deux succès vides."""
    http = _HttpSimule(google_erreur_libre=TimeoutError("délai dépassé"))
    service = _service(http, monkeypatch)

    rapport = await service.resolve_for_alert_report("Blacksad")

    statuts = [(a.provider, a.search_type, a.status) for a in rapport.attempts]
    assert (AUTORITE_GOOGLE_BOOKS, "title", ProviderQueryStatus.SUCCESS) in statuts
    assert (AUTORITE_GOOGLE_BOOKS, "free", ProviderQueryStatus.TIMEOUT) in statuts
    resultat = result_from_report(rapport)
    assert resultat.status == BookSearchStatus.UNAVAILABLE
    assert AUTORITE_GOOGLE_BOOKS in resultat.unavailable


@pytest.mark.asyncio
async def test_repli_pendant_panne_bnf_n_est_pas_confirme() -> None:
    """Divergence documentée : historique identifié, nouveau contrat `partial`."""
    http = _HttpSimule(
        bnf_erreur=TimeoutError("délai dépassé"),
        ol_charge={"docs": [_doc_ol("Blacksad", "Canales")]},
    )
    service = BookMetadataService(http_client=http)  # type: ignore[arg-type]

    historique = await service.resolve_for_alert("Blacksad")
    assert historique is not None
    assert historique.best.source == AUTORITE_OPENLIBRARY

    rapport = await service.resolve_for_alert_report("Blacksad")
    resultat = result_from_report(rapport)

    assert resultat.status == BookSearchStatus.PARTIAL
    assert resultat.reference is None
    assert [o.isbn13 for o in resultat.works] == [ISBN_VALIDE]
    assert resultat.provenance == AUTORITE_OPENLIBRARY
    assert AUTORITE_BNF in resultat.unavailable


@pytest.mark.asyncio
async def test_repli_ambigu_pendant_panne_bnf_reste_ambigu() -> None:
    """Plusieurs œuvres pendant une panne : `ambiguous` des deux côtés."""
    http = _HttpSimule(
        bnf_erreur=TimeoutError("délai dépassé"),
        ol_charge={
            "docs": [
                _doc_ol("Blacksad", "Canales", ISBN_VALIDE),
                _doc_ol("Blacksad", "Autre", ISBN_AUTRE),
            ]
        },
    )
    service = BookMetadataService(http_client=http)  # type: ignore[arg-type]

    assert (await service.resolve_for_alert("Blacksad")) is not None
    resultat = result_from_report(await service.resolve_for_alert_report("Blacksad"))

    assert resultat.status == BookSearchStatus.AMBIGUOUS
    assert resultat.reference is None
    assert len(resultat.works) == 2


@pytest.mark.asyncio
async def test_panne_bnf_sans_candidat_est_unavailable() -> None:
    """Aucun candidat et un échec : pas de `not_found` par défaut."""
    service = BookMetadataService(  # type: ignore[arg-type]
        http_client=_HttpSimule(bnf_erreur=TimeoutError("délai dépassé"))
    )

    assert await service.resolve_for_alert("Blacksad") is None
    resultat = result_from_report(await service.resolve_for_alert_report("Blacksad"))

    assert resultat.status == BookSearchStatus.UNAVAILABLE
    assert AUTORITE_BNF in resultat.unavailable


@pytest.mark.asyncio
async def test_consulted_suit_l_ordre_reel_sans_doublons() -> None:
    """`title` + `title_anywhere` BNF : deux tentatives, un seul consulté."""
    service = BookMetadataService(http_client=_HttpSimule())  # type: ignore[arg-type]

    rapport = await service.resolve_for_alert_report("Blacksad", "Canales")
    resultat = result_from_report(rapport)

    assert [(a.provider, a.search_type) for a in rapport.attempts] == [
        (AUTORITE_BNF, "title"),
        (AUTORITE_BNF, "title_anywhere"),
        (AUTORITE_OPENLIBRARY, "title"),
    ]
    assert resultat.consulted == (AUTORITE_BNF, AUTORITE_OPENLIBRARY)
    assert resultat.status == BookSearchStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_isbn_consulted_egale_verdict_consulted() -> None:
    """Le dérivé du rapport ne réécrit pas la liste du verdict."""
    for http, attendu in (
        (_HttpSimule(), (AUTORITE_BNF, AUTORITE_OPENLIBRARY)),
        (
            _HttpSimule(bnf_erreur=TimeoutError("délai dépassé")),
            (AUTORITE_BNF, AUTORITE_OPENLIBRARY),
        ),
    ):
        service = BookMetadataService(http_client=http)  # type: ignore[arg-type]
        rapport = await service.consult_authorities_report("9782810206087")
        assert rapport.verdict is not None
        resultat = result_from_verdict_report(rapport)
        assert resultat.consulted == rapport.verdict.consulted == attendu


@pytest.mark.asyncio
async def test_contradiction_reste_distincte_d_une_panne(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un ISBN confirmé pour un autre ouvrage : `not_found` + conflit, pas `unavailable`."""
    http = _HttpSimule(
        google_cible={
            "items": [_volume_google("Livre Reel", ["Vrai Auteur"], "9782810206087")]
        }
    )
    service = _service(http, monkeypatch)

    rapport = await service.consult_authorities_report("9782810206087")
    assert rapport.verdict is not None
    assert rapport.verdict.outcome == "confirme"
    assert all(a.status == ProviderQueryStatus.SUCCESS for a in rapport.attempts)

    resultat = result_from_verdict_report(rapport, expected_title="Un tout autre titre")

    assert resultat.status == BookSearchStatus.NOT_FOUND
    assert resultat.conflict is not None
    assert resultat.conflict.isbn13 == "9782810206087"


@pytest.mark.asyncio
async def test_panne_openlibrary_puis_vide_vaut_unavailable() -> None:
    """Un ancien `inconnu` ne prend plus le pas sur une tentative en échec."""
    service = BookMetadataService(  # type: ignore[arg-type]
        http_client=_HttpSimule(ol_statut=500)
    )

    rapport = await service.consult_authorities_report("9782810206087")
    assert rapport.verdict is not None
    assert rapport.verdict.outcome == "inconnu"
    resultat = result_from_verdict_report(rapport)

    assert resultat.status == BookSearchStatus.UNAVAILABLE
    assert AUTORITE_OPENLIBRARY in resultat.unavailable


@pytest.mark.asyncio
async def test_libelles_de_panne_bnf_preserves() -> None:
    """Compatibilité : les motifs historiques sont rendus à l'identique."""
    for http, attendu in (
        (_HttpSimule(bnf_statut=503), "statut HTTP 503"),
        (
            _HttpSimule(bnf_erreur=TimeoutError("délai dépassé")),
            "TimeoutError: délai dépassé",
        ),
        (_HttpSimule(bnf_contenu=_SRU_DOCTYPE), "DOCTYPE déclaré dans la réponse"),
    ):
        service = BookMetadataService(http_client=http)  # type: ignore[arg-type]
        verdict = await service.consult_authorities("9782810206087")
        assert verdict.panne == attendu


@pytest.mark.asyncio
async def test_isbn_invalide_rejete_sans_reseau() -> None:
    """Le nouveau cas d'usage rejette explicitement, avant tout appel."""
    service = AsyncMock(spec=BookMetadataService)
    cas_usage = BookSearchUseCase(service=service)

    with pytest.raises(ValueError, match="ISBN invalide"):
        await cas_usage.search_by_isbn("pas-un-isbn")

    assert service.mock_calls == []


@pytest.mark.asyncio
async def test_resultat_serialise_sans_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ni le contrat sérialisé ni les logs ne portent la clé, même en échec."""
    monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr(CLE_FACTICE))
    http = _HttpSimule(google_erreur_cible=TimeoutError("délai dépassé"))
    service = BookMetadataService(http_client=http)  # type: ignore[arg-type]
    cas_usage = BookSearchUseCase(service=service)

    with caplog.at_level(logging.WARNING):
        resultat = await cas_usage.search_by_title("Blacksad")

    serialise = json.dumps(asdict(resultat), default=str)
    assert CLE_FACTICE not in serialise
    assert CLE_FACTICE not in caplog.text
    assert "key=" not in caplog.text
    assert resultat.status == BookSearchStatus.UNAVAILABLE

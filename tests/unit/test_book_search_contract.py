"""Contrat bibliographique normalisé (Lot 0) : mapping explicite et testé.

Fixtures et mocks uniquement : aucune requête réseau, aucune base, aucun LLM.
Le comportement Telegram existant est vérifié inchangé par les suites
dédiées (`test_add_isbn`, `test_add_creation_card`, `test_add_keyboards`,
`test_add_screens`, `test_alert_edition_choices`, `test_book_metadata_service`),
rejouées avec ce fichier.
"""

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.domain.enums import BookSearchStatus, MatchConfidence
from app.services.book_metadata_service import (
    AUTORITE_BNF,
    AUTORITE_GOOGLE_BOOKS,
    AUTORITE_OPENLIBRARY,
    GOOGLE_BOOKS_SOURCE_NOTE,
    BookMetadata,
    BookMetadataService,
    BookResolution,
    IsbnVerdict,
)
from app.use_cases.book_search import (
    BookEditionChoice,
    BookSearchResult,
    BookSearchUseCase,
    result_from_isbn_verdict,
    result_from_resolution,
)

# ISBN 9782810206087 (« Les Enfants perdus », Neyef) :ISBN-13 valide qu'aucune
# autorité moquée ici ne connaît — cas `not_found` réel du Lot 0.
ISBN_INTROUVABLE = "9782810206087"
ISBN_CANDIDAT = "9782369903086"
ISBN_AUTRE_OUVRAGE = "9782369903062"


@pytest.fixture(autouse=True)
def _cle_google_books_absente(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise le `.env` du poste : sans clé, Google n'est pas consulté."""
    monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr(""))


def _notice(isbn: str | None = ISBN_CANDIDAT, **champs: Any) -> BookMetadata:
    fields: dict[str, Any] = {
        "title": "La couleur des choses",
        "author": "Martin Panchaud",
        "publisher": "Çà et là",
        "isbn13": isbn,
        "source": AUTORITE_BNF,
        "confidence": MatchConfidence.HIGH,
        "publication_year": 2022,
        "format_detail": "1 vol. (224 p.)",
    }
    fields.update(champs)
    return BookMetadata(**fields)


def test_resolution_confirmee_est_found_avec_editions_separees() -> None:
    """Une résolution non ambiguë confirme l'identité et liste les éditions."""
    principale = _notice()
    reedition = _notice(
        isbn="9782369903093", publisher="Autre éditeur", publication_year=2018
    )
    doublon = _notice(isbn="9782369903093", publisher="Autre éditeur")
    resolution = BookResolution(best=principale, alternatives=[reedition, doublon])

    resultat = result_from_resolution(resolution)

    assert resultat.status == BookSearchStatus.FOUND
    assert resultat.reference is not None
    assert resultat.reference.isbn13 == ISBN_CANDIDAT
    assert resultat.reference == resultat.editions[0]
    # Deux ISBN différents restent deux éditions, le doublon est dédupliqué.
    assert [edition.isbn13 for edition in resultat.editions] == [
        ISBN_CANDIDAT,
        "9782369903093",
    ]
    assert resultat.works == ()
    assert resultat.provenance == AUTORITE_BNF
    assert resultat.note is None  # BNF : aucune provenance de repli à signaler.


def test_oeuvre_ambigue_liste_les_oeuvres_sans_edition() -> None:
    """Tant que c'est ambigu, rien n'est rattaché : des choix, pas une fiche."""
    roman = _notice(
        isbn=ISBN_AUTRE_OUVRAGE,
        title="Les Enfants perdus",
        author="Rosemonde Ahou de Saintange",
        publication_year=1989,
    )
    bande = _notice(
        isbn="9782810206088", title="Les Enfants perdus", author="Neyef",
        publication_year=2025,
    )
    resolution = BookResolution(best=roman, other_works=[bande])

    resultat = result_from_resolution(resolution)

    assert resultat.status == BookSearchStatus.AMBIGUOUS
    assert resultat.reference is None
    assert resultat.editions == ()
    assert [oeuvre.isbn13 for oeuvre in resultat.works] == [
        ISBN_AUTRE_OUVRAGE,
        "9782810206088",
    ]
    assert all(oeuvre.provenance == AUTORITE_BNF for oeuvre in resultat.works)
    assert resultat.note is not None
    assert "autre ouvrage" in resultat.note


def test_indice_auteur_concordant_leve_l_ambiguite() -> None:
    """`matched` tranche l'homonymie : les autres notices restent homonymes."""
    roman = _notice(isbn=ISBN_AUTRE_OUVRAGE, title="Les Enfants perdus")
    bande = _notice(isbn="9782810206088", title="Les Enfants perdus")
    resolution = BookResolution(
        best=roman, other_works=[bande], author_hint_match="matched"
    )

    assert result_from_resolution(resolution).status == BookSearchStatus.FOUND


def test_aucune_resolution_est_not_found_sans_consultation_inventee() -> None:
    """`None` ne dit pas si c'est une absence ou une panne : `consulted=None`."""
    resultat = result_from_resolution(None)

    assert resultat.status == BookSearchStatus.NOT_FOUND
    assert resultat.reference is None
    assert resultat.consulted is None
    assert resultat.unavailable == ()


def test_isbn_confirme_sans_titre_de_reference_est_found() -> None:
    """Aucun titre de référence n'est exigé pour une recherche par ISBN seul."""
    verdict = IsbnVerdict(
        outcome="confirme", notice=_notice(), consulted=(AUTORITE_BNF,)
    )

    resultat = result_from_isbn_verdict(verdict)

    assert resultat.status == BookSearchStatus.FOUND
    assert resultat.reference is not None
    assert resultat.reference.isbn13 == ISBN_CANDIDAT
    assert resultat.consulted == (AUTORITE_BNF,)


def test_isbn_contredit_est_not_found_avec_conflit_expose() -> None:
    """L'ISBN d'un autre ouvrage : rien de confirmé, le réel en `conflict`."""
    verdict = IsbnVerdict(
        outcome="contradiction",
        notice=_notice(isbn=ISBN_AUTRE_OUVRAGE, title="Keeping two"),
        consulted=(AUTORITE_BNF,),
    )

    resultat = result_from_isbn_verdict(verdict, expected_title="La couleur des choses")

    assert resultat.status == BookSearchStatus.NOT_FOUND
    assert resultat.reference is None
    assert resultat.conflict is not None
    assert resultat.conflict.isbn13 == ISBN_AUTRE_OUVRAGE
    assert resultat.conflict.title == "Keeping two"


def test_isbn_confirme_mais_titre_discordant_est_contradictoire() -> None:
    """Le contrôle de titre s'applique au même seuil que `confirm_isbn_for_title`."""
    verdict = IsbnVerdict(
        outcome="confirme",
        notice=_notice(title="Keeping two"),
        consulted=(AUTORITE_BNF,),
    )

    resultat = result_from_isbn_verdict(verdict, expected_title="La couleur des choses")

    assert resultat.status == BookSearchStatus.NOT_FOUND
    assert resultat.conflict is not None


def test_isbn_inconnu_est_not_found_avec_autorites_reelles() -> None:
    """`inconnu` : les autorités ont répondu « rien », sans panne."""
    verdict = IsbnVerdict(
        outcome="inconnu", consulted=(AUTORITE_BNF, AUTORITE_OPENLIBRARY)
    )

    resultat = result_from_isbn_verdict(verdict)

    assert resultat.status == BookSearchStatus.NOT_FOUND
    assert resultat.reference is None
    assert resultat.consulted == (AUTORITE_BNF, AUTORITE_OPENLIBRARY)
    assert AUTORITE_GOOGLE_BOOKS not in (resultat.consulted or ())


def test_silence_sans_notice_est_unavailable() -> None:
    """Aucune autorité n'a pu répondre : ni absence, ni ambiguïté."""
    verdict = IsbnVerdict(
        outcome="silence",
        consulted=(AUTORITE_BNF, AUTORITE_OPENLIBRARY),
        panne="TimeoutError: délai dépassé",
    )

    resultat = result_from_isbn_verdict(verdict)

    assert resultat.status == BookSearchStatus.UNAVAILABLE
    assert resultat.reference is None
    assert AUTORITE_BNF in resultat.unavailable


def test_silence_avec_notice_de_repli_est_partiel_non_confirme() -> None:
    """Règle préservée : un repli pendant une panne BNF ne confirme jamais."""
    notice = _notice(source=AUTORITE_OPENLIBRARY, confidence=MatchConfidence.MEDIUM)
    verdict = IsbnVerdict(
        outcome="silence",
        notice=notice,
        consulted=(AUTORITE_BNF, AUTORITE_OPENLIBRARY),
        panne="TimeoutError: délai dépassé",
    )

    resultat = result_from_isbn_verdict(verdict)

    assert resultat.status == BookSearchStatus.PARTIAL
    assert resultat.reference is None
    assert [oeuvre.isbn13 for oeuvre in resultat.works] == [ISBN_CANDIDAT]
    assert resultat.provenance == AUTORITE_OPENLIBRARY


def test_notice_sans_isbn_est_partielle() -> None:
    """Une notice Google sans ISBN nomme l'ouvrage sans confirmer d'édition."""
    notice = _notice(
        isbn=None, source=AUTORITE_GOOGLE_BOOKS, confidence=MatchConfidence.MEDIUM
    )
    resolution = BookResolution(best=notice)

    resultat = result_from_resolution(resolution)

    assert resultat.status == BookSearchStatus.PARTIAL
    assert resultat.reference is None
    assert resultat.editions == ()
    assert len(resultat.works) == 1
    assert resultat.works[0].isbn13 is None


def test_metadonnees_inconnues_restent_nulles() -> None:
    """Auteur et éditeur inconnus à `None`, sans langue, rôle ni flag numérique."""
    notice = _notice(author=None, publisher=None, publication_year=None)
    verdict = IsbnVerdict(
        outcome="confirme", notice=notice, consulted=(AUTORITE_BNF,)
    )

    resultat = result_from_isbn_verdict(verdict)

    assert resultat.status == BookSearchStatus.FOUND
    assert resultat.reference is not None
    assert resultat.reference.publisher is None
    assert resultat.reference.publication_year is None
    assert not hasattr(resultat.reference, "language")
    assert not hasattr(resultat.reference, "roles")
    assert not hasattr(resultat.reference, "is_ebook")


def test_statut_found_implique_reference_et_reciproquement() -> None:
    """Garde-fou du contrat : une identité confirmée se distingue des candidats."""
    with pytest.raises(ValueError, match="reference"):
        BookSearchResult(status=BookSearchStatus.FOUND)
    with pytest.raises(ValueError, match="reference"):
        BookSearchResult(
            status=BookSearchStatus.NOT_FOUND,
            reference=BookEditionChoice(
                isbn13=ISBN_CANDIDAT, provenance=AUTORITE_BNF
            ),
        )


class _ReponseVide:
    """Réponse HTTP minimale : succès sans résultat, jamais une panne."""

    status_code = 200

    def __init__(self, charge: Any = None, contenu: bytes = b"") -> None:
        self._charge = charge
        self.content = contenu

    def json(self) -> Any:
        return self._charge


class _ClientCatalogueVide:
    """SRU BNF vide + OpenLibrary sans document ; Google doit rester muet."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str, **_kwargs: Any) -> _ReponseVide:
        self.urls.append(url)
        if "googleapis.com" in url:
            raise AssertionError("Google Books ne doit pas être interrogé sans clé")
        if "openlibrary.org" in url:
            return _ReponseVide(charge={"docs": []})
        return _ReponseVide(contenu=b'<?xml version="1.0"?><root/>')


class _ClientBnFEnPanne(_ClientCatalogueVide):
    """La BNF ne répond pas, OpenLibrary répond « rien » : `silence`, pas `inconnu`."""

    async def get(self, url: str, **_kwargs: Any) -> _ReponseVide:
        self.urls.append(url)
        if "googleapis.com" in url:
            raise AssertionError("Google Books ne doit pas être interrogé sans clé")
        if "openlibrary.org" in url:
            return _ReponseVide(charge={"docs": []})
        raise TimeoutError("délai dépassé")


@pytest.mark.asyncio
async def test_google_absent_des_consultes_sans_cle() -> None:
    """Sans clé, Google n'est ni interrogé ni annoncé comme consulté."""
    service = BookMetadataService(http_client=_ClientCatalogueVide())
    cas_usage = BookSearchUseCase(service=service)

    resultat = await cas_usage.search_by_isbn(ISBN_INTROUVABLE)

    assert resultat.status == BookSearchStatus.NOT_FOUND
    assert resultat.consulted == (AUTORITE_BNF, AUTORITE_OPENLIBRARY)
    assert service.http_client is not None
    assert not any("googleapis.com" in url for url in service.http_client.urls)


@pytest.mark.asyncio
async def test_panne_bnf_est_unavailable_pas_inconnu() -> None:
    """Une panne ne se lit jamais comme une absence de l'ouvrage."""
    service = BookMetadataService(http_client=_ClientBnFEnPanne())
    cas_usage = BookSearchUseCase(service=service)

    resultat = await cas_usage.search_by_isbn(ISBN_INTROUVABLE)

    assert resultat.status == BookSearchStatus.UNAVAILABLE
    assert AUTORITE_BNF in resultat.unavailable


@pytest.mark.asyncio
async def test_cas_usage_titre_delegue_sans_base_ni_llm() -> None:
    """Le cas d'usage ne connaît que le service : ni dépôt, ni moteur IA."""
    from app.services.book_metadata_service import BibliographicReport

    service = AsyncMock(spec=BookMetadataService)
    service.resolve_for_alert_report.return_value = BibliographicReport(
        kind="title", resolution=BookResolution(best=_notice())
    )
    cas_usage = BookSearchUseCase(service=service)

    resultat = await cas_usage.search_by_title("La couleur des choses", "Panchaud")

    service.resolve_for_alert_report.assert_awaited_once_with(
        "La couleur des choses", "Panchaud"
    )
    assert resultat.status == BookSearchStatus.FOUND


def test_module_sans_dependance_base_ni_llm() -> None:
    """Garde-fou statique : le contrat ne tire ni persistance ni IA."""
    arbre = ast.parse(Path("app/use_cases/book_search.py").read_text(encoding="utf-8"))
    racines: set[str] = set()
    modules: set[str] = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Import):
            racines.update(alias.name.split(".")[0] for alias in noeud.names)
        elif isinstance(noeud, ast.ImportFrom) and noeud.module:
            racines.add(noeud.module.split(".")[0])
            modules.add(noeud.module)
    assert not (racines & {"llm", "redis", "sqlalchemy", "alembic"}), racines
    assert not any(
        module == "app.db" or module.startswith("app.db.") for module in modules
    ), modules


def test_resolution_google_trouvee_porte_sa_provenance() -> None:
    """Un candidat du repli (libre ou ciblé) : `found`, source Google nommée."""
    notice = _notice(
        source=AUTORITE_GOOGLE_BOOKS, confidence=MatchConfidence.MEDIUM
    )
    resolution = BookResolution(best=notice)

    resultat = result_from_resolution(resolution)

    assert resultat.status == BookSearchStatus.FOUND
    assert resultat.provenance == AUTORITE_GOOGLE_BOOKS
    assert resultat.note == GOOGLE_BOOKS_SOURCE_NOTE
    assert resultat.reference is not None
    assert resultat.reference.provenance == AUTORITE_GOOGLE_BOOKS

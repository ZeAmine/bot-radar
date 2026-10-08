"""Le réalignement d'un brouillon sur un ISBN saisi, face à une autorité qui décroche.

`realign_draft_to_isbn` décide de ce que devient l'identité d'une alerte en cours de
création : la notice de l'autorité fait autorité, une contradiction est refusée, un
silence vide la fiche. Une panne est un quatrième cas — ni confirmation, ni refus, ni
silence — et c'est celui qu'une notice de repli faisait disparaître.

Ces tests exercent le vrai `BookMetadataService` avec un client qui tombe en panne sur
un seul ISBN : les deux cas vois se répondent donc dans le même run, ce qui est la
seule façon de prouver qu'ils ne se confondent pas.
"""

from __future__ import annotations

import pytest

from app.bot.handlers import alerts_helpers
from app.services.book_metadata_service import BookMetadataService
from app.use_cases.book_search import BookSearchUseCase

ISBN_PANNE = "9782369903086"  # la BNF décroche sur ce code
ISBN_SAIN = "9782072909207"  # la BNF répond « je ne connais pas »

_SRU_VIDE = b"""<?xml version="1.0"?>
<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:numberOfRecords>0</srw:numberOfRecords>
</srw:searchRetrieveResponse>"""

_DOC = {
    "title": "La couleur des choses",
    "author_name": ["Martin Panchaud"],
    "publisher": ["Ça et là"],
    "first_publish_year": 2022,
    "isbn": [ISBN_PANNE, ISBN_SAIN],
}


class _Reponse:
    def __init__(self, status_code: int, content: bytes = b"", payload: dict | None = None):
        self.status_code = status_code
        self.content = content
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _ClientPanneSurUnISBN:
    """La BNF ne tombe que sur `ISBN_PANNE` ; OpenLibrary répond toujours."""

    async def get(self, url: str, **_kwargs: object) -> _Reponse:
        if "openlibrary.org" in url:
            return _Reponse(200, payload={"numFound": 1, "docs": [_DOC]})
        if ISBN_PANNE in url:
            raise TimeoutError("délai dépassé")
        return _Reponse(200, content=_SRU_VIDE)


@pytest.fixture
def autorite(monkeypatch) -> None:
    """Branche le cas d'usage sur le vrai service, avec le client qui décroche.

    `realign_draft_to_isbn` consomme désormais `BookSearchUseCase` : le simuler au
    niveau du service laisserait passer le mapping (statuts, T9). Ici le service réel
    tourne, donc le contrat est exercé de bout en bout, sans réseau.
    """
    monkeypatch.setattr(
        alerts_helpers,
        "BookSearchUseCase",
        lambda: BookSearchUseCase(BookMetadataService(http_client=_ClientPanneSurUnISBN())),
    )


def _brouillon() -> dict:
    return {"name": "La couleur des choses", "author": "Un auteur d'avant", "variants": ["avant"]}


@pytest.mark.asyncio
@pytest.mark.usefixtures("autorite")
async def test_une_panne_ne_construit_pas_une_fiche():
    """Personne n'a tranché : l'ISBN est gardé, mais rien ne lui est accroché.

    C'est le défaut : le repli OpenLibrary existait, et le brouillon partait avec son
    auteur, son éditeur et son `book_context` — une fiche que la BNF n'a jamais confirmée.
    """
    realignment = await alerts_helpers.realign_draft_to_isbn(_brouillon(), ISBN_PANNE)

    assert realignment.conflict is None, "une panne n'est pas une contradiction"
    assert realignment.updates["isbn"] == ISBN_PANNE, "l'ISBN saisi est conservé"
    assert realignment.updates["author"] is None
    assert realignment.updates["publisher"] is None
    assert realignment.updates["edition_alternatives"] == []
    assert realignment.updates["variants"] == [], "les mots-clés de l'ancien ouvrage tombent"


@pytest.mark.asyncio
@pytest.mark.usefixtures("autorite")
async def test_une_panne_le_dit_a_l_utilisateur():
    """La note dit que la vérification est à refaire, pas que l'ISBN est inconnu.

    Le motif technique reste dans les journaux : un `TimeoutError` dans un récapitulatif
    n'apprend rien et fait du bruit à l'utilisateur.
    """
    realignment = await alerts_helpers.realign_draft_to_isbn(_brouillon(), ISBN_PANNE)
    note = realignment.updates["isbn_note"]

    assert "Sources indisponibles : BNF" in note
    assert "Candidat" in note
    assert "TimeoutError" not in note


@pytest.mark.asyncio
@pytest.mark.usefixtures("autorite")
async def test_un_repli_apres_une_reponse_de_la_bnf_est_accepte():
    """Le contraste exact : la BNF ayant répondu, le repli est un repli."""
    realignment = await alerts_helpers.realign_draft_to_isbn(_brouillon(), ISBN_SAIN)

    assert realignment.conflict is None
    assert realignment.updates["author"] == "Martin Panchaud"
    assert realignment.updates["publisher"] == "Ça et là"
    assert "Source OpenLibrary (repli BNF)" in realignment.updates["isbn_note"]

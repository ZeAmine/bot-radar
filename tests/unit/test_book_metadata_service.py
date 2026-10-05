"""Tests unitaires pour le service BookMetadataService (parsing, BNF SRU, recommandations)."""

import urllib.parse
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, patch

import pytest

from app.core.constants import MatchConfidence
from app.services.book_metadata_service import (
    AUTORITE_BNF,
    AUTORITE_OPENLIBRARY,
    OPENLIBRARY_SOURCE_NOTE,
    BookMetadata,
    BookMetadataService,
    BookResolution,
    IsbnVerdict,
    _declares_doctype,
    ambiguity_note,
    clean_bnf_creator,
    clean_bnf_publisher,
    clean_bnf_title,
    clean_openlibrary_author,
    generate_recommended_keywords,
    parse_query_title_and_author,
    provenance_note,
    same_author,
    title_similarity,
)


def test_parse_query_title_and_author():
    """Vérifie l'extraction du titre et de l'indice d'auteur dans différentes formulations."""
    assert parse_query_title_and_author("deathbringer de ismael le Grand") == (
        "deathbringer",
        "ismael le Grand",
    )
    assert parse_query_title_and_author("la saga de grimr de Jeremy Morrow") == (
        "la saga de grimr",
        "Jeremy Morrow",
    )
    assert parse_query_title_and_author("La bête par Chabouté") == ("La bête", "Chabouté")
    assert parse_query_title_and_author("Blacksad") == ("Blacksad", None)


def test_clean_bnf_helpers():
    """Vérifie le nettoyage des champs bruts issus de la BNF."""
    # Titres
    assert clean_bnf_title("La saga de Grimr / Jérémie Moreau") == "La saga de Grimr"
    assert clean_bnf_title("Ion mud / Amaury Bündgen") == "Ion mud"

    # Créateurs
    assert clean_bnf_creator("Moreau, Jérémie (1987-....). Auteur du texte") == "Jérémie Moreau"
    assert clean_bnf_creator("Legrand, Ismaël (Auteur du texte)") == "Ismaël Legrand"
    # Rôle BNF non parenthésé : il ne doit pas coller au nom jusqu'aux boutons.
    assert clean_bnf_creator("Fébule, Nicolas. Auteur du texte") == "Nicolas Fébule"
    assert clean_bnf_creator("Develle, Jeanne. Auteur du texte") == "Jeanne Develle"

    # Éditeurs
    assert clean_bnf_publisher("Éditions Delcourt (Paris)") == "Éditions Delcourt"
    assert clean_bnf_publisher("Casterman (Bruxelles)") == "Casterman"


def test_generate_recommended_keywords():
    """Vérifie la génération automatique des mots-clés d'inclusion, d'exclusion et variantes."""
    # Deathbringer
    inc, exc, var = generate_recommended_keywords("Deathbringer", "Ismaël Legrand")
    assert "deathbringer" in inc
    assert "figurine" in exc
    assert "warhammer" in exc
    assert "death bringer" in var

    # La saga de Grimr
    inc_g, exc_g, var_g = generate_recommended_keywords("La saga de Grimr", "Jérémie Moreau")
    assert "la saga de grimr" in inc_g
    assert "saga de grimr" in inc_g
    assert "roman" in exc_g
    assert "poche" in exc_g


@pytest.mark.asyncio
async def test_lookup_by_title_and_author_mocked():
    """Vérifie que lookup_by_title_and_author trie et renvoie le meilleur résultat."""
    service = BookMetadataService()

    fake_record = BookMetadata(
        title="Deathbringer",
        author="Ismaël Legrand",
        publisher="Guy Delcourt Productions",
        isbn13="9782413078128",
        source="bnf",
    )

    with patch.object(service, "_query_bnf_sru", new=AsyncMock(return_value=[fake_record])):
        res = await service.lookup("deathbringer de ismael le Grand")
        assert res is not None
        assert res.title == "Deathbringer"
        assert res.author == "Ismaël Legrand"
        assert res.isbn13 == "9782413078128"
        assert "deathbringer" in res.recommended_includes
        assert "death bringer" in res.recommended_variants


async def test_lookup_series_notices_keeps_the_tomes_and_drops_the_lookalikes():
    """Une série expose ses tomes, pas les ouvrages voisins qui partagent un mot."""
    service = BookMetadataService()
    records = [
        BookMetadata(
            title="Blacksad", author="Juan Díaz Canales",
            isbn13="9782205049504", source=AUTORITE_BNF,
        ),
        BookMetadata(
            title="Blacksad Tome 2", author="Juan Díaz Canales",
            isbn13="9782205049696", source=AUTORITE_BNF,
        ),
        BookMetadata(
            title="Blacksad Tome 3", author="Juan Díaz Canales",
            isbn13="9782205051484", source=AUTORITE_BNF,
        ),
        # Même préfixe de titre, autre auteur : ce n'est pas un tome de la série.
        BookMetadata(
            title="Blacksad : la bande dessinée noire", author="Un Autre",
            isbn13="9780000000002", source=AUTORITE_BNF,
        ),
        BookMetadata(
            title="Une tout autre série", author="Un Autre",
            isbn13="9780000000019", source=AUTORITE_BNF,
        ),
    ]
    with patch.object(service, "_query_bnf_sru", new=AsyncMock(return_value=records)):
        notices = await service.lookup_series_notices(
            "Blacksad", author_hint="Juan Diaz Canales"
        )

    assert [notice.title for notice in notices] == [
        "Blacksad",
        "Blacksad Tome 2",
        "Blacksad Tome 3",
    ]


async def test_lookup_series_notices_is_bounded_and_deduplicates_isbns():
    """Un ISBN n'est rattaché qu'une fois, et la série reste bornée."""
    service = BookMetadataService()
    records = [
        BookMetadata(
            title=f"Blacksad Tome {index}", author="Juan Díaz Canales",
            isbn13=f"97822050514{index:02d}", source=AUTORITE_BNF,
        )
        for index in range(1, 9)
    ]
    records.append(records[0])  # doublon d'ISBN

    with patch.object(service, "_query_bnf_sru", new=AsyncMock(return_value=records)):
        notices = await service.lookup_series_notices("Blacksad", limit=3)
        empty = await service.lookup_series_notices("   ")

    assert len(notices) == 3
    assert len({notice.isbn13 for notice in notices}) == 3
    assert empty == []


# --- Cas réel : « La couleur des choses » de Martin Panchaud ---------------------
# Deux éditions BNF pour le même titre, et un ISBN réel attribué à un AUTRE ouvrage
# du même éditeur (« Keeping two »), le type d'erreur produite par un LLM.
BNF_RECORD_TEMPLATE = """<srw:record>
<srw:recordSchema>dc</srw:recordSchema>
<srw:recordData>
<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier>http://catalogue.bnf.fr/ark:/12148/cb{ark}</dc:identifier>
  <dc:title>{title}</dc:title>
  <dc:creator>{creator}</dc:creator>
  <dc:publisher>{publisher}</dc:publisher>
  {date}<dc:format>{fmt}</dc:format>
  <dc:identifier>ISBN {isbn}</dc:identifier>
</oai_dc:dc>
</srw:recordData>
</srw:record>"""

SRU_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">'
    "<srw:numberOfRecords>{count}</srw:numberOfRecords><srw:records>{records}"
    "</srw:records></srw:searchRetrieveResponse>"
)

PANCHAUD_EDITION_2022 = BNF_RECORD_TEMPLATE.format(
    ark="471036825",
    title="La couleur des choses / Martin Panchaud",
    creator="Panchaud, Martin (1982-....). Auteur du texte",
    publisher="Çà et là (Bussy-Saint-Georges)",
    date="<dc:date>2022</dc:date>",
    fmt="1 vol. (224 p.) : ill. en coul. ; 24 cm",
    isbn="9782369903086",
)
PANCHAUD_EDITION_19CM = BNF_RECORD_TEMPLATE.format(
    ark="48753868c",
    title="La couleur des choses",
    creator="Panchaud, Martin (Auteur du texte)",
    publisher="Editions çà et là (Bussy-Saint-Georges)",
    date="",
    fmt="1 vol. (224 p.) ; 19 cm",
    isbn="9782369904151",
)
OTHER_BOOK_SAME_PUBLISHER = BNF_RECORD_TEMPLATE.format(
    ark="471351310",
    title="Keeping two",
    creator="Crane, Jordan (1973-....). Auteur du texte",
    publisher="Çà et là (Bussy-Saint-Georges)",
    date="<dc:date>2022</dc:date>",
    fmt="1 vol. (non paginé [ca 320] p.) : ill. ; 22 cm",
    isbn="9782369903062",
)
# Ouvrages voisins partageant les mêmes mots : ne doivent jamais être retenus.
LOOKALIKE_DE_LA_COULEUR = BNF_RECORD_TEMPLATE.format(
    ark="911991356",
    title="De la couleur des choses",
    creator="Casabianca, François de (1973-....). Auteur du texte",
    publisher="éditions de l'Amitié",
    date="<dc:date>2009</dc:date>",
    fmt="1 vol. (95 p.) : ill. en coul. ; 21 cm",
    isbn="9782911991356",
)
LOOKALIKE_CLEMENCE = BNF_RECORD_TEMPLATE.format(
    ark="2010020834",
    title="Clémence et la couleur des choses",
    creator="Develle, Jeanne. Auteur du texte",
    publisher="Flammarion",
    date="<dc:date>1975</dc:date>",
    fmt="1 vol. (114 p.-[3] f. de pl.) : ill. ; 17 cm",
    isbn="9782010020834",
)

BNF_RECORDS = [
    PANCHAUD_EDITION_2022,
    PANCHAUD_EDITION_19CM,
    LOOKALIKE_DE_LA_COULEUR,
    LOOKALIKE_CLEMENCE,
    OTHER_BOOK_SAME_PUBLISHER,
]


# Document OpenLibrary : un même document regroupe toutes les éditions de l'œuvre,
# y compris une traduction anglaise (9780261102217) qui ne doit pas être retenue.
OPENLIBRARY_DOC = {
    "title": "La couleur des choses",
    "author_name": ["Martin Panchaud (Auteur du texte)"],
    "publisher": ["Ça et là"],
    "first_publish_year": 2022,
    "number_of_pages_median": 236,
    "language": ["fre"],
    "isbn": ["9782369903086", "2369903082", "9782072909207", "9780261102217"],
}
OPENLIBRARY_HOBBIT_DOC = {
    "title": "The Hobbit",
    "author_name": ["J. R. R. Tolkien"],
    "publisher": ["Allen & Unwin"],
    "first_publish_year": 1937,
    "isbn": ["9780261102217"],
}


class _FakeResponse:
    def __init__(self, content: bytes = b"", payload: dict[str, object] | None = None,
                 status_code: int = 200) -> None:
        self.status_code = status_code
        self.content = content
        self._payload = payload or {}

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeCatalogueClient:
    """Émule le SRU BNF et l'API Search d'OpenLibrary, et journalise les URL demandées."""

    def __init__(
        self,
        bnf_empty: bool = False,
        openlibrary_docs: list[dict[str, object]] | None = None,
        openlibrary_status: int = 200,
        bnf_panne_sur: str | None = None,
    ) -> None:
        self.queries: list[str] = []
        self.bnf_empty = bnf_empty
        self.bnf_panne_sur = bnf_panne_sur
        self.openlibrary_docs = openlibrary_docs
        self.openlibrary_status = openlibrary_status

    @property
    def openlibrary_queries(self) -> list[str]:
        return [url for url in self.queries if "openlibrary.org" in url]

    async def get(self, url: str, **_kwargs: object) -> _FakeResponse:
        self.queries.append(url)

        if "openlibrary.org" in url:
            if self.openlibrary_status != 200:
                return _FakeResponse(status_code=self.openlibrary_status)
            docs = self.openlibrary_docs
            if docs is None:
                params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                requested = params.get("isbn")
                docs = (
                    [{**OPENLIBRARY_DOC, "isbn": requested}]
                    if requested
                    else [OPENLIBRARY_DOC]
                )
            return _FakeResponse(payload={"numFound": len(docs), "docs": docs})

        if self.bnf_panne_sur and self.bnf_panne_sur in url:
            raise TimeoutError("délai dépassé")

        if self.bnf_empty:
            return _FakeResponse(SRU_HEADER.format(count=0, records="").encode("utf-8"))

        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
        matched = [
            record
            for record in BNF_RECORDS
            if f'"{record.split("ISBN ")[1].split("<")[0]}"' in query
        ] or BNF_RECORDS
        payload = SRU_HEADER.format(count=len(matched), records="".join(matched))
        return _FakeResponse(payload.encode("utf-8"))


_FakeBnFClient = _FakeCatalogueClient


def test_title_similarity_ignores_case_accents_and_subtitle():
    """Le suffixe d'auteur BNF ne doit pas faire tomber la similarité de titre."""
    assert title_similarity("La couleur des choses", "la Couleur des Choses") == 1.0
    assert title_similarity("La couleur des choses", "Keeping two") < 0.4
    assert title_similarity("", "Keeping two") == 0.0
    # Un titre voisin plus long n'est pas un simple sous-titre
    assert title_similarity("La couleur des choses", "De la couleur des choses") < 1.0


def test_same_author_compares_significant_words():
    """Les créateur·rices BNF sont comparés par mots, pas par chaîne brute."""
    assert same_author("Martin Panchaud", "Panchaud, Martin") is True
    assert same_author("Martin Panchaud", "François de Casabianca") is False
    assert same_author("Martin Panchaud", None) is False


@pytest.mark.asyncio
async def test_verify_isbn_rejects_isbn_belonging_to_another_book():
    """Un ISBN valide mais attribué à un autre ouvrage est refusé (piège du LLM)."""
    service = BookMetadataService(http_client=_FakeBnFClient())

    assert await service.verify_isbn("9782369903062", "La couleur des choses") is None
    # Le bon ISBN passe et renvoie la notice qui l'atteste
    verified = await service.verify_isbn("9782369903086", "La couleur des choses")
    assert verified is not None
    assert verified.isbn13 == "9782369903086"
    assert verified.author == "Martin Panchaud"
    assert verified.publication_year == 2022

    # Un ISBN au checksum invalide n'est jamais interrogé
    assert await service.verify_isbn("97823699030", "La couleur des choses") is None


@pytest.mark.asyncio
async def test_resolve_for_alert_prefers_dated_edition_and_lists_alternatives():
    """L'édition la mieux documentée devient la référence, l'autre reste visible.

    Les ouvrages voisins qui partagent les mêmes mots (« De la couleur des choses »)
    ne doivent pas être proposés comme éditions alternatives.
    """
    service = BookMetadataService(http_client=_FakeBnFClient())

    resolution = await service.resolve_for_alert("La couleur des choses", "Martin Panchaud")
    assert resolution is not None
    assert resolution.best.isbn13 == "9782369903086"
    assert resolution.best.format_detail == "1 vol. (224 p.) : ill. en coul. ; 24 cm"
    assert [alt.isbn13 for alt in resolution.alternatives] == ["9782369904151"]


@pytest.mark.asyncio
async def test_resolve_for_alert_rejects_unrelated_records():
    """Une recherche sans rapport ne doit jamais produire d'ISBN."""
    service = BookMetadataService(http_client=_FakeBnFClient())

    assert await service.resolve_for_alert("One Piece", "Oda Eiichiro") is None
    assert await service.resolve_for_alert("   ") is None


@pytest.mark.asyncio
async def test_openlibrary_fallback_resolves_book_absent_from_bnf():
    """Un ouvrage absent de la BNF est résolu via OpenLibrary, en confiance réduite."""
    client = _FakeCatalogueClient(bnf_empty=True)
    service = BookMetadataService(http_client=client)

    resolution = await service.resolve_for_alert("La couleur des choses", "Martin Panchaud")
    assert resolution is not None
    assert resolution.best.source == "openlibrary"
    assert resolution.best.confidence == MatchConfidence.MEDIUM
    # Édition francophone retenue, pas la traduction anglaise regroupée dans le document
    assert resolution.best.isbn13 == "9782369903086"
    assert resolution.best.author == "Martin Panchaud"
    assert resolution.best.format_detail == "236 p."
    assert client.openlibrary_queries


@pytest.mark.asyncio
async def test_openlibrary_fallback_exposes_other_french_editions_only():
    """Les autres éditions francophone du document deviennent des alternatives."""
    client = _FakeCatalogueClient(bnf_empty=True)
    service = BookMetadataService(http_client=client)

    resolution = await service.resolve_for_alert("La couleur des choses")
    assert resolution is not None
    assert [alt.isbn13 for alt in resolution.alternatives] == ["9782072909207"]


@pytest.mark.asyncio
async def test_openlibrary_fallback_keeps_the_numbered_notice_it_sets_aside():
    """Le document « Vol. 2 » n'est ni édition ni homonyme : il ne doit pas disparaître.

    Il porte la preuve que la notice retenue décrit une œuvre numérotée. Sans lui,
    `resolve_for_alert` rendait une résolution où rien ne trahissait la série — l'alerte
    partait avec l'ISBN d'un tome.
    """
    volume_1 = {
        "title": "Absolute Martian Manhunter",
        "author_name": ["Deniz Camp"],
        "publisher": ["DC Comics"],
        "first_publish_year": 2025,
        "isbn": ["9781799505204"],
    }
    volume_2 = {
        "title": "Absolute Martian Manhunter, Vol. 2",
        "author_name": ["Deniz Camp"],
        "publisher": ["DC Comics"],
        "first_publish_year": 2026,
        "isbn": ["9781799510871"],
    }
    client = _FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[volume_1, volume_2])
    service = BookMetadataService(http_client=client)

    resolution = await service.resolve_for_alert("Absolute Martian Manhunter")

    assert resolution is not None
    assert resolution.best.title == "Absolute Martian Manhunter"
    assert resolution.alternatives == []
    assert resolution.other_works == []
    assert resolution.notice_titles == (
        "Absolute Martian Manhunter",
        "Absolute Martian Manhunter, Vol. 2",
    )

    signal = resolution.series_signal("Absolute Martian Manhunter")
    assert signal.is_series is True
    assert signal.volumes == ("Tome 2",)


@pytest.mark.asyncio
async def test_openlibrary_verifies_isbn_unknown_to_bnf():
    """Le repli applique le même garde-fou de titre que la BNF."""
    client = _FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[OPENLIBRARY_DOC])
    service = BookMetadataService(http_client=client)

    verified = await service.verify_isbn("9782369903086", "La couleur des choses")
    assert verified is not None
    assert verified.source == "openlibrary"
    assert verified.isbn13 == "9782369903086"

    # Même ISBN, autre ouvrage attendu : refusé
    assert await service.verify_isbn("9782369903086", "Klara and the Sun") is None


@pytest.mark.asyncio
async def test_bnf_contradiction_is_never_overridden_by_openlibrary():
    """La BNF fait autorité : une contradiction suffit, OpenLibrary n'est pas consulté."""
    client = _FakeCatalogueClient(openlibrary_docs=[OPENLIBRARY_DOC])
    service = BookMetadataService(http_client=client)

    assert await service.verify_isbn("9782369903062", "La couleur des choses") is None
    assert client.openlibrary_queries == []


@pytest.mark.asyncio
async def test_openlibrary_not_queried_when_bnf_answers():
    """Aucun coût réseau supplémentaire quand la BNF a déjà résolu l'ouvrage."""
    client = _FakeCatalogueClient()
    service = BookMetadataService(http_client=client)

    resolution = await service.resolve_for_alert("La couleur des choses", "Martin Panchaud")
    assert resolution is not None
    assert resolution.best.source == "bnf"
    assert client.openlibrary_queries == []


@pytest.mark.asyncio
async def test_openlibrary_failure_degrades_to_title_search():
    """Une panne d'OpenLibrary ne bloque jamais : l'alerte part sans ISBN."""
    client = _FakeCatalogueClient(bnf_empty=True, openlibrary_status=503)
    service = BookMetadataService(http_client=client)

    assert await service.resolve_for_alert("La couleur des choses") is None
    assert await service.verify_isbn("9782369903086", "La couleur des choses") is None


@pytest.mark.asyncio
async def test_openlibrary_lookup_by_isbn_fallback():
    """`/addisbn` et la détection de conflit de `/edit` profitent aussi du repli."""
    service = BookMetadataService(http_client=_FakeCatalogueClient(bnf_empty=True))

    meta = await service.lookup_by_isbn("9782369903086")
    assert meta is not None
    assert meta.title == "La couleur des choses"

    # Un code inconnu des deux côtés ne remonte rien
    inconnu = BookMetadataService(
        http_client=_FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[])
    )
    assert await inconnu.lookup_by_isbn("9780000000002") is None


@pytest.mark.asyncio
async def test_openlibrary_returns_the_requested_isbn_even_in_english():
    """Quand l'utilisateur saisit un ISBN, c'est lui qui fait foi, pas la préférence FR."""
    client = _FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[OPENLIBRARY_HOBBIT_DOC])
    service = BookMetadataService(http_client=client)

    verified = await service.verify_isbn("9780261102217", "The Hobbit")
    assert verified is not None
    assert verified.isbn13 == "9780261102217"
    assert verified.title == "The Hobbit"


def test_clean_openlibrary_author_strips_role():
    """Les noms d'auteur OpenLibrary portent parfois une mention de rôle."""
    assert clean_openlibrary_author("Martin Panchaud (Auteur du texte)") == "Martin Panchaud"
    assert clean_openlibrary_author("J. R. R. Tolkien") == "J. R. R. Tolkien"


# --- Cas réel : « Les Enfants perdus », homonymie BNF -----------------------------
# Le titre d'une BD de 2025 est aussi celui d'un roman de 1989. La BNF ne connaît
# que le roman : le prendre pour l'œuvre demandée affichait son ISBN, son auteur et
# son éditeur comme s'ils étaient ceux de la BD.
def _homonym_records() -> list[BookMetadata]:
    return [
        BookMetadata(
            title="Les Enfants perdus",
            author="Rosemonde Ahou de Saintange",
            publisher="La Pensée universelle",
            isbn13="9782214080627",
            source="bnf",
            publication_year=1989,
        ),
        BookMetadata(
            title="Les Enfants perdus",
            author="Nef",
            publisher="Éditions du Chêne",
            isbn13="9782810206087",
            source="bnf",
            publication_year=2025,
        ),
    ]


@pytest.mark.asyncio
async def test_resolve_for_alert_exposes_homonyms_instead_of_choosing_silently():
    """Deux œuvres de même titre : aucune n'est confirmée, les deux sont proposées."""
    service = BookMetadataService()
    records = _homonym_records()

    with patch.object(service, "_query_bnf_sru", new=AsyncMock(return_value=records)):
        resolution = await service.resolve_for_alert("Les Enfants perdus")

    assert resolution is not None
    assert resolution.is_ambiguous is True
    assert [work.author for work in resolution.work_choices()] == [
        "Rosemonde Ahou de Saintange",
        "Nef",
    ]
    # Les homonymes ne sont pas des éditions : l'ISBN du roman ne doit jamais
    # apparaître dans les éditions concurrentes de la BD.
    assert [edition.isbn13 for edition in resolution.editions()] == ["9782214080627"]


@pytest.mark.asyncio
async def test_resolve_for_alert_author_hint_designs_the_homonym():
    """« Les Enfants perdus de Nef » désigne la notice de Nef, pas celle du roman."""
    service = BookMetadataService()
    records = _homonym_records()

    with patch.object(service, "_query_bnf_sru", new=AsyncMock(return_value=records)):
        resolution = await service.resolve_for_alert("Les Enfants perdus", "Nef")

    assert resolution is not None
    assert resolution.best.author == "Nef"
    assert resolution.best.isbn13 == "9782810206087"
    assert resolution.author_hint_match == "matched"
    # L'indice d'auteur tranche : le roman n'est plus un homonyme à arbitrer.
    assert resolution.is_ambiguous is False


@pytest.mark.asyncio
async def test_resolve_for_alert_author_hint_contradicted_by_the_only_record():
    """Un indice d'auteur que la seule notice disponible contredit : rien n'est confirmé."""
    service = BookMetadataService()
    records = _homonym_records()[:1]

    with patch.object(service, "_query_bnf_sru", new=AsyncMock(return_value=records)):
        resolution = await service.resolve_for_alert("Les Enfants perdus", "Nef")

    assert resolution is not None
    assert resolution.best.author == "Rosemonde Ahou de Saintange"
    assert resolution.author_hint_match == "conflicts"
    assert resolution.is_ambiguous is True


@pytest.mark.asyncio
async def test_resolve_for_alert_does_not_call_a_lookalike_a_homonym():
    """Un titre voisin n'est pas un homonyme : il ne doit pas déclencher d'avertissement."""
    service = BookMetadataService()
    records = _homonym_records()

    with patch.object(service, "_query_bnf_sru", new=AsyncMock(return_value=records)):
        resolution = await service.resolve_for_alert("Les enfants perdus tome 2")

    assert resolution is not None
    assert resolution.other_works == []
    assert resolution.is_ambiguous is False


@pytest.mark.asyncio
async def test_resolve_for_alert_keeps_same_work_editions_out_of_homonyms():
    """Deux éditions d'un même auteur ne sont jamais des œuvres homonymes."""
    service = BookMetadataService()

    resolution = await service.resolve_for_alert("La couleur des choses", "Martin Panchaud")

    assert resolution is not None
    assert resolution.other_works == []
    assert resolution.is_ambiguous is False


@pytest.mark.asyncio
async def test_confirm_ne_confirme_pas_une_panne_meme_si_le_repli_repond():
    """Une BNF muette et un OpenLibrary bavard ne font pas une confirmation.

    `confirm_isbn_for_title` prenait la notice de repli sans regarder la panne : le
    brouillon de création se réalignait sur elle, auteur et éditeur compris, comme si la
    BNF avait validé l'ISBN.
    """
    client = _FakeCatalogueClient(bnf_panne_sur="bib.isbn", openlibrary_docs=[OPENLIBRARY_DOC])
    service = BookMetadataService(http_client=client)

    verdict = await service.confirm_isbn_for_title("9782369903086", "La couleur des choses")

    assert verdict.confirme is False, "une notice de repli ne vaut pas confirmation"
    assert verdict.a_tranche is False, "et ne vaut pas davantage une accusation"
    assert verdict.panne is not None
    assert "TimeoutError" in verdict.panne


@pytest.mark.asyncio
async def test_confirm_accepte_le_repli_quand_la_bnf_a_repondu():
    """Le contraste exact : ici la BNF a répondu, le repli reste un repli."""
    client = _FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[OPENLIBRARY_DOC])
    service = BookMetadataService(http_client=client)

    verdict = await service.confirm_isbn_for_title("9782369903086", "La couleur des choses")

    assert verdict.confirme is True
    assert verdict.autorite == AUTORITE_OPENLIBRARY
    assert verdict.panne is None


@pytest.mark.asyncio
async def test_verify_isbn_ne_valide_pas_un_repli_obtenu_pendant_une_panne():
    """`verify_isbn` est cette fonction-là : elle ne doit rien confirmer de plus."""
    client = _FakeCatalogueClient(bnf_panne_sur="bib.isbn", openlibrary_docs=[OPENLIBRARY_DOC])
    service = BookMetadataService(http_client=client)

    assert await service.verify_isbn("9782369903086", "La couleur des choses") is None


@pytest.mark.asyncio
async def test_confirm_isbn_for_title_separates_conflict_from_absence():
    """« Confirmé », « rattaché à un autre ouvrage » et « inconnu » sont trois verdicts.

    Les confondre, c'est refuser une saisie valide parce que le réseau a tremblé, ou
    accepter l'ISBN d'un autre livre.
    """
    service = BookMetadataService(http_client=_FakeBnFClient())

    confirmed = await service.confirm_isbn_for_title("9782369903086", "La couleur des choses")
    assert confirmed.confirme is True

    conflict = await service.confirm_isbn_for_title("9782369903086", "Les Enfants perdus")
    assert conflict.a_tranche is True, "un autre ouvrage est une preuve, pas un silence"
    assert conflict.notice is not None
    assert conflict.notice.title == "La couleur des choses"
    assert conflict.notice.author == "Martin Panchaud"

    # Une BNF qui ne connaît pas ce code : personne ne le connaît, personne ne le contredit.
    muet = BookMetadataService(
        http_client=_FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[])
    )
    unknown = await muet.confirm_isbn_for_title("9782810206087", "Les Enfants perdus")
    assert unknown.outcome == "inconnu"

    # Un code mal saisi n'est jamais interrogé : c'est une absence, pas un verdict.
    mal_saisi = await service.confirm_isbn_for_title("97828102060", "X")
    assert mal_saisi.outcome == "inconnu"
    assert mal_saisi.consulted == (), "personne n'a été consulté"


@pytest.mark.asyncio
async def test_resolve_input_returns_a_resolution_for_a_typed_isbn():
    """Une saisie en ISBN nomme un ouvrage : elle n'est pas ambiguë."""
    service = BookMetadataService(http_client=_FakeBnFClient())

    resolution = await service.resolve_input("9782369903086")

    assert resolution is not None
    assert resolution.best.isbn13 == "9782369903086"
    assert resolution.is_ambiguous is False
    # Un ISBN nomme un tome : rien à détecter, et l'alerte garde son code.
    assert resolution.notice_titles == ()


def test_ambiguity_note_only_speaks_when_nothing_is_confirmed():
    """Le récap n'explique une homonymie que lorsqu'elle empêche une fiche."""
    roman_only = BookResolution(
        best=BookMetadata(title="Les Enfants perdus", author="Rosemonde", isbn13="9782214080627")
    )
    assert ambiguity_note(roman_only) is None

    homonyms = BookResolution(
        best=BookMetadata(title="Les Enfants perdus", author="Rosemonde", isbn13="9782214080627"),
        other_works=[
            BookMetadata(title="Les Enfants perdus", author="Nef", isbn13="9782810206087")
        ],
    )
    note = ambiguity_note(homonyms)
    assert note is not None
    assert note == "⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre."

    many = BookResolution(
        best=BookMetadata(title="L'Insensé", author="Manu Larcenet", isbn13="9782406101963"),
        other_works=[
            BookMetadata(title="L'Insensé", author="Nicolas Fébule", isbn13="9782810206087"),
            BookMetadata(title="L'Insensé", author="Morgan Sportes", isbn13="9782246482017"),
        ],
    )
    assert ambiguity_note(many) == "⚠️ « L'Insensé » : 2 autres ouvrages portent ce titre."

    conflict = BookResolution(
        best=BookMetadata(title="Les Enfants perdus", author="Rosemonde", isbn13="9782214080627"),
        author_hint_match="conflicts",
    )
    conflict_note = ambiguity_note(conflict)
    assert conflict_note is not None
    assert "Rosemonde" in conflict_note


class _Client:
    """Un client qui ne tombe en panne que sur une requête précise.

    Reproduit le défaut visé : la sonde `explain` du script d'audit répond, donc
    l'autorité paraît disponible, mais cette notice-là ne remonte rien.
    """

    def __init__(self, contenu: bytes, panne_sur: str) -> None:
        self.contenu = contenu
        self.panne_sur = panne_sur
        self.requetes: list[str] = []

    async def get(self, url: str, **_kwargs) -> object:
        self.requetes.append(url)
        if self.panne_sur in url:
            raise TimeoutError("délai dépassé")
        return type("Reponse", (), {"status_code": 200, "content": self.contenu})()


_SRU_VIDE = b"""<?xml version="1.0"?>
<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:numberOfRecords>0</srw:numberOfRecords>
</srw:searchRetrieveResponse>"""


@pytest.mark.asyncio
async def test_verdict_isbn_inconnu_quand_l_autorite_repond_vide():
    """Zéro notice après un 200 : l'autorité a répondu, et elle ne connaît rien.

    C'est une absence de preuve, qui n'autorise aucune accusation — mais ce n'est pas
    une panne : le rapport doit pouvoir le distinguer.
    """
    client = _Client(_SRU_VIDE, panne_sur="inconnu-de-l-inconnu")
    service = BookMetadataService(http_client=client)

    verdict = await service.consult_authorities("9782369903062")

    assert verdict.outcome == "inconnu"
    assert verdict.panne is None
    assert verdict.consulted == (AUTORITE_BNF, AUTORITE_OPENLIBRARY), "les deux ont été consultées"


@pytest.mark.asyncio
async def test_verdict_notice_manquee_quand_une_seule_requete_echoue():
    """La BNF répond à la sonde mais échoue sur cette notice : panne isolée.

    Sans `panne`, ce cas se lirait « personne ne connaît cet ISBN » — une accusation
    que rien ne prouve, produite par la seule inexplicabilité d'une requête.
    """
    client = _Client(_SRU_VIDE, panne_sur="bib.isbn")
    service = BookMetadataService(http_client=client)

    verdict = await service.consult_authorities("9782369903062")

    assert verdict.notice is None, "aucune notice ne peut être inventée"
    assert verdict.panne is not None
    assert "TimeoutError" in verdict.panne, "le motif de panne doit être transmis"
    assert verdict.outcome == "silence"


@pytest.mark.asyncio
async def test_verdict_conserve_la_panne_meme_si_le_repli_repond():
    """Panne BNF puis OpenLibrary : la notice n'est pas un verdict, la panne l'accompagne.

    Sans `panne`, ce cas était indiscernable d'une BNF qui répond « je ne connais pas
    cet ISBN » : deux verdicts strictement identiques, dont le rapport tirait la même
    conclusion — un repli légitime, alors que personne n'avait tranché.
    """
    client = _FakeCatalogueClient(bnf_panne_sur="bib.isbn", openlibrary_docs=[OPENLIBRARY_DOC])
    service = BookMetadataService(http_client=client)

    verdict = await service.consult_authorities("9782369903086")

    assert verdict.notice is not None, "le repli existe, il ne tranche pas"
    assert verdict.notice.source == AUTORITE_OPENLIBRARY
    assert verdict.panne is not None, "la panne ne doit pas disparaître derrière le repli"
    assert "TimeoutError" in verdict.panne
    assert verdict.a_tranche is False, "un repli obtenu pendant une panne n'a rien tranché"


@pytest.mark.asyncio
async def test_verdict_repli_sans_panne_quand_la_bnf_a_repondu():
    """Le contraste exact : ici la BNF a répondu, le repli est un repli."""
    client = _FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[OPENLIBRARY_DOC])
    service = BookMetadataService(http_client=client)

    verdict = await service.consult_authorities("9782369903086")

    assert verdict.confirme is True
    assert verdict.notice is not None
    assert verdict.notice.source == AUTORITE_OPENLIBRARY
    assert verdict.panne is None


@pytest.mark.asyncio
async def test_lookup_by_isbn_rend_la_notice_meme_apres_une_panne():
    """La façade ne bouge pas : `/add` et `/edit` continuent de voir le repli."""
    service = BookMetadataService(
        http_client=_FakeCatalogueClient(
            bnf_panne_sur="bib.isbn", openlibrary_docs=[OPENLIBRARY_DOC]
        )
    )

    meta = await service.lookup_by_isbn("9782369903086")

    assert meta is not None
    assert meta.source == "openlibrary"


@pytest.mark.asyncio
async def test_lookup_by_isbn_ne_change_pas_pour_ses_appelants():
    """Les appelants existants voient la même chose avant et après : `None` ou la notice.

    `/add` et `/edit` ne doivent jamais transformer une panne en refus de saisie.
    """
    en_panne = BookMetadataService(http_client=_Client(_SRU_VIDE, panne_sur="bib.isbn"))
    assert await en_panne.lookup_by_isbn("9782369903062") is None

    sans_notice = BookMetadataService(http_client=_Client(_SRU_VIDE, panne_sur="jamais"))
    assert await sans_notice.lookup_by_isbn("9782369903062") is None


@pytest.mark.parametrize(
    ("issue", "champs", "refus"),
    [
        pytest.param("confirme", {}, "la notice est requise", id="confirme-sans-notice"),
        pytest.param("contradiction", {}, "la notice est requise", id="contradiction-sans-notice"),
        pytest.param(
            # Le cas que trois champs facultatifs rendaient lisible : une notice rendue
            # et une panne dans le même objet, lisible comme une confirmation.
            "confirme",
            {"notice": BookMetadata(title="X"), "panne": "délai dépassé"},
            "ce n'est pas un silence",
            id="confirme-avec-panne",
        ),
        pytest.param("silence", {}, "motif de l'absence", id="silence-sans-motif"),
        pytest.param(
            "inconnu", {"notice": BookMetadata(title="X")}, "il n'y a rien à rendre",
            id="inconnu-avec-notice",
        ),
        pytest.param(
            "inconnu", {"panne": "délai dépassé"}, "il n'y a rien à rendre",
            id="inconnu-avec-panne",
        ),
    ],
)
def test_le_verdict_refuse_un_etat_contradictoire(issue, champs, refus):
    """Trois champs facultatifs admettaient « l'autorité n'a pas répondu » + une autorité.

    C'est le défaut affiché : « REPLI … aucune autorité a répondu » au-dessus de
    « conforme aux notices ». Un seul champ d'issue rend la contradiction impossible à
    écrire, au lieu d'en multiplier les lectures possibles.
    """
    with pytest.raises(ValueError, match=refus):
        IsbnVerdict(outcome=issue, **champs)


@pytest.mark.asyncio
async def test_les_autorites_consultees_suivent_les_appels_effectifs():
    """La liste vient du service, pas d'une constante : un troisième repli s'y inscrira.

    Une constante recopiée dans un appelant ne peut que dire « ni la BNF ni
    OpenLibrary ». Ici, elle est écrite au moment où les appels sont faits, dans
    l'ordre où ils ont lieu.
    """
    client = _FakeCatalogueClient()
    trouve = await BookMetadataService(http_client=client).consult_authorities("9782369903086")

    assert trouve.consulted == (AUTORITE_BNF,)
    assert client.openlibrary_queries == [], "le repli n'a pas eu lieu : il n'est pas nommé"

    repli_client = _FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[OPENLIBRARY_DOC])
    repli = await BookMetadataService(http_client=repli_client).consult_authorities(
        "9782369903086"
    )

    assert repli.consulted == (AUTORITE_BNF, AUTORITE_OPENLIBRARY)
    assert len(repli_client.openlibrary_queries) == 1, "l'ordre des appels est celui de la liste"


@pytest.mark.asyncio
async def test_verdict_refuse_un_isbn_malforme_sans_appeler_personne():
    """Frontière : un code invalide ne se confond pas non plus avec une panne."""
    service = BookMetadataService(http_client=_Client(_SRU_VIDE, panne_sur="jamais"))

    verdict = await service.consult_authorities("pas-un-isbn")

    assert verdict.outcome == "inconnu"
    assert verdict.panne is None
    assert service.http_client.requetes == [], "aucun appel réseau pour une saisie invalide"


# --- Le garde-fou « fail-closed » de la réponse SRU ------------------------------
#
# Le `XMLParser` de la C de CPython ne prend que `target` et `encoding` :
# `resolve_entities` comme `forbid_entities` y lèvent un TypeError. Ce qu'il
# n'expose pas non plus, c'est une poignée pour dire au parseur « n'accepte
# aucune entité ». Ce qui reste possible est donc une *déclaration* d'entité
# dans le sous-ensemble interne du document — et expat l'honore : sans le garde,
# un `&isbn;` déclaré par la réponse devient un ISBN que la BNF aurait « confirmé ».
# Le garde refuse donc le document entier, et le dit : un refus sans motif se
# lirait comme une absence de notice.


def _sru(record: str, doctype: str = "") -> bytes:
    """Enveloppe SRU d'un seul record, avec une DOCTYPE facultative.

    Volontairement distincte de `SRU_HEADER` : une DOCTYPE doit se placer entre
    la déclaration XML et l'élément racine, donc après le `?>`.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"{doctype}"
        '<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">'
        f"<srw:numberOfRecords>1</srw:numberOfRecords><srw:records>{record}"
        "</srw:records></srw:searchRetrieveResponse>"
    ).encode()


class _ClientSRUFige:
    """Le SRU renvoie toujours le même octet ; le repli répond « aucun ouvrage »."""

    def __init__(self, contenu: bytes) -> None:
        self.contenu = contenu
        self.requetes: list[str] = []

    async def get(self, url: str, **_kwargs: object) -> _FakeResponse:
        self.requetes.append(url)
        if "openlibrary.org" in url:
            return _FakeResponse(payload={"numFound": 0, "docs": []})
        return _FakeResponse(content=self.contenu)


_PANNE_DOCTYPE = "DOCTYPE déclaré dans la réponse"


@pytest.mark.asyncio
async def test_reponse_sru_sans_doctype_est_toujours_lue():
    """Le garde écarte un document qui déclare une DOCTYPE, et rien d'autre.

    Un garde qui refuse tout resterait invisible sur le cas précédent : sans cette
    lecture, une réponse parfaitement normale finirait écartée en silence.
    """
    service = BookMetadataService(
        http_client=_ClientSRUFige(_sru(PANCHAUD_EDITION_2022))
    )

    notices, panne = await service._bnf_records("bib.isbn adj \"9782369903086\"")

    assert [notice.isbn13 for notice in notices] == ["9782369903086"]
    assert panne is None, "une réponse lisible n'est pas une panne"


@pytest.mark.asyncio
async def test_entite_interne_declenchee_est_ecartee_et_annoncee_comme_panne():
    """Une entité interne déclarée par la réponse n'est jamais développée.

    C'est le cas qui tuerait la garde : expat développe ce `&isbn;` sans rien
    demander, et la notice qui en sort porte un ISBN que personne n'a lu dans la
    réponse. Le garde rend une liste vide *et* le motif, parce qu'une liste vide
    sans motif dirait « la BNF ignore cet ISBN » — une accusation.
    """
    charge = _sru(
        PANCHAUD_EDITION_2022.replace("9782369903086", "&isbn;"),
        doctype=(
            '<!DOCTYPE srw:searchRetrieveResponse ['
            '<!ENTITY isbn "9782369903086">'
            "]>"
        ),
    )
    service = BookMetadataService(http_client=_ClientSRUFige(charge))

    notices, panne = await service._bnf_records("bib.isbn adj \"9782369903086\"")

    assert notices == [], "aucune notice ne sort d'un document qui déclare ses entités"
    assert panne == _PANNE_DOCTYPE
    assert ET.fromstring(charge) is not None, "le parseur nu, lui, aurait développé &isbn;"


@pytest.mark.asyncio
async def test_entite_externe_ne_lit_pas_le_fichier_local_et_le_signale(tmp_path):
    """Un `SYSTEM file://…` ne fait entrer aucun octet du disque, et se voit.

    Le parseur de la C refuse déjà l'entité externe (`undefined entity`), mais par
    accident d'implémentation, pas par contrat : le jour où un vrai durcissement
    (un parseur dédié, une liste blanche) le remplace, c'est cette assertion qui tient la
    ligne. Le motif est exigé ici parce qu'un `ParseError` brut, lui, ne dit rien
 de la décision — il dirait seulement que quelque chose a échoué.
    """
    contenu_local = "9782369903062 CONTENU LOCAL QUI NE DOIT PAS FUIR"
    fichier = tmp_path / "secret.txt"
    fichier.write_text(contenu_local, encoding="utf-8")
    charge = _sru(
        PANCHAUD_EDITION_2022.replace("9782369903086", "&fuite;"),
        doctype=(
            "<!DOCTYPE srw:searchRetrieveResponse ["
            f'<!ENTITY fuite SYSTEM "{fichier.as_uri()}">'
            "]>"
        ),
    )
    service = BookMetadataService(http_client=_ClientSRUFige(charge))

    notices, panne = await service._bnf_records("bib.isbn adj \"9782369903086\"")

    assert notices == []
    assert panne == _PANNE_DOCTYPE
    assert contenu_local not in (panne or "")
    assert all(contenu_local not in (notice.isbn13 or "") for notice in notices)


@pytest.mark.asyncio
async def test_une_doctype_ne_peut_pas_confirmer_un_isbn():
    """Ce que l'appelant public rend : un silence motivé, jamais une confirmation.

    Le garde protège le parseur ; il doit aussi protéger l'interface. Un verdict
    `confirme` fondé sur un ISBN développé par la réponse ferait figer une
    identité d'ouvrage pour l'éternité d'une alerte, et `inconnu` accuserait
    l'autorité de l'ignorer alors qu'elle n'a pas été lue.
    """
    charge = _sru(
        PANCHAUD_EDITION_2022.replace("9782369903086", "&isbn;"),
        doctype=(
            '<!DOCTYPE srw:searchRetrieveResponse ['
            '<!ENTITY isbn "9782369903086">'
            "]>"
        ),
    )
    service = BookMetadataService(http_client=_ClientSRUFige(charge))

    verdict = await service.consult_authorities("9782369903086")

    assert verdict.notice is None
    assert verdict.outcome == "silence", "une réponse écartée n'est pas une absence de notice"
    assert verdict.panne == _PANNE_DOCTYPE
    assert verdict.consulted == (AUTORITE_BNF, AUTORITE_OPENLIBRARY), "le repli a bien été tenté"


# --- Le faux positif que le garde par octets ne pouvait pas voir -----------------
#
# `b"<!DOCTYPE" in resp.content` cherchait le littéral dans les octets. Or le
# littéral peut apparaître dans le *texte* d'une notice — via un CDATA, seul
# endroit XML où `<` n'a pas besoin d'être échappé. La réponse était alors
# écartée comme une panne alors qu'elle était parfaitement lisible : une
# recherche d'octets ne voit pas la structure du document.


@pytest.mark.asyncio
async def test_un_cdata_qui_mentionne_la_doctype_est_lu():
    """Une notice qui *parle* de `<!DOCTYPE html>` n'est pas une notice écartée.

    C'est le faux positif assumé du garde précédent : il annonçait une panne BNF
    alors que la réponse portait la notice attendue.
    """
    record = BNF_RECORD_TEMPLATE.format(
        ark="471036825",
        title="La couleur des choses <![CDATA[référence <!DOCTYPE html> en annexe]]>",
        creator="Panchaud, Martin (1982-....). Auteur du texte",
        publisher="Çà et là (Bussy-Saint-Georges)",
        date="<dc:date>2022</dc:date>",
        fmt="1 vol. (224 p.) : ill. en coul. ; 24 cm",
        isbn="9782369903086",
    )
    service = BookMetadataService(http_client=_ClientSRUFige(_sru(record)))

    notices, panne = await service._bnf_records('bib.isbn adj "9782369903086"')

    assert panne is None, "un CDATA est du texte, pas une déclaration"
    assert len(notices) == 1
    assert "DOCTYPE" in (notices[0].title or ""), "le texte est bien passé dans la notice"


@pytest.mark.asyncio
async def test_un_texte_echappe_mentionnant_la_doctype_est_lu():
    """La forme échappée `&lt;!DOCTYPE…` ne déclenche rien non plus."""
    record = BNF_RECORD_TEMPLATE.format(
        ark="471036825",
        title="La couleur des choses &lt;!DOCTYPE html&gt;",
        creator="Panchaud, Martin (1982-....). Auteur du texte",
        publisher="Çà et là (Bussy-Saint-Georges)",
        date="<dc:date>2022</dc:date>",
        fmt="1 vol. (224 p.) : ill. en coul. ; 24 cm",
        isbn="9782369903086",
    )
    service = BookMetadataService(http_client=_ClientSRUFige(_sru(record)))

    notices, panne = await service._bnf_records('bib.isbn adj "9782369903086"')

    assert panne is None
    assert len(notices) == 1
    assert "<!DOCTYPE" in (notices[0].title or ""), "l'échappement redevient du texte"


def test_la_garde_reconnait_la_declaration_et_son_menson():
    """La garde est structurelle : elle distingue déclarer de mentionner."""
    declaree = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY isbn "9782369903086">]>'
        b"<r>&isbn;</r>"
    )
    externe = b'<?xml version="1.0"?><!DOCTYPE r SYSTEM "file:///etc/passwd"><r/>'
    cdata = b'<?xml version="1.0"?><r><![CDATA[<!DOCTYPE html>]]></r>'
    echappe = b'<?xml version="1.0"?><r>&lt;!DOCTYPE html&gt;</r>'
    ordinaire = b'<?xml version="1.0"?><r><t>rien de special</t></r>'
    malforme = b'<?xml version="1.0"?><r><t>jamais ferme'

    declaree_utf16 = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE r [<!ENTITY isbn "9782369903086">]><r>&isbn;</r>'
    ).encode("utf-16")

    assert _declares_doctype(declaree), "une déclaration avec entité interne est refusée"
    assert _declares_doctype(externe), "une entité externe est refusée"
    # Le garde par octets laissait passer celle-ci : en UTF-16, les octets
    # `<!DOCTYPE` ne sont pas là. C'était un contournement, pas un oubli.
    assert b"<!DOCTYPE" not in declaree_utf16, "le cas ne vaut rien s'il contient les octets"
    assert _declares_doctype(declaree_utf16), "une entité interne en UTF-16 est refusée"
    assert not _declares_doctype(cdata), "un CDATA n'est pas une déclaration"
    assert not _declares_doctype(echappe), "du texte échappé n'est pas une déclaration"
    assert not _declares_doctype(ordinaire), "un document ordinaire ne déclare rien"
    assert not _declares_doctype(malforme), "un document malformé n'est pas une déclaration"
    assert not _declares_doctype(b""), "une charge vide ne déclare rien"
    assert not _declares_doctype(b"   \n "), "des blancs ne déclarent rien"


# --- Identifiant d'autorité : ce que la notice porte vraiment --------------------
_SRU_EAN_NU = b"""<?xml version="1.0" encoding="UTF-8"?>
<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
 <srw:numberOfRecords>1</srw:numberOfRecords><srw:records>
<srw:record><srw:recordSchema>dc</srw:recordSchema><srw:recordData>
<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier>http://catalogue.bnf.fr/ark:/12148/cb471036825</dc:identifier>
  <dc:title>Dailyeux : 150 recettes pour se regaler tous les jours</dc:title>
  <dc:creator>Whoogy's</dc:creator>
  <dc:publisher>Le Chen</dc:publisher>
  <dc:date>2026</dc:date>
  <dc:format>1 vol. (272 p.) : ill. en coul. ; 26 cm</dc:format>
  <dc:identifier>9782812322495</dc:identifier>
</oai_dc:dc>
</srw:recordData></srw:record>
</srw:records></srw:searchRetrieveResponse>"""


@pytest.mark.asyncio
async def test_notice_carrying_only_a_bare_ean_is_kept():
    """Une notice dont l'identifiant est un EAN nu reste une notice de livre.

    Elle disparaissait avant, sans un mot : l'identifiant ne portait pas le mot
    « ISBN », et tout ce que la notice disait de l'ouvrage partait avec elle.
    """
    service = BookMetadataService(http_client=_ClientSRUFige(_SRU_EAN_NU))

    records, panne = await service._bnf_records('bib.anywhere all "Dailyeux"')

    assert panne is None
    assert len(records) == 1
    assert records[0].isbn13 == "9782812322495"
    assert records[0].title.startswith("Dailyeux")


@pytest.mark.asyncio
async def test_bnf_never_confirms_an_isbn_and_openlibrary_does():
    """Limite structurelle, consignée pour qu'on ne la « corrige » pas à l'aveugle.

    Le SRU de la BNF ne sait pas être interrogé par ISBN : `bib.isbn adj` comme
    `bib.anywhere all` renvoient zéro notice, même pour un ISBN que la BNF possède
    par ailleurs (vérifié sur `9782864971412`, extrait d'une notice réellement
    obtenue). Un ISBN soumis au projet est donc toujours tranché par OpenLibrary,
    et c'est dit comme tel au récap. Un jour où la BNF expose un point d'entrée par
    ISBN, ce test est celui qui doit changer.
    """
    client = _FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[OPENLIBRARY_DOC])
    service = BookMetadataService(http_client=client)

    verdict = await service.consult_authorities("9782369903086")

    assert verdict.confirme is True
    assert verdict.consulted == (AUTORITE_BNF, AUTORITE_OPENLIBRARY)
    assert verdict.notice is not None
    assert verdict.notice.source == AUTORITE_OPENLIBRARY
    assert provenance_note(verdict.notice) == OPENLIBRARY_SOURCE_NOTE

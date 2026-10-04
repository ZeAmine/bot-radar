"""Tests unitaires pour le service BookMetadataService (parsing, BNF SRU, recommandations)."""

import urllib.parse
from unittest.mock import AsyncMock, patch

import pytest

from app.core.constants import MatchConfidence
from app.services.book_metadata_service import (
    BookMetadata,
    BookMetadataService,
    BookResolution,
    ambiguity_note,
    clean_bnf_creator,
    clean_bnf_publisher,
    clean_bnf_title,
    clean_openlibrary_author,
    generate_recommended_keywords,
    parse_query_title_and_author,
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
    ) -> None:
        self.queries: list[str] = []
        self.bnf_empty = bnf_empty
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
async def test_confirm_isbn_for_title_separates_conflict_from_absence():
    """« Confirmé », « rattaché à un autre ouvrage » et « inconnu » sont trois verdicts.

    Les confondre, c'est refuser une saisie valide parce que le réseau a tremblé, ou
    accepter l'ISBN d'un autre livre.
    """
    service = BookMetadataService(http_client=_FakeBnFClient())

    confirmed = await service.confirm_isbn_for_title("9782369903086", "La couleur des choses")
    assert confirmed.meta is not None
    assert confirmed.conflicting_title is None

    conflict = await service.confirm_isbn_for_title("9782369903086", "Les Enfants perdus")
    assert conflict.meta is None
    assert conflict.conflicting_title == "La couleur des choses"
    assert conflict.conflicting_author == "Martin Panchaud"

    # Une BNF muette sur un code récent : personne ne le connaît, personne ne le contredit.
    silent = BookMetadataService(
        http_client=_FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[])
    )
    unknown = await silent.confirm_isbn_for_title("9782810206087", "Les Enfants perdus")
    assert unknown.meta is None
    assert unknown.conflicting_title is None

    # Un code mal saisi n'est jamais interrogé : c'est une absence, pas un verdict.
    assert (await service.confirm_isbn_for_title("97828102060", "X")).meta is None


@pytest.mark.asyncio
async def test_resolve_input_returns_a_resolution_for_a_typed_isbn():
    """Une saisie en ISBN nomme un ouvrage : elle n'est pas ambiguë."""
    service = BookMetadataService(http_client=_FakeBnFClient())

    resolution = await service.resolve_input("9782369903086")

    assert resolution is not None
    assert resolution.best.isbn13 == "9782369903086"
    assert resolution.is_ambiguous is False


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
    assert "aussi le titre d'une autre œuvre" in note
    assert "surveillance par titre" in note

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

    verdict = await service.lookup_by_isbn_verdict("9782369903062")

    assert verdict.notice is None
    assert verdict.panne is None
    assert verdict.a_repondu is True


@pytest.mark.asyncio
async def test_verdict_notice_manquee_quand_une_seule_requete_echoue():
    """La BNF répond à la sonde mais échoue sur cette notice : panne isolée.

    Sans `panne`, ce cas se lirait « personne ne connaît cet ISBN » — une accusation
    que rien ne prouve, produite par la seule inexplicabilité d'une requête.
    """
    client = _Client(_SRU_VIDE, panne_sur="bib.isbn")
    service = BookMetadataService(http_client=client)

    verdict = await service.lookup_by_isbn_verdict("9782369903062")

    assert verdict.notice is None, "aucune notice ne peut être inventée"
    assert verdict.panne is not None
    assert "TimeoutError" in verdict.panne, "le motif de panne doit être transmis"
    assert verdict.a_repondu is False


@pytest.mark.asyncio
async def test_lookup_by_isbn_ne_change_pas_pour_ses_appelants():
    """Les appelants existants voient la même chose avant et après : `None` ou la notice.

    `/add` et `/edit` ne doivent jamais transformer une panne en refus de saisie.
    """
    en_panne = BookMetadataService(http_client=_Client(_SRU_VIDE, panne_sur="bib.isbn"))
    assert await en_panne.lookup_by_isbn("9782369903062") is None

    sans_notice = BookMetadataService(http_client=_Client(_SRU_VIDE, panne_sur="jamais"))
    assert await sans_notice.lookup_by_isbn("9782369903062") is None


@pytest.mark.asyncio
async def test_verdict_refuse_un_isbn_malforme_sans_appeler_personne():
    """Frontière : un code invalide ne se confond pas non plus avec une panne."""
    service = BookMetadataService(http_client=_Client(_SRU_VIDE, panne_sur="jamais"))

    verdict = await service.lookup_by_isbn_verdict("pas-un-isbn")

    assert verdict.notice is None
    assert verdict.panne is None
    assert service.http_client.requetes == [], "aucun appel réseau pour une saisie invalide"

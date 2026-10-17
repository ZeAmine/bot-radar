"""Notices de catalogue simulées, et le faux client qui les sert.

Le SRU de la BNF, l'API Search d'OpenLibrary et l'API Volumes de Google Books sont
émulés une seule fois ici : plusieurs fichiers de test traversent la vraie résolution
d'ouvrage, et deux copies de ces notices finiraient par répondre autre chose. Les
notices sont des données de test, pas des assertions — le fichier suit `fakes_llm.py`,
déjà partagé de la même façon.

Il ne collecte rien (`python_files = ["test_*.py"]`) et ne doit rien savoir des tests
qui l'utilisent.
"""

from __future__ import annotations

import urllib.parse

# Deux éditions BNF du même titre, et un ISBN réel attribué à un autre ouvrage
# du même éditeur (« Keeping two ») — l'erreur typique d'un LLM.
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

# Réponse BNF réelle pour « Manuel du cuisinier amateur » (Whoogy's) : la notice de
# référence, sans ISBN, est écartée ; les tomes se distinguent par le numéro après « : ».
BNF_MANUEL_TOMES = [
    BNF_RECORD_TEMPLATE.format(
        ark="471752286",
        title=(
            "Manuel du cuisinier amateur : 150 recettes et techniques. Tome 1"
            " / par Whoogy's ; photographies de Maurine Toussaint"
        ),
        creator="Whoogy's. Auteur du texte",
        publisher="Chêne (Vanves)",
        date="<dc:date>2022</dc:date>",
        fmt="1 vol. (258 p.) : ill. en coul. ; 27 cm",
        isbn="9782812321344",
    ),
    BNF_RECORD_TEMPLATE.format(
        ark="47567143s",
        title=(
            "Manuel du cuisinier amateur : 150 recettes et techniques. Tome 2"
            " / par Whoogy's ; photographies de Maurine Toussaint"
        ),
        creator="Whoogy's. Auteur du texte",
        publisher="Chêne (Vanves)",
        date="<dc:date>2024</dc:date>",
        fmt="1 vol. (256 p.) : ill. en coul. ; 27 cm",
        isbn="9782812321689",
    ),
]


# Un document OpenLibrary regroupe toutes les éditions de l'œuvre, dont une
# traduction anglaise (9780261102217) à ne pas retenir.
OPENLIBRARY_DOC = {
    "title": "La couleur des choses",
    "author_name": ["Martin Panchaud (Auteur du texte)"],
    "publisher": ["Ça et là"],
    "first_publish_year": 2022,
    "number_of_pages_median": 236,
    "language": ["fre"],
    "isbn": ["9782369903086", "2369903082", "9782072909207", "9780261102217"],
}
GOOGLE_BOOKS_ITEM = {
    "volumeInfo": {
        "title": "Dailycieux",
        "authors": ["Whoogy's"],
        "publisher": "Éditions du Chêne",
        "publishedDate": "2026-09-23",
        "pageCount": 240,
        "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9782812322495"}],
    }
}

GOOGLE_BOOKS_ITEM_AUTRE = {
    "volumeInfo": {
        "title": "Dailycieux",
        "authors": ["Whoogy's"],
        "publisher": "Éditions du Chêne",
        "publishedDate": "2026-09-23",
        "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780000000019"}],
    }
}

OPENLIBRARY_HOBBIT_DOC = {
    "title": "The Hobbit",
    "author_name": ["J. R. R. Tolkien"],
    "publisher": ["Allen & Unwin"],
    "first_publish_year": 1937,
    "isbn": ["9780261102217"],
}


class FakeResponse:
    def __init__(self, content: bytes = b"", payload: dict[str, object] | None = None,
                 status_code: int = 200) -> None:
        self.status_code = status_code
        self.content = content
        self._payload = payload or {}

    def json(self) -> dict[str, object]:
        return self._payload


class FakeCatalogueClient:
    """Émule le SRU BNF, l'API Search d'OpenLibrary et l'API Volumes de Google Books."""

    def __init__(
        self,
        bnf_empty: bool = False,
        openlibrary_docs: list[dict[str, object]] | None = None,
        openlibrary_status: int = 200,
        bnf_panne_sur: str | None = None,
        bnf_records: list[str] | None = None,
        google_books_items: list[dict[str, object]] | None = None,
    ) -> None:
        self.queries: list[str] = []
        self.bnf_empty = bnf_empty
        self.bnf_panne_sur = bnf_panne_sur
        self.openlibrary_docs = openlibrary_docs
        self.openlibrary_status = openlibrary_status
        self.bnf_records = BNF_RECORDS if bnf_records is None else bnf_records
        self.google_books_items = google_books_items
        self.google_books_queries: list[str] = []

    @property
    def openlibrary_queries(self) -> list[str]:
        return [url for url in self.queries if "openlibrary.org" in url]

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.queries.append(url)
        params = kwargs.get("params")
        params = params if isinstance(params, dict) else {}

        if "googleapis.com" in url:
            # Google Books reçoit sa requête en paramètre séparé, pas dans l'URL.
            self.google_books_queries.append(str(params.get("q", "")))
            items = self.google_books_items
            if items is None:
                return FakeResponse(payload={"totalItems": 0})
            return FakeResponse(payload={"totalItems": len(items), "items": items})

        if "openlibrary.org" in url:
            if self.openlibrary_status != 200:
                return FakeResponse(status_code=self.openlibrary_status)
            docs = self.openlibrary_docs
            if docs is None:
                params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                requested = params.get("isbn")
                docs = (
                    [{**OPENLIBRARY_DOC, "isbn": requested}]
                    if requested
                    else [OPENLIBRARY_DOC]
                )
            return FakeResponse(payload={"numFound": len(docs), "docs": docs})

        if self.bnf_panne_sur and self.bnf_panne_sur in url:
            raise TimeoutError("délai dépassé")

        if self.bnf_empty:
            return FakeResponse(SRU_HEADER.format(count=0, records="").encode("utf-8"))

        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
        matched = [
            record
            for record in self.bnf_records
            if f'"{record.split("ISBN ")[1].split("<")[0]}"' in query
        ] or self.bnf_records
        payload = SRU_HEADER.format(count=len(matched), records="".join(matched))
        return FakeResponse(payload.encode("utf-8"))


FakeBnFClient = FakeCatalogueClient

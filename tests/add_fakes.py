"""Les doubles qui remplacent une frontière d'un parcours produit, et leurs données.

Deux parcours partagent ce fichier : `/add`, dont les écrans sont édités en place, et
l'ingestion, dont la légende d'annonce part par `Bot.send_message`/`Bot.send_photo`.
Les jouer demande de remplacer tout ce que le produit interroge — le moteur IA, la BNF,
la base, Telegram — sans jamais ouvrir de connexion. Ce fichier porte ces doubles
**une fois** : un test de parcours les enchaîne, un test de copie regarde l'écran qu'ils
font produire. Deux copies donneraient deux chaînes légèrement différentes, donc deux
vérités sur ce que l'utilisateur lit.

Le câblage de ces doubles — quels noms de production sont remplacés, et où — vit dans
`tests/add_journey.py` ; la lecture de ce qu'un écran a publié, dans `tests/screens.py`.

Un module partagé, pas un module de test importé par un autre : ce fichier ne collecte
rien (`python_files = ["test_*.py"]`) et ne doit rien savoir des tests qui l'utilisent.
Il suit `tests/fakes_llm.py`, déjà partagé de la même façon.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import Message
from sqlalchemy.exc import MissingGreenlet

from app.core.constants import ComparisonStatus
from app.db.models.book_market_cache import BookMarketCache
from app.services.book_metadata_service import (
    BookMetadata,
    BookMetadataService,
    BookResolution,
    IsbnVerdict,
)

# Bandeaux de parcours qui ont disparu au profit d'un titre unique : leur présence
# signalerait que la carte redeviendrait dépendante du chemin qui l'a produite.
ROUTE_BANNERS = (
    "Alerte configurée en langage naturel",
    "Modification enregistrée",
    "Aucune modification",
)

# Édition de référence de l'œuvre demandée, confirmée par la BNF.
BNF_EDITION = BookMetadata(
    title="La couleur des choses",
    author="Martin Panchaud",
    publisher="Çà et là",
    isbn13="9782369903086",
    publication_year=2022,
    format_detail="1 vol. (224 p.) : ill. en coul. ; 24 cm",
)
# Autre édition de la même œuvre : c'est celle-là que l'utilisateur va choisir.
BNF_ALTERNATIVE = BookMetadata(
    title="La couleur des choses",
    author="Martin Panchaud",
    publisher="Editions çà et là",
    isbn13="9782369904151",
    format_detail="1 vol. (224 p.) ; 19 cm",
)
# ISBN bien formé et réel, mais qui désigne « Keeping two » de Jordan Crane.
ISBN_AUTRE_LIVRE = "9782369903062"
# ISBN bien formé, absent de la BNF : un ouvrage paru la semaine dernière.
ISBN_INCONNU = "9782810206087"


class DraftState:
    """Fausse FSM : conserve le brouillon comme le vrai contexte en production."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.state = None

    async def clear(self) -> None:
        self.data = {}
        self.state = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, data: dict | None = None, **kwargs) -> None:
        self.data.update(data or {})
        self.data.update(kwargs)


class FakeMessage(MagicMock):
    """Message Telegram reconnu par `isinstance`."""

    __class__ = Message


class UnloadedBook:
    """Livre tel que `BookRepository.create` le renvoie : `market_cache` non chargé.

    Un accès paresseux lève en session async ; reproduire ce comportement est le
    seul moyen de verrouiller le défaut « alerte créée, aucune confirmation ».
    """

    def __init__(self, isbn13: str) -> None:
        self.id = uuid.uuid4()
        self.isbn13 = isbn13

    @property
    def market_cache(self) -> object:
        raise MissingGreenlet("greenlet_spawn has not been called")


def deepseek_payload(**overrides) -> dict:
    """Réponse du moteur IA pour `/add` : elle propose l'ISBN d'un autre livre."""
    payload = {
        "name": "La couleur des choses",
        "category_slug": "bd_comics",
        "isbn13": ISBN_AUTRE_LIVRE,
        "author": "Martin Panchaud",
        "publisher": "Çà et là",
        "include_keywords": ["La couleur des choses"],
        "exclude_keywords": [],
        "max_price_cents": 1200,
        "is_series": False,
    }
    payload.update(overrides)
    return payload


def fake_deepseek(payload: dict) -> MagicMock:
    """Moteur IA simulé : il rend le JSON fourni, sans ouvrir de connexion."""
    client = MagicMock()
    client.is_configured = True
    client.generate_json = AsyncMock(return_value=payload)
    return client


def fake_bnf(
    resolution: BookResolution | None,
    verified_isbn: str | None = None,
    conflicting_isbn: str | None = None,
) -> MagicMock:
    """BNF simulée : elle connaît les notices qu'elle a rendues, et rien d'autre.

    Une autorité qui a nommé une œuvre dans sa résolution la connaît : son ISBN y est
    confirmé. Un ISBN hors de la résolution n'est confirmé que s'il est explicitement
    déclaré (`verified_isbn`), et `conflicting_isbn` reste le cas de la contradiction.
    """
    known: dict[str, BookMetadata] = {}
    if resolution is not None:
        for meta in (resolution.best, *resolution.alternatives, *resolution.other_works):
            if meta.isbn13:
                known.setdefault(meta.isbn13, meta)
    if verified_isbn and BNF_EDITION.isbn13:
        known.setdefault(BNF_EDITION.isbn13, BNF_EDITION)

    def _verdict(isbn: str) -> IsbnVerdict:
        if conflicting_isbn and isbn == conflicting_isbn:
            return IsbnVerdict(
                outcome="contradiction",
                notice=BookMetadata(title="Keeping two", author="Jordan Crane"),
            )
        notice = known.get(isbn)
        if notice is not None:
            return IsbnVerdict(outcome="confirme", notice=notice)
        return IsbnVerdict(outcome="inconnu")

    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(return_value=resolution)
    bnf.verify_isbn = AsyncMock(
        return_value=known.get(verified_isbn) if verified_isbn else None
    )
    bnf.confirm_isbn_for_title = AsyncMock(side_effect=lambda isbn, _title=None: _verdict(isbn))
    bnf.lookup_by_isbn = AsyncMock(return_value=BNF_EDITION)
    return bnf


def fake_book_search(
    bnf: MagicMock,
) -> MagicMock:
    """Cas d'usage simulé : mappe les verdicts du faux service via les vrais adaptateurs.

    Telegram appelle `BookSearchUseCase` : le simuler au niveau du service laisserait
    passer le mapping (statuts, `confirmed`, T9, série). Ici, les adaptateurs réels
    tournent, donc une résolution de série produit bien `is_series=True`.
    """
    from app.services.book_metadata_service import (
        AUTORITE_BNF,
        BibliographicReport,
        IsbnVerdict,
    )
    from app.use_cases.book_search import result_from_report, result_from_verdict_report

    class _UseCase:
        async def search_by_title(self, title: str, author_hint: str | None = None):
            resolution = await bnf.resolve_for_alert(title, author_hint)
            return result_from_report(
                BibliographicReport(kind="title", resolution=resolution),
                query_title=title,
            )

        async def search_by_isbn(self, isbn: str, expected_title: str | None = None):
            verdict = await bnf.confirm_isbn_for_title(isbn, expected_title)
            if not isinstance(verdict, IsbnVerdict):
                # Service simulé d'un test écrit pour l'ancien joint (`verify_isbn`) :
                # on dérive le verdict de sa réponse, sans rien changer au code réel.
                notice = await bnf.verify_isbn(isbn, expected_title or "")
                verdict = (
                    IsbnVerdict(
                        outcome="confirme", notice=notice, consulted=(AUTORITE_BNF,)
                    )
                    if notice is not None
                    else IsbnVerdict(outcome="inconnu", consulted=(AUTORITE_BNF,))
                )
            return result_from_verdict_report(
                BibliographicReport(kind="isbn", verdict=verdict), expected_title
            )

    return MagicMock(return_value=_UseCase())


def cached_market_price() -> BookMarketCache:
    """Cote déjà en base pour un ouvrage du catalogue : prix du jour et plus bas 1 an."""
    return BookMarketCache(
        id=uuid.uuid4(),
        book_id=uuid.uuid4(),
        lowest_delivered_price_cents=1800,
        best_provider_name="Momox",
        comparison_status=ComparisonStatus.AVAILABLE,
        historical_low_1y_cents=1650,
    )


class FakeTelegramBot:
    """Bot Telegram de test : il retient ce qui a été remis, sans ouvrir de réseau.

    `Bot.send_message` et `Bot.send_photo` sont la seule sortie du produit vers
    l'utilisateur : les remplacer laisse la légende se construire pour de vrai, par
    `build_listing_caption`, et rend lisible **le message** que l'alerte a produit —
    pas un rendu appelé à la main.
    """

    def __init__(self) -> None:
        self.sent_to_telegram: list[dict] = []
        self.send_message = AsyncMock(side_effect=self._capture)
        self.send_photo = AsyncMock(side_effect=self._capture)

    async def _capture(self, **kwargs) -> MagicMock:
        self.sent_to_telegram.append(kwargs)
        return MagicMock(message_id=len(self.sent_to_telegram))

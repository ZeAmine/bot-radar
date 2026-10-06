"""Harnais de parcours `/add` : les fausses frontières, partagées par les tests.

Jouer un parcours de création d'alerte de bout en bout demande de simuler tout ce que
le produit interroge — le moteur IA, la BNF, l'agrégateur de marché, les repositories —
et de piloter ce que le handler croit faire en arrière-plan. Ce fichier porte ces
simulations **une fois** : un test de parcours les enchaîne, un test de copie y branche
les mêmes handlers pour regarder l'écran. Deux copies de ces fausses frontières
donneraient deux chaînes légèrement différentes, donc deux vérités sur ce que
l'utilisateur lit.

Un module partagé, pas un module de test importé par un autre : ce fichier ne collecte
rien (`python_files = ["test_*.py"]`) et ne doit rien savoir des tests qui l'utilisent.
Il suit `tests/fakes_llm.py`, déjà partagé de la même façon.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from aiogram.types import Message
from sqlalchemy.exc import MissingGreenlet

from app.core.constants import ComparisonStatus
from app.db.models.book_market_cache import BookMarketCache
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
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
    """BNF simulée : seul l'ISBN explicitement déclaré reçoit un verdict."""
    confirmed = verified_isbn == BNF_EDITION.isbn13
    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(return_value=resolution)
    bnf.verify_isbn = AsyncMock(return_value=BNF_EDITION if confirmed else None)
    if conflicting_isbn:
        verdict = IsbnVerdict(
            outcome="contradiction",
            notice=BookMetadata(title="Keeping two", author="Jordan Crane"),
        )
    else:
        verdict = (
            IsbnVerdict(outcome="confirme", notice=BNF_EDITION)
            if confirmed
            else IsbnVerdict(outcome="inconnu")
        )
    bnf.confirm_isbn_for_title = AsyncMock(return_value=verdict)
    bnf.lookup_by_isbn = AsyncMock(return_value=BNF_EDITION)
    return bnf


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


def message_with_recap(command: str) -> tuple[FakeMessage, MagicMock]:
    """Message `/add` et son message d'attente, tous deux observables."""
    message = FakeMessage()
    message.text = command
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)
    return message, loading_message


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


def repositories(
    isbn13: str,
    *,
    already_catalogued: bool = False,
    lazy_created_book: bool = False,
) -> dict:
    """Repositories simulées et spies pour `save_alert`.

    `already_catalogued` décrit un livre déjà au catalogue et déjà coté : c'est le
    seul cas où la confirmation peut afficher un prix sans attendre les marchands.
    `lazy_created_book` décrit le livre qui vient d'être créé : `create` rend un objet
    dont `market_cache` n'est pas chargé, et `get_by_id` le relit comme en production.
    """
    book = MagicMock()
    book.id = uuid.uuid4()
    book.isbn13 = isbn13
    book.market_cache = cached_market_price() if already_catalogued else None

    alert = MagicMock()
    alert.id = uuid.uuid4()

    created = UnloadedBook(isbn13) if lazy_created_book else book
    book_repo = MagicMock()
    book_repo.get_by_isbn13 = AsyncMock(return_value=book if already_catalogued else None)
    book_repo.create = AsyncMock(return_value=created)
    book_repo.get_by_id = AsyncMock(return_value=book)
    book_repo.link_alert_to_book = AsyncMock()

    alert_repo = MagicMock()
    alert_repo.create = AsyncMock(return_value=alert)
    alert_repo.find_existing = AsyncMock(return_value=None)

    category_repo = MagicMock()
    category_repo.get_by_id = AsyncMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    return {
        "book_repo": book_repo,
        "alert_repo": alert_repo,
        "category_repo": category_repo,
        # La cotation part en arrière-plan après la création : le handler ne fait
        # que la demander, elle est vérifiée dans son propre module.
        "schedule_pricing": MagicMock(),
        # La recherche des variantes part en arrière-plan : le test la déclenche lui-même.
        "tasks": [],
    }


def defer_task(holder: list) -> object:
    """Remplace `safe_create_task` : retient la coroutine pour que le test la pilote."""

    def _run(coro, name=None):
        holder.append((coro, name))
        return None

    return _run


async def drain(tasks: list) -> None:
    """Exécute la recherche de fond retenue, comme le ferait la boucle d'événements."""
    for coro, _name in tasks:
        await coro
    tasks.clear()


def patched(repos: dict, bnf: MagicMock, client: MagicMock):
    """Patches de toutes les frontières du flux `/add`."""
    return (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch("app.services.ai.modules.natural_add.natural_add_module.client", client),
        patch(
            "app.services.ai.modules.natural_add.BookMetadataService",
            return_value=bnf,
        ),
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module."
            "generate_variants_and_exclusions",
            new_callable=AsyncMock,
            return_value=KeywordVariantsOutcome(
                variants=["couleurdeschoses"],
                exclusions=["keeping two"],
                answered=True,
            ),
        ),
        patch(
            "app.bot.handlers.alerts_create.safe_create_task",
            new=defer_task(repos["tasks"]),
        ),
        patch("app.bot.handlers.alerts_create.BookRepository", return_value=repos["book_repo"]),
        patch("app.bot.handlers.alerts_create.AlertRepository", return_value=repos["alert_repo"]),
        patch(
            "app.bot.handlers.alerts_create.CategoryRepository",
            return_value=repos["category_repo"],
        ),
        patch(
            "app.bot.handlers.alerts_create.schedule_book_pricing",
            new=repos["schedule_pricing"],
        ),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=repos["alert_repo"]),
        patch("app.bot.handlers.alerts.BookRepository", return_value=repos["book_repo"]),
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=repos["category_repo"]),
    )


def recap_text(loading_message: MagicMock) -> str:
    """Le récap publié par le message d'attente, après ses éditions successives."""
    return loading_message.edit_text.call_args[0][0]


def edition_buttons(loading_message: MagicMock) -> list:
    """Boutons de bascule d'édition proposés par un clavier de récap."""
    keyboard = loading_message.edit_text.call_args[1]["reply_markup"]
    return [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("al_create_edition")
    ]


def work_buttons(keyboard) -> list:
    """Boutons d'œuvres homonymes proposés par un clavier de récap."""
    return [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("al_create_work")
    ]


def incoming_message(texte: str) -> FakeMessage:
    """Message entrant du parcours, avec ses réponses observables."""
    message = FakeMessage()
    message.text = texte
    message.answer = AsyncMock(return_value=MagicMock(spec=Message))
    message.answer.return_value.edit_text = AsyncMock()
    return message


def confirm_press() -> tuple[MagicMock, MagicMock]:
    """Callback « Créer l'alerte » et le message qu'il édite, observables."""
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.data = "alert_create_confirm"
    callback.message = message
    callback.answer = AsyncMock()
    return callback, message


def index_of_line(lines: list[str], prefix: str) -> int:
    """Index de la première ligne qui commence par `prefix`."""
    return next(index for index, line in enumerate(lines) if line.startswith(prefix))


async def open_isbn_edit_field(state: DraftState) -> None:
    """Ouvre le champ ISBN du menu ✏️ Modifier, comme le fait le bouton du récap."""
    from app.bot.handlers.alerts_create_confirm import handle_create_edit_choice

    menu_message = MagicMock(spec=Message)
    menu_message.edit_text = AsyncMock()
    menu_callback = MagicMock()
    menu_callback.data = "al_create_edit:isbn"
    menu_callback.message = menu_message
    menu_callback.answer = AsyncMock()

    await handle_create_edit_choice(menu_callback, state)
    assert state.data["current_edit_field"] == "isbn"

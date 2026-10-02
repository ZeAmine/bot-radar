"""Tests bout en bout du flux `/add` en langage naturel, DeepSeek simulé.

Ces tests enchaînent les vrais handlers (`start_add_alert` → sélection d'édition ou
saisie manuelle de l'ISBN → `save_alert`) et ne simulent que les frontières :
DeepSeek, la BNF, l'agrégateur de marché et les repositories. Ce qu'ils verrouillent
est le contrat produit de l'ADR-014 puis ADR-017 :

- l'ISBN du modèle n'est jamais persisté sans confirmation BNF ;
- les éditions concurrentes sont proposées dans le récap avant la création ;
- l'ISBN finalement retenu est celui qui pilote Vinted, le matching et la cote ;
- un ISBN saisi à la main prime, même quand le modèle en propose un autre.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aiogram.types import Message

from app.bot.handlers.alerts_create import save_alert, start_add_alert
from app.bot.handlers.alerts_create_confirm import (
    handle_create_edit_choice,
    process_creation_edit_input,
    select_alert_creation_edition,
)
from app.core.constants import ComparisonStatus
from app.db.models.user import User
from app.providers.base import MarketOffer
from app.services.book_metadata_service import BookMetadata, BookMetadataService, BookResolution

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
    """Message Telegram recognized par `isinstance`."""

    __class__ = Message


def _deepseek_payload(**overrides) -> dict:
    """Réponse DeepSeek du `/add` : elle propose l'ISBN d'un autre livre."""
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


def _fake_deepseek(payload: dict) -> MagicMock:
    client = MagicMock()
    client.is_configured = True
    client.generate_json = AsyncMock(return_value=payload)
    return client


def _fake_bnf(
    resolution: BookResolution | None,
    verified_isbn: str | None = None,
) -> MagicMock:
    """BNF simulée : `verify_isbn` ne confirme que l'ISBN explicitement déclaré."""
    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(return_value=resolution)
    bnf.verify_isbn = AsyncMock(
        return_value=BNF_EDITION if verified_isbn == BNF_EDITION.isbn13 else None
    )
    bnf.lookup_by_isbn = AsyncMock(return_value=BNF_EDITION)
    return bnf


def _fake_market_offer(isbn13: str) -> MarketOffer:
    return MarketOffer(
        provider_name="chasse_aux_livres",
        item_price_cents=1800,
        shipping_price_cents=None,
        delivered_price_cents=1800,
        source_url=f"https://www.chasse-aux-livres.fr/prix/B0/{isbn13}",
        price_includes_shipping=True,
        item_condition="Très bon (Momox)",
        is_available=True,
        is_comparable=True,
        raw_payload={
            "isbn13": isbn13,
            "best_merchant": "Momox",
            "historical_low_1y_cents": 1650,
            "partners": {},
        },
    )


def _message_with_recap(command: str) -> tuple[FakeMessage, MagicMock]:
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


def _repositories(isbn13: str) -> dict:
    """Repositories simulées et spies pour `save_alert`."""
    book = MagicMock()
    book.id = uuid.uuid4()
    book.isbn13 = isbn13

    alert = MagicMock()
    alert.id = uuid.uuid4()

    book_repo = MagicMock()
    book_repo.get_by_isbn13 = AsyncMock(return_value=None)
    book_repo.create = AsyncMock(return_value=book)
    book_repo.link_alert_to_book = AsyncMock()

    alert_repo = MagicMock()
    alert_repo.create = AsyncMock(return_value=alert)

    category_repo = MagicMock()
    category_repo.get_by_id = AsyncMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    market_repo = MagicMock()
    market_repo.save_snapshot = AsyncMock()
    market_repo.update_or_create_cache = AsyncMock()

    return {
        "book_repo": book_repo,
        "alert_repo": alert_repo,
        "category_repo": category_repo,
        "market_repo": market_repo,
        "provider": MagicMock(get_offers=AsyncMock(return_value=[_fake_market_offer(isbn13)])),
    }


def _patched(repos: dict, bnf: MagicMock, client: MagicMock):
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
            return_value=(["couleurdeschoses"], ["keeping two"]),
        ),
        patch("app.bot.handlers.alerts_create.BookRepository", return_value=repos["book_repo"]),
        patch("app.bot.handlers.alerts_create.AlertRepository", return_value=repos["alert_repo"]),
        patch(
            "app.bot.handlers.alerts_create.CategoryRepository",
            return_value=repos["category_repo"],
        ),
        patch(
            "app.bot.handlers.alerts_create.MarketPriceRepository",
            return_value=repos["market_repo"],
        ),
        patch(
            "app.bot.handlers.alerts_create.ChasseAuxLivresProvider",
            return_value=repos["provider"],
        ),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=repos["alert_repo"]),
        patch("app.bot.handlers.alerts.BookRepository", return_value=repos["book_repo"]),
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=repos["category_repo"]),
        patch(
            "app.bot.handlers.alerts.MarketPriceRepository",
            return_value=repos["market_repo"],
        ),
    )


def _recap_text(loading_message: MagicMock) -> str:
    return loading_message.edit_text.call_args[0][0]


def _edition_buttons(loading_message: MagicMock) -> list:
    keyboard = loading_message.edit_text.call_args[1]["reply_markup"]
    return [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("al_create_edition")
    ]


@pytest.mark.asyncio
async def test_add_end_to_end_model_isbn_of_another_book_then_edition_switch():
    """Le modèle propose l'ISBN d'un autre livre : la BNF tranche, l'utilisateur choisit.

    Chaîne complète : `/add` → récap avec éditions concurrentes → clic sur l'édition
    voulue → création de l'alerte avec l'ISBN choisi, l'URL Vinted ISBN et la cote.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    bnf = _fake_bnf(resolution)
    client = _fake_deepseek(_deepseek_payload())
    repos = _repositories(BNF_ALTERNATIVE.isbn13)

    message, loading_message = _message_with_recap(
        "/add la couleur des choses. Max 12 €"
    )
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    patches = _patched(repos, bnf, client)
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state, db_session, db_user)

        # 1. L'ISBN du modèle est refusé : la BNF le rattache à un autre ouvrage.
        bnf.verify_isbn.assert_awaited_once_with(ISBN_AUTRE_LIVRE, "La couleur des choses")

        recap = _recap_text(loading_message)
        assert "🔢 <b>ISBN :</b> <code>9782369903086</code>" in recap
        assert ISBN_AUTRE_LIVRE not in recap
        assert "Éditions concurrentes de cette œuvre" in recap
        assert "✅ <b><code>9782369903086</code>" in recap
        assert "<code>9782369904151</code>" in recap

        buttons = _edition_buttons(loading_message)
        assert [button.text for button in buttons] == [
            "✅ 9782369903086",
            "📚 9782369904151",
        ]

        # 2. L'utilisateur choisit l'autre édition avant de valider.
        switch_message = MagicMock(spec=Message)
        switch_message.edit_text = AsyncMock()
        switch_callback = MagicMock()
        switch_callback.data = "al_create_edition:1"
        switch_callback.message = switch_message
        switch_callback.answer = AsyncMock()

        await select_alert_creation_edition(switch_callback, state)

        assert state.data["isbn"] == "9782369904151"
        switch_recap = switch_message.edit_text.call_args[0][0]
        assert "🔢 <b>ISBN :</b> <code>9782369904151</code>" in switch_recap

        # 3. Création : l'ISBN choisi pilote l'URL Vinted, le livre et la cote.
        confirm_message = MagicMock(spec=Message)
        confirm_message.edit_text = AsyncMock()
        confirm_callback = MagicMock()
        confirm_callback.data = "alert_create_confirm"
        confirm_callback.message = confirm_message
        confirm_callback.answer = AsyncMock()

        await save_alert(confirm_callback, state, db_user, db_session)

        create_kwargs = repos["alert_repo"].create.call_args.kwargs
        assert create_kwargs["isbn"] == "9782369904151"
        assert "search_text=9782369904151" in create_kwargs["vinted_search_url"]
        assert repos["book_repo"].create.call_args.kwargs["isbn13"] == "9782369904151"

        cache_kwargs = repos["market_repo"].update_or_create_cache.call_args.kwargs
        assert cache_kwargs["book_id"] == repos["book_repo"].create.return_value.id
        assert cache_kwargs["comparison_status"] == ComparisonStatus.AVAILABLE
        assert cache_kwargs["historical_low_1y_cents"] == 1650

        confirmation = confirm_message.edit_text.call_args[0][0]
        assert "ISBN :</b> <code>9782369904151</code>" in confirmation
        assert "16,50 €" in confirmation
    finally:
        for entered in reversed(patches):
            entered.stop()


@pytest.mark.asyncio
async def test_add_end_to_end_user_types_correct_isbn_when_model_is_wrong():
    """Aucun ISBN confirmé : l'utilisateur saisit le bon et l'alerte part dessus.

    Chaîne complète : `/add` avec un ISBN du modèle rejeté et une BNF muette →
    récap en alerte → menu ✏️ Modifier → champ ISBN → saisie du bon ISBN →
    récap à jour → création sur cet ISBN.
    """
    bnf = _fake_bnf(resolution=None)
    client = _fake_deepseek(_deepseek_payload())
    repos = _repositories(BNF_EDITION.isbn13)
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])

    message, loading_message = _message_with_recap(
        "/add la couleur des choses. Max 12 €"
    )
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    patches = _patched(repos, bnf, client)
    with (
        patch(
            "app.bot.handlers.alerts_helpers.BookMetadataService",
            return_value=_fake_bnf(resolution),
        ),
        patch(
            "app.services.book_metadata_service.BookMetadataService",
            return_value=_fake_bnf(resolution),
        ),
    ):
        for entered in patches:
            entered.start()
        try:
            await start_add_alert(message, state, db_session, db_user)

            # 1. Sans confirmation BNF, l'alerte part en surveillance par titre.
            recap = _recap_text(loading_message)
            assert "🔢 <b>ISBN :</b> <i>non confirmé par la BNF</i>" in recap
            assert "surveillance par titre" in recap
            assert not _edition_buttons(loading_message)

            # 2. L'utilisateur corrige via le menu de modification.
            menu_message = MagicMock(spec=Message)
            menu_message.edit_text = AsyncMock()
            menu_callback = MagicMock()
            menu_callback.data = "al_create_edit:isbn"
            menu_callback.message = menu_message
            menu_callback.answer = AsyncMock()

            await handle_create_edit_choice(menu_callback, state)
            assert state.data["current_edit_field"] == "isbn"

            # 3. Saisie du bon ISBN : la liste d'éditions est réalignée.
            typed_message = FakeMessage()
            typed_message.text = "9782369903086"
            typed_message.answer = AsyncMock()
            await process_creation_edit_input(typed_message, state)

            assert state.data["isbn"] == "9782369903086"
            assert state.data["is_series"] is False
            assert [edition["isbn13"] for edition in state.data["edition_alternatives"]] == [
                BNF_EDITION.isbn13,
                BNF_ALTERNATIVE.isbn13,
            ]

            typed_answer = typed_message.answer.call_args[0][0]
            assert "🔢 <b>ISBN :</b> <code>9782369903086</code>" in typed_answer
            assert "Éditions concurrentes de cette œuvre" in typed_answer

            # 4. Création sur l'ISBN saisi, avec l'URL Vinted par ISBN.
            confirm_message = MagicMock(spec=Message)
            confirm_message.edit_text = AsyncMock()
            confirm_callback = MagicMock()
            confirm_callback.data = "alert_create_confirm"
            confirm_callback.message = confirm_message
            confirm_callback.answer = AsyncMock()

            await save_alert(confirm_callback, state, db_user, db_session)

            create_kwargs = repos["alert_repo"].create.call_args.kwargs
            assert create_kwargs["isbn"] == "9782369903086"
            assert "search_start_type=item_page_isbn" in create_kwargs["vinted_search_url"]
            assert "search_text=9782369903086" in create_kwargs["vinted_search_url"]
            assert repos["book_repo"].create.call_args.kwargs["isbn13"] == "9782369903086"
            assert repos["market_repo"].update_or_create_cache.await_count == 1
        finally:
            for entered in reversed(patches):
                entered.stop()


@pytest.mark.asyncio
async def test_add_end_to_end_typed_isbn_beats_model_proposal():
    """Un ISBN saisi dans la commande prime sur celui proposé par le modèle."""
    resolution = BookResolution(best=BNF_ALTERNATIVE)
    bnf = _fake_bnf(resolution, verified_isbn=BNF_EDITION.isbn13)
    client = _fake_deepseek(_deepseek_payload())
    repos = _repositories(BNF_EDITION.isbn13)

    message, loading_message = _message_with_recap(
        "/add 9782369903086 la couleur des choses max 12€"
    )
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    patches = _patched(repos, bnf, client)
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state, db_session, db_user)

        bnf.verify_isbn.assert_awaited_once_with("9782369903086", "La couleur des choses")

        recap = _recap_text(loading_message)
        assert "🔢 <b>ISBN :</b> <code>9782369903086</code>" in recap
        assert ISBN_AUTRE_LIVRE not in recap
    finally:
        for entered in reversed(patches):
            entered.stop()

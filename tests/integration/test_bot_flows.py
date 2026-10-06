"""Les flux Telegram, conduits comme l'utilisateur les conduit.

Ces tests ne simulent pas les handlers : ils nourrissent un vrai `Dispatcher`
aiogram avec de vrais `Update`, laissent l'état FSM vivre dans son stockage, et
exécutent les handlers sur une vraie session SQLAlchemy. Les `callback_data`
proviennent des claviers que le bot a lui-même envoyés — un préfixe renommé
casserait le test au lieu de passer inaperçu.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from aiogram.types import (
    User as TgUser,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers import alerts, callbacks
from app.bot.handlers.alerts_create_confirm import DRAFT_GONE_MESSAGE
from app.bot.middlewares.unhandled_callbacks import (
    STALE_CALLBACK_MESSAGE,
    UnconsumedCallbackMiddleware,
)
from app.core.constants import AlertStatus, CategorySlug, ComparisonStatus
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.services.book_metadata_service import BookMetadata, BookResolution
from tests.database import fresh_database

BOT_TOKEN = "123456789:AAG_ci_test_dummy_token_abc123"
CHAT_ID = 4242
TG_USER_ID = 4242


# --------------------------------------------------------------------------- outillage


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Base réelle (PostgreSQL en CI, SQLite sinon), catégories amorcées."""
    async with fresh_database(replayed_on_postgres=True) as (session, _maker):
        session.add(
            Category(
                id=uuid.uuid4(),
                slug=CategorySlug.BD_COMICS.value,
                label="BD & Comics",
                emoji="📚",
                is_active=True,
            )
        )
        await session.commit()
        yield session


@pytest.fixture
async def driver() -> AsyncGenerator[tuple[Bot, Dispatcher], None]:
    """Les vrais handlers, dans un vrai dispatcher, avec un Bot sans réseau.

    `bot.session` est simulée : aucune requête ne part vers Telegram, et l'objet
    rendu par chaque appel d'API porte les méthodes asynchrones qu'un message
    Telegram expose. Tout le reste est celui de la production — les handlers
    réels, l'injection des dépendances, le FSM — à ceci près que les middlewares
    d'authentification et de débit sont laissés de côté : ils ouvrent leur propre
    session et refuseraient des updates de test.

    Les routeurs de l'application sont des singletons déjà rattachés à la chaîne
    du dispatcher de production. On les rebranche sur une chaîne fraîche, que la
    fixture détache à chaque test pour ne pas laisser un parent derrière elle.
    """
    bot = Bot(token=BOT_TOKEN)
    bot.session = AsyncMock()
    sent = MagicMock()
    sent.edit_text = AsyncMock()
    sent.edit_reply_markup = AsyncMock()
    sent.delete = AsyncMock()
    bot.session.return_value = sent

    # Les routeurs de l'application sont rattachés une fois pour toutes à la chaîne
    # du dispatcher de production, et aiogram refuse un second parent (le setter
    # n'accepte pas non plus `None`). On rebranche donc les deux routeurs utiles sur
    # une chaîne fraîche en remettant leur parent à zéro.
    for module_router in (alerts.router, callbacks.router):
        module_router._parent_router = None

    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    # Le filet global de la production : sans lui, un callback qu'aucun handler ne
    # filtre resterait sans réponse.
    dispatcher.update.outer_middleware(UnconsumedCallbackMiddleware())
    dispatcher.include_router(alerts.router)
    dispatcher.include_router(callbacks.router)
    yield bot, dispatcher
    await storage.close()


def _tg_user() -> TgUser:
    return TgUser(id=TG_USER_ID, is_bot=False, first_name="Test", username="test")


def _chat() -> Chat:
    return Chat(id=CHAT_ID, type="private")


def _message_update(text: str, update_id: int = 1) -> Update:
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=_chat(),
        from_user=_tg_user(),
        text=text,
    )
    return Update(update_id=update_id, message=message)


def _callback_update(data: str, panel_text: str = "panel", update_id: int = 500) -> Update:
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=_chat(),
        from_user=_tg_user(),
        text=panel_text,
    )
    callback = CallbackQuery(
        id=str(uuid.uuid4()),
        from_user=_tg_user(),
        chat_instance="chat-instance",
        data=data,
        message=message,
    )
    return Update(update_id=update_id, callback_query=callback)


def _api_methods(bot: Bot) -> list[Any]:
    return [call.args[1] for call in bot.session.call_args_list]


def _texts(bot: Bot) -> list[str]:
    return [m.text for m in _api_methods(bot) if getattr(m, "text", None)]


def _last_keyboard(bot: Bot) -> InlineKeyboardMarkup:
    for method in reversed(_api_methods(bot)):
        keyboard = getattr(method, "reply_markup", None)
        if isinstance(keyboard, InlineKeyboardMarkup):
            return keyboard
    raise AssertionError("aucun clavier n'a été envoyé")


def _buttons_matching(bot: Bot, prefix: str) -> list[str]:
    keyboard = _last_keyboard(bot)
    return [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith(prefix)
    ]


async def _feed(
    bot: Bot,
    dispatcher: Dispatcher,
    update: Update,
    session: AsyncSession,
    user: User,
) -> None:
    await dispatcher.feed_update(bot, update, db_session=session, db_user=user)


async def _state_of(bot: Bot, dispatcher: Dispatcher):
    context = dispatcher.fsm.get_context(bot=bot, chat_id=CHAT_ID, user_id=TG_USER_ID)
    return await context.get_state()


async def _add_user(session: AsyncSession) -> User:
    user = User(id=uuid.uuid4(), telegram_user_id=TG_USER_ID)
    session.add(user)
    await session.commit()
    return user


def _edition_resolution() -> BookResolution:
    """Une œuvre à deux éditions : c'est ce choix que l'utilisateur arbitre."""
    return BookResolution(
        best=BookMetadata(
            title="La couleur des choses",
            author="Martin Panchaud",
            publisher="Çà et là",
            isbn13="9782369903086",
            publication_year=2022,
        ),
        alternatives=[
            BookMetadata(
                title="La couleur des choses",
                author="Martin Panchaud",
                publisher="Futuré",
                isbn13="9782369904151",
                publication_year=2025,
            )
        ],
    )


def _bnf_stub(resolution: BookResolution | None) -> MagicMock:
    service = MagicMock()
    service.resolve_input = AsyncMock(return_value=resolution)
    service.resolve_for_alert = AsyncMock(return_value=resolution)
    return service


# --------------------------------------------------------------------------- flux (a)


@pytest.mark.asyncio
async def test_edition_then_create_renders_the_confirmation(driver, db_session):
    """Choisir une édition puis « Créer l'alerte » confirme, sur une vraie base.

    Le livre est neuf : `save_alert` doit relire sa fiche avant d'en lire la cote,
    sans quoi la confirmation n'atteint jamais Telegram alors que l'alerte est
    bel et bien écrite.
    """
    bot, dispatcher = driver
    user = await _add_user(db_session)
    bnf = _bnf_stub(_edition_resolution())

    with (
        patch("app.bot.handlers.alerts_helpers.BookMetadataService", return_value=bnf),
        # La cotation part en arrière-plan après la confirmation : un test ne doit pas
        # atteindre les marchands, ni laisser une tâche en vol à sa fermeture.
        patch("app.bot.handlers.alerts_create.schedule_book_pricing") as pricing,
    ):
        await _feed(bot, dispatcher, _message_update("/add", 1), db_session, user)

        category_data = _buttons_matching(bot, "add_cat:")[0]
        assert category_data == f"add_cat:{CategorySlug.BD_COMICS.value}"
        await _feed(bot, dispatcher, _callback_update(category_data, update_id=2), db_session, user)

        await _feed(bot, dispatcher, _message_update("La couleur des choses", 3), db_session, user)
        await _feed(bot, dispatcher, _message_update("12", 4), db_session, user)

        editions = _buttons_matching(bot, "al_create_edition:")
        assert editions == ["al_create_edition:0", "al_create_edition:1"]
        chosen = editions[1]
        await _feed(bot, dispatcher, _callback_update(chosen, update_id=5), db_session, user)

        assert _buttons_matching(bot, "alert_create_confirm") == ["alert_create_confirm"]
        await _feed(
            bot,
            dispatcher,
            _callback_update("alert_create_confirm", update_id=6),
            db_session,
            user,
        )
        # La cote du livre neuf part en arrière-plan, après la confirmation et non avant.
        assert pricing.called, "la cote du livre créé n'a pas été préchauffée"

    confirmation = [text for text in _texts(bot) if "✅ <b>Alerte créée</b>" in text]
    assert confirmation, "la confirmation de création n'a jamais été rendue"
    assert "<b>ISBN :</b> <code>9782369904151</code>" in confirmation[-1]

    assert await _state_of(bot, dispatcher) is None

    # Double appui : le bouton « Créer l'alerte » est encore là une seconde. L'état
    # étant vidé, aucun handler ne le prenait en charge et Telegram faisait tourner
    # le bouton sans fin. Le second appui doit acquitter, sans effacer la confirmation
    # ni créer une deuxième alerte.
    bot.session.call_args_list.clear()
    await _feed(
        bot,
        dispatcher,
        _callback_update("alert_create_confirm", panel_text=confirmation[-1], update_id=7),
        db_session,
        user,
    )
    acknowledgements = [
        method for method in _api_methods(bot) if type(method).__name__ == "AnswerCallbackQuery"
    ]
    assert acknowledgements, "un appui périmé n'a pas été acquitté : le bouton tourne sans fin"
    # Le flux de création explique lui-même pourquoi le bouton est périmé ; le filet
    # global ne sert que là où personne ne parle.
    assert acknowledgements[0].text == DRAFT_GONE_MESSAGE
    rerendered = [
        method
        for method in _api_methods(bot)
        if type(method).__name__ in {"EditMessageText", "SendMessage", "ReplyToMessage"}
    ]
    assert rerendered == [], "l'appui périmé a réécrit la confirmation"
    assert len((await db_session.execute(select(Alert))).scalars().all()) == 1

    alert = (await db_session.execute(select(Alert))).scalar_one()
    assert alert.isbn == "9782369904151"
    assert alert.vinted_search_url is not None
    assert "9782369904151" in alert.vinted_search_url
    book = (await db_session.execute(select(Book))).scalar_one()
    assert book.isbn13 == "9782369904151"
    link = (await db_session.execute(select(AlertBook))).scalar_one()
    assert link.alert_id == alert.id
    assert link.book_id == book.id


# --------------------------------------------------------------------------- flux (b)


@pytest.mark.asyncio
async def test_cancel_at_the_title_step_aborts_without_looking_a_book_up(driver, db_session):
    """« /cancel » annule : il ne doit déclencher aucune recherche d'ouvrage."""
    bot, dispatcher = driver
    user = await _add_user(db_session)
    bnf = _bnf_stub(_edition_resolution())

    with patch("app.bot.handlers.alerts_helpers.BookMetadataService", return_value=bnf):
        await _feed(bot, dispatcher, _message_update("/add", 1), db_session, user)
        category_data = _buttons_matching(bot, "add_cat:")[0]
        await _feed(bot, dispatcher, _callback_update(category_data, update_id=2), db_session, user)

        # L'utilisateur est bien au prompt de titre : c'est là que le bug vivait.
        assert "Entre le titre de l'œuvre" in _texts(bot)[-1]

        await _feed(bot, dispatcher, _message_update("/cancel", 3), db_session, user)

    bnf.resolve_input.assert_not_awaited()
    bnf.resolve_for_alert.assert_not_awaited()
    assert "❌ Opération annulée avec succès." in _texts(bot)
    assert await _state_of(bot, dispatcher) is None
    assert (await db_session.execute(select(Alert))).scalar_one_or_none() is None


# --------------------------------------------------------------------------- flux (c)


async def _seed_series(session: AsyncSession) -> tuple[User, Listing, list[Book]]:
    """Une alerte de série, ses tomes cotés, et l'annonce qui l'a déclenchée."""
    user = await _add_user(session)
    category = (
        await session.execute(
            select(Category).where(Category.slug == CategorySlug.BD_COMICS.value)
        )
    ).scalar_one()

    alert = Alert(
        id=uuid.uuid4(),
        user_id=user.id,
        category_id=category.id,
        name="Blacksad",
        isbn=None,
        include_keywords=["blacksad"],
        exclude_keywords=[],
        variants=[],
        max_item_price_cents=3000,
        accepted_conditions=["new_with_tag"],
        status=AlertStatus.ACTIVE,
    )
    session.add(alert)
    await session.flush()

    books: list[Book] = []
    for number, price in ((1, 509), (2, 899), (3, 983)):
        book = Book(
            id=uuid.uuid4(),
            title=f"Blacksad — Tome {number}",
            normalized_title=f"blacksad tome {number}",
            isbn13=f"97800000000{number:02d}",
        )
        session.add(book)
        await session.flush()
        session.add(
            BookMarketCache(
                id=uuid.uuid4(),
                book_id=book.id,
                lowest_delivered_price_cents=price,
                comparison_status=ComparisonStatus.AVAILABLE,
                historical_low_1y_cents=price,
            )
        )
        session.add(AlertBook(alert_id=alert.id, book_id=book.id))
        books.append(book)

    listing = Listing(
        id=uuid.uuid4(),
        source_name="vinted",
        external_id="extern-blacksad",
        url="https://www.vinted.fr/items/1",
        title="BD Blacksad",
        normalized_title="bd blacksad",
        description="",
        category_slug=CategorySlug.BD_COMICS.value,
        item_price_cents=1000,
        estimated_total_cents=1500,
        photos=[],
        raw_payload={},
    )
    session.add(listing)
    await session.flush()
    session.add(ListingMatch(alert_id=alert.id, listing_id=listing.id))
    await session.commit()
    return user, listing, books


@pytest.mark.asyncio
async def test_back_to_all_tomes_lists_the_real_tomes_and_a_tome_still_opens(driver, db_session):
    """« Tous les tomes » rouvre la liste, et un tome reste sélectionnable.

    Un panneau qui s'ouvre mais dont aucun tome ne se choisit serait encore un
    bouton mort : c'est tout le système derrière le bouton qui est éprouvé ici.
    """
    bot, dispatcher = driver
    user, listing, books = await _seed_series(db_session)

    # Une série déjà cotée ne rappelle pas les marchands, mais si la résolution
    # descendait jusqu'à la cotation, aucun test ne doit partir sur le réseau.
    with patch(
        "app.use_cases.view_market_details.price_book_now",
        new=AsyncMock(return_value=None),
    ):
        panel_data = f"market:{listing.id}"
        await _feed(bot, dispatcher, _callback_update(panel_data, update_id=1), db_session, user)

        tome_buttons = _buttons_matching(bot, "market_tome:")
        assert len(tome_buttons) == len(books), "la série n'a pas listé ses tomes"
        panel = _texts(bot)[-1]
        assert "Cotes par tome" in panel
        for number, price in ((1, "5,09 €"), (2, "8,99 €"), (3, "9,83 €")):
            assert f"Tome {number} : {price}" in panel

        # Un tome se choisit toujours : c'est la suite du flux, pas un bouton mort.
        await _feed(
            bot, dispatcher, _callback_update(tome_buttons[1], update_id=2), db_session, user
        )
        assert "📊 <b>Prix du marché</b>" in _texts(bot)[-1]
        back = _buttons_matching(bot, "market_details:")
        assert back == [f"market_details:{listing.id}"]

        # « Tous les tomes » ramène à la liste des tomes.
        await _feed(
            bot,
            dispatcher,
            _callback_update(back[0], panel_text=_texts(bot)[-1], update_id=3),
            db_session,
            user,
        )

    assert "Cotes par tome" in _texts(bot)[-1]
    reopened = _buttons_matching(bot, "market_tome:")
    assert len(reopened) == len(books)


# --------------------------------------------------------------------------- filet global


@pytest.mark.asyncio
async def test_a_callback_no_handler_wants_stops_spinning(driver, db_session):
    """Un bouton qu'aucun handler ne prend en charge cesse tout de même de tourner."""
    bot, dispatcher = driver
    user = await _add_user(db_session)

    await _feed(
        bot,
        dispatcher,
        _callback_update("bouton_dune_autre_epoque:42", update_id=1),
        db_session,
        user,
    )

    answers = [
        method for method in _api_methods(bot) if type(method).__name__ == "AnswerCallbackQuery"
    ]
    assert len(answers) == 1, "le callback non pris en charge n'a pas été acquitté"
    assert answers[0].text == STALE_CALLBACK_MESSAGE


@pytest.mark.asyncio
async def test_a_handled_callback_is_answered_exactly_once(driver, db_session):
    """Le filet ne double pas l'acquittement d'un handler."""
    bot, dispatcher = driver
    user = await _add_user(db_session)

    await _feed(
        bot,
        dispatcher,
        _callback_update("alert_create_cancel", update_id=1),
        db_session,
        user,
    )

    answers = [
        method for method in _api_methods(bot) if type(method).__name__ == "AnswerCallbackQuery"
    ]
    assert len(answers) == 1, "le filet a répondu en plus du handler"

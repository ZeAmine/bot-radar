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
from datetime import UTC, datetime, timedelta
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
from app.use_cases import prepare_bd_alert as preparation
from tests.database import postgres_replayed_database
from tests.fakes_market_source import source_history_values

BOT_TOKEN = "123456789:AAG_ci_test_dummy_token_abc123"
CHAT_ID = 4242
TG_USER_ID = 4242


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Base réelle (PostgreSQL en CI, SQLite sinon), catégories amorcées."""
    async with postgres_replayed_database() as database:
        session = database.session
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
async def driver(monkeypatch, db_session) -> AsyncGenerator[tuple[Bot, Dispatcher], None]:
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
    bot.last_message_id = 1000

    async def fake_api(_bot, method, **_kwargs):
        from aiogram.methods import EditMessageText, SendMessage

        if isinstance(method, (SendMessage, EditMessageText)):
            if isinstance(method, SendMessage):
                bot.last_message_id += 1
            message_id = getattr(method, "message_id", None) or bot.last_message_id
            return Message(
                message_id=message_id,
                date=datetime.now(UTC),
                chat=_chat(),
                text=method.text,
                reply_markup=method.reply_markup,
            ).as_(bot)
        return True

    bot.session.side_effect = fake_api
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.bot.handlers import market_panel

    maker = async_sessionmaker(db_session.bind, expire_on_commit=False, autoflush=False)

    async def cached_price(book_id, *, reason):
        assert reason == "alert_created"
        from app.db.repositories.book_repo import BookRepository

        async with maker() as session:
            book = await BookRepository(session).get_by_id(book_id)
            if book.market_cache is not None:
                return book.market_cache
            cache = BookMarketCache(
                book_id=book_id,
                comparison_status=ComparisonStatus.UNAVAILABLE,
                checked_at=datetime.now(UTC),
                collection_status="empty",
            )
            session.add(cache)
            await session.commit()
            return cache

    monkeypatch.setattr(preparation, "AsyncSessionLocal", maker)
    monkeypatch.setattr(preparation, "preparation_runner", preparation.PreparationRunner())
    monkeypatch.setattr(preparation, "price_book_now", cached_price)

    for module_router in (alerts.router, callbacks.router):
        module_router._parent_router = None

    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.update.outer_middleware(UnconsumedCallbackMiddleware())
    dispatcher.include_router(alerts.router)
    dispatcher.include_router(callbacks.router)
    yield bot, dispatcher
    await storage.close()
    for view in list(market_panel._panels.values()):
        market_panel.close_panel(view.message)


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


def _callback_update(
    data: str, panel_text: str = "panel", update_id: int = 500, message_id: int | None = None
) -> Update:
    message = Message(
        message_id=message_id or update_id,
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


@pytest.mark.parametrize(
    "action",
    [
        "alert_create_cancel",
        "alert_create_confirm",
        "al_create_edition:0",
        "al_create_edit:name",
        "cond_toggle:0",
    ],
)
async def test_old_draft_buttons_leave_new_draft_and_database_untouched(driver, db_session, action):
    from app.bot.states.alert_creation import AlertCreationState

    bot, dispatcher = driver
    user = await _add_user(db_session)
    state = dispatcher.fsm.get_context(bot=bot, chat_id=CHAT_ID, user_id=TG_USER_ID)
    draft = {"creation_token": "new-draft", "category_slug": "tech", "name": "B"}
    await state.set_data(draft)
    await state.set_state(AlertCreationState.confirm_creation)
    await _feed(bot, dispatcher, _callback_update(action + "@old-draft"), db_session, user)
    assert await state.get_data() == draft
    assert await state.get_state() == AlertCreationState.confirm_creation.state
    assert (await db_session.execute(select(Alert))).scalars().all() == []


async def test_new_add_replaces_button_generation(driver, db_session):
    bot, dispatcher = driver
    user = await _add_user(db_session)
    await _feed(bot, dispatcher, _message_update("/add", 1), db_session, user)
    old_category = _buttons_matching(bot, "add_cat:")[0]
    await _feed(bot, dispatcher, _message_update("/add", 2), db_session, user)
    new_category = _buttons_matching(bot, "add_cat:")[0]
    assert new_category != old_category
    state = dispatcher.fsm.get_context(bot=bot, chat_id=CHAT_ID, user_id=TG_USER_ID)
    before = await state.get_data()
    await _feed(bot, dispatcher, _callback_update(old_category), db_session, user)
    assert await state.get_data() == before
    await _feed(bot, dispatcher, _callback_update(new_category), db_session, user)
    assert (await state.get_data())["category_slug"] == "bd_comics"


@pytest.mark.parametrize("action", ["keep", "update"])
@pytest.mark.parametrize("new_price", [0, 2000])
async def test_existing_alert_buttons_are_bound_and_current_ones_work(
    driver, db_session, action, new_price
):
    from app.bot.handlers.alerts_helpers import render_existing_alert_response

    bot, dispatcher = driver
    user = await _add_user(db_session)
    category = (await db_session.execute(select(Category))).scalar_one()
    existing = Alert(
        user_id=user.id,
        category_id=category.id,
        name="Existante",
        include_keywords=["existante"],
        max_item_price_cents=1500,
    )
    db_session.add(existing)
    await db_session.commit()
    await _feed(bot, dispatcher, _message_update("/add", 1), db_session, user)
    state = dispatcher.fsm.get_context(bot=bot, chat_id=CHAT_ID, user_id=TG_USER_ID)
    token = (await state.get_data())["creation_token"]
    message = _message_update("Existante 20€").message.as_(bot)
    await render_existing_alert_response(message, state, existing, new_price_cents=new_price)
    prefix = "alert_create_cancel" if action == "keep" else "upd_alert_price:"
    button = _buttons_matching(bot, prefix)[0]
    assert button.endswith(f"@{token}")
    assert len(button.encode()) <= 64
    await _feed(bot, dispatcher, _callback_update(button), db_session, user)
    await db_session.refresh(existing)
    assert existing.max_item_price_cents == (1500 if action == "keep" else new_price)
    assert await state.get_data() == {}


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
    """Cas d'usage simulé, fidèle aux deux chemins du contrat.

    `search_by_title` mappe la résolution figée ; `search_by_isbn` (la revalidation
    d'un choix d'œuvre/édition) ne confirme que les ISBN que cette résolution porte
    réellement — tout autre code rend `not_found`, comme une autorité qui ne le
    connaît pas.
    """
    from app.domain.enums import BookSearchStatus
    from app.services.book_metadata_service import BibliographicReport
    from app.use_cases.book_search import (
        BookSearchResult,
        result_from_report,
    )

    notices = (
        [resolution.best, *resolution.alternatives, *resolution.other_works]
        if resolution is not None
        else []
    )
    known = {notice.isbn13: notice for notice in notices if notice.isbn13}

    service = MagicMock()

    async def _search_by_title(title: str, _author_hint: str | None = None, **_options):
        return result_from_report(
            BibliographicReport(kind="title", resolution=resolution), query_title=title
        )

    async def _search_by_isbn(isbn: str, expected_title: str | None = None):
        notice = known.get(isbn)
        if notice is None:
            return BookSearchResult(status=BookSearchStatus.NOT_FOUND)
        return result_from_report(
            BibliographicReport(kind="title", resolution=BookResolution(best=notice)),
            query_title=expected_title or notice.title,
        )

    service.search_by_title = AsyncMock(side_effect=_search_by_title)
    service.search_by_isbn = AsyncMock(side_effect=_search_by_isbn)
    return service


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
        patch("app.bot.handlers.alerts_helpers.BookSearchUseCase", return_value=bnf),
        patch("app.bot.handlers.alerts_create_confirm.BookSearchUseCase", return_value=bnf),
        patch(
            "app.use_cases.prepare_bd_alert.price_book_now",
            new_callable=AsyncMock,
            wraps=preparation.price_book_now,
        ) as pricing,
    ):
        await _feed(bot, dispatcher, _message_update("/add", 1), db_session, user)

        category_data = _buttons_matching(bot, "add_cat:")[0]
        assert category_data.partition("@")[0] == f"add_cat:{CategorySlug.BD_COMICS.value}"
        token = category_data.partition("@")[2]
        assert len(token) == 12
        await _feed(bot, dispatcher, _callback_update(category_data, update_id=2), db_session, user)

        await _feed(bot, dispatcher, _message_update("La couleur des choses", 3), db_session, user)
        await _feed(bot, dispatcher, _message_update("12", 4), db_session, user)

        editions = _buttons_matching(bot, "al_create_edition:")
        assert editions == [f"al_create_edition:0@{token}", f"al_create_edition:1@{token}"]
        chosen = editions[1]
        await _feed(bot, dispatcher, _callback_update(chosen, update_id=5), db_session, user)

        assert _buttons_matching(bot, "alert_create_confirm") == [f"alert_create_confirm@{token}"]
        await _feed(
            bot,
            dispatcher,
            _callback_update(_buttons_matching(bot, "alert_create_confirm")[0], update_id=6),
            db_session,
            user,
        )
        # La cote du livre neuf est demandée avant l'activation et la confirmation.
        assert pricing.called, "la cote du livre créé n'a pas été préchauffée"

    confirmation = [text for text in _texts(bot) if "✅ <b>Alerte créée</b>" in text]
    assert confirmation, "la confirmation de création n'a jamais été rendue"
    assert "<b>ISBN :</b> <code>9782369904151</code>" in confirmation[-1]

    assert await _state_of(bot, dispatcher) is None

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
    # Ici le flux explique lui-même le bouton périmé ; le filet global ne parle qu'ailleurs.
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


@pytest.mark.asyncio
async def test_the_confirmation_dates_a_settled_absence_instead_of_promising_a_cote(
    driver, db_session
):
    """Une passe déjà revenue vide se dit par sa date, jamais par « en cours de calcul ».

    `save_alert` relit la fiche du livre qu'il vient de lier : quand une passe a eu
    lieu sans trouver d'offre, la confirmation doit le dire. La même cote est servie
    par plusieurs plateformes, et c'est le relevé — pas le prix — que sa date nomme.
    """
    from app.services.market_price_service import NO_OFFER_LINE, PENDING_COTE_LINE

    bot, dispatcher = driver
    user = await _add_user(db_session)

    # Le livre est déjà au catalogue, coté une fois : cette passe-là n'a rien trouvé.
    book = Book(
        id=uuid.uuid4(),
        title="La couleur des choses",
        normalized_title="la couleur des choses",
        isbn13="9782369904151",
    )
    db_session.add(book)
    await db_session.flush()
    db_session.add(
        BookMarketCache(
            id=uuid.uuid4(),
            book_id=book.id,
            lowest_delivered_price_cents=None,
            comparison_status=ComparisonStatus.UNAVAILABLE,
            historical_low_1y_cents=None,
            collection_status="empty",
            last_successful_collection_at=datetime(2026, 10, 6, 9, 30, tzinfo=UTC),
            checked_at=datetime(2026, 10, 6, 9, 30, tzinfo=UTC),
        )
    )
    await db_session.commit()

    bnf = _bnf_stub(_edition_resolution())
    with (
        patch("app.bot.handlers.alerts_helpers.BookSearchUseCase", return_value=bnf),
        patch("app.bot.handlers.alerts_create_confirm.BookSearchUseCase", return_value=bnf),
        patch(
            "app.use_cases.prepare_bd_alert.price_book_now",
            new_callable=AsyncMock,
            wraps=preparation.price_book_now,
        ),
    ):
        await _feed(bot, dispatcher, _message_update("/add", 1), db_session, user)
        category_data = _buttons_matching(bot, "add_cat:")[0]
        await _feed(bot, dispatcher, _callback_update(category_data, update_id=2), db_session, user)
        await _feed(bot, dispatcher, _message_update("La couleur des choses", 3), db_session, user)
        await _feed(bot, dispatcher, _message_update("12", 4), db_session, user)

        # L'édition qui porte l'ISBN du livre déjà coté.
        chosen = _buttons_matching(bot, "al_create_edition:")[1]
        await _feed(bot, dispatcher, _callback_update(chosen, update_id=5), db_session, user)
        await _feed(
            bot,
            dispatcher,
            _callback_update(_buttons_matching(bot, "alert_create_confirm")[0], update_id=6),
            db_session,
            user,
        )

    confirmation = [text for text in _texts(bot) if "✅ <b>Alerte créée</b>" in text]
    assert confirmation, "la confirmation de création n'a jamais été rendue"
    assert NO_OFFER_LINE in confirmation[-1]
    # La date est celle du relevé, lue heure de Paris.
    assert "06/10" not in confirmation[-1]
    assert PENDING_COTE_LINE not in confirmation[-1]


@pytest.mark.asyncio
async def test_cancel_at_the_title_step_aborts_without_looking_a_book_up(driver, db_session):
    """« /cancel » annule : il ne doit déclencher aucune recherche d'ouvrage."""
    bot, dispatcher = driver
    user = await _add_user(db_session)
    bnf = _bnf_stub(_edition_resolution())

    with patch("app.bot.handlers.alerts_helpers.BookSearchUseCase", return_value=bnf):
        await _feed(bot, dispatcher, _message_update("/add", 1), db_session, user)
        category_data = _buttons_matching(bot, "add_cat:")[0]
        await _feed(bot, dispatcher, _callback_update(category_data, update_id=2), db_session, user)

        # C'est au prompt de titre que l'annulation est exercée.
        assert "Entre le titre de l'œuvre" in _texts(bot)[-1]

        await _feed(bot, dispatcher, _message_update("/cancel", 3), db_session, user)

    bnf.search_by_title.assert_not_awaited()
    bnf.search_by_title.assert_not_awaited()
    assert "❌ Opération annulée avec succès." in _texts(bot)
    assert await _state_of(bot, dispatcher) is None
    assert (await db_session.execute(select(Alert))).scalar_one_or_none() is None


async def _seed_series(session: AsyncSession) -> tuple[User, Listing, list[Book]]:
    """Une alerte de série, ses tomes cotés, et l'annonce qui l'a déclenchée."""
    user = await _add_user(session)
    category = (
        await session.execute(select(Category).where(Category.slug == CategorySlug.BD_COMICS.value))
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
                **source_history_values(price, datetime.now(UTC)),
                comparison_status=ComparisonStatus.AVAILABLE,
                historical_low_1y_cents=price,
                checked_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
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
async def test_tome_buttons_allow_switching_without_a_global_series_button(driver, db_session):
    """Les boutons individuels permettent de passer directement d'un tome à l'autre.

    Un panneau qui s'ouvre mais dont aucun tome ne se choisit serait encore un
    bouton mort : c'est tout le système derrière le bouton qui est éprouvé ici.
    """
    bot, dispatcher = driver
    user, listing, books = await _seed_series(db_session)
    panel_data = f"market:{listing.id}"
    await _feed(bot, dispatcher, _callback_update(panel_data, update_id=1), db_session, user)

    tome_buttons = _buttons_matching(bot, "market_tome:")
    assert len(tome_buttons) == len(books), "la série n'a pas listé ses tomes"
    panel = _texts(bot)[-1]
    assert "Tome 1" in panel
    for number, price in ((1, "5,09 €"), (2, "8,99 €"), (3, "9,83 €")):
        assert f"Tome {number}" in panel
        assert price in panel

    # Un tome se choisit toujours : c'est la suite du flux, pas un bouton mort.
    await _feed(
        bot,
        dispatcher,
        _callback_update(tome_buttons[1], update_id=2, message_id=bot.last_message_id),
        db_session,
        user,
    )
    assert "📊 <b>Prix du marché</b>" in _texts(bot)[-1]
    assert _buttons_matching(bot, "market_details:") == []
    reopened = _buttons_matching(bot, "market_tome:")
    assert len(reopened) == len(books)
    await _feed(
        bot,
        dispatcher,
        _callback_update(reopened[0], update_id=3, message_id=bot.last_message_id),
        db_session,
        user,
    )
    assert "Tome 1" in _texts(bot)[-1]


@pytest.mark.asyncio
async def test_market_panel_reads_the_cache_and_keeps_the_selected_tome(driver, db_session):
    """Le panneau ne relance aucune collecte : il lit le cache et garde le tome choisi."""
    from app.bot.handlers import market_panel

    bot, dispatcher = driver
    user, _listing, books = await _seed_series(db_session)
    alert_id = (await db_session.execute(select(Alert.id))).scalar_one()

    with patch(
        "app.services.book_market_pricing.price_book_now", new=AsyncMock()
    ) as fetch:
        await _feed(bot, dispatcher, _callback_update(f"ed_f:{alert_id}:market"), db_session, user)
        message_id = bot.last_message_id
        first = _texts(bot)[-1]
        assert "Actualisation" not in first
        assert "Dernière MAJ :" in first
        assert "5,09 €" in first
        fetch.assert_not_awaited()

        buttons = _buttons_matching(bot, "market_tome:")
        await _feed(
            bot,
            dispatcher,
            _callback_update(buttons[1], message_id=message_id),
            db_session,
            user,
        )
        selected = _texts(bot)[-1]
        assert "<b>Blacksad — Tome 2</b>" in selected
        assert "8,99 €" in selected
        assert "Dernière MAJ :" in selected
        fetch.assert_not_awaited()

    await _feed(
        bot,
        dispatcher,
        _callback_update("close_market_panel", message_id=message_id),
        db_session,
        user,
    )
    assert (CHAT_ID, message_id) not in market_panel._panels


async def _seed_series_without_prices(db_session) -> tuple[Alert, list[Book]]:
    """Alerte série (sans ISBN) + deux volumes liés, sans aucune cote en base."""
    user = await _add_user(db_session)
    category = (
        await db_session.execute(
            select(Category).where(Category.slug == CategorySlug.BD_COMICS.value)
        )
    ).scalar_one()

    alert = Alert(
        id=uuid.uuid4(),
        user_id=user.id,
        category_id=category.id,
        name="Série X",
        isbn=None,
        include_keywords=["serie x"],
        exclude_keywords=[],
        variants=[],
        max_item_price_cents=5000,
        accepted_conditions=None,
        status=AlertStatus.ACTIVE,
    )
    db_session.add(alert)
    await db_session.flush()

    books: list[Book] = []
    for number in (1, 2):
        book = Book(
            id=uuid.uuid4(),
            title=f"Série X — Tome {number}",
            normalized_title=f"serie x tome {number}",
            isbn13=f"97800000000{number:02d}",
        )
        db_session.add(book)
        await db_session.flush()
        db_session.add(AlertBook(alert_id=alert.id, book_id=book.id))
        books.append(book)

    await db_session.commit()
    return alert, books


@pytest.mark.asyncio
async def test_price_write_recomputes_the_series_total(db_session):
    """E4 : chaque écriture de cote réécrit le total de série de l'alerte.

    Le total n'est jamais recomposé à l'affichage : il suit les cotes parce qu'il
    est réécrit là où elles le sont (`persist_market_result`, chemin unique).
    """
    from app.core.constants import MatchConfidence
    from app.providers.base import MarketOffer
    from app.services.book_market_pricing import BookPricingResult, persist_market_result

    alert, books = await _seed_series_without_prices(db_session)
    assert alert.series_total_cents is None

    from app.db.repositories.market_price_repo import MarketPriceRepository

    for book, historical in zip(books[:2], (300, 450), strict=True):
        await MarketPriceRepository(db_session).save_snapshot(
            book.id,
            MarketOffer(
                provider_name="synthetic",
                source_url="https://example.invalid/observed",
                item_price_cents=historical,
                delivered_price_cents=historical,
                shipping_price_cents=0,
                item_condition="Très bon",
                observed_at=datetime.now(UTC) - timedelta(days=30),
            ),
        )
    await db_session.commit()

    # Premier volume coté : le total ne porte que sur ce qui est connu.
    await persist_market_result(
        db_session,
        books[0].id,
        BookPricingResult(
            offers=[
                MarketOffer(
                    provider_name="synthetic",
                    source_url="https://example.invalid/observed",
                    item_price_cents=500,
                    delivered_price_cents=500,
                    shipping_price_cents=0,
                    item_condition="Très bon",
                    observed_at=datetime.now(UTC),
                )
            ],
            lowest_delivered_price_cents=500,
            current_observed_at=datetime.now(UTC),
            best_provider_name="test",
            best_source_url=None,
            confidence=MatchConfidence.HIGH,
            comparison_status=ComparisonStatus.AVAILABLE,
            cache_duration_hours=24,
            historical_low_1y_cents=300,
        ),
    )
    await db_session.commit()
    await db_session.refresh(alert)
    assert alert.series_total_cents is None

    # Second volume coté : le total s'étend à la série connue.
    await persist_market_result(
        db_session,
        books[1].id,
        BookPricingResult(
            offers=[
                MarketOffer(
                    provider_name="synthetic",
                    source_url="https://example.invalid/observed",
                    item_price_cents=700,
                    delivered_price_cents=700,
                    shipping_price_cents=0,
                    item_condition="Très bon",
                    observed_at=datetime.now(UTC),
                )
            ],
            lowest_delivered_price_cents=700,
            current_observed_at=datetime.now(UTC),
            best_provider_name="test",
            best_source_url=None,
            confidence=MatchConfidence.HIGH,
            comparison_status=ComparisonStatus.AVAILABLE,
            cache_duration_hours=24,
            historical_low_1y_cents=450,
        ),
    )
    await db_session.commit()
    await db_session.refresh(alert)
    assert alert.series_total_cents == 1200


@pytest.mark.asyncio
async def test_single_book_alert_has_no_series_total(db_session):
    """E4 : une alerte à un seul livre n'est pas une série — son total reste NULL."""
    from app.core.constants import MatchConfidence
    from app.services.book_market_pricing import BookPricingResult, persist_market_result

    user = await _add_user(db_session)
    category = (
        await db_session.execute(
            select(Category).where(Category.slug == CategorySlug.BD_COMICS.value)
        )
    ).scalar_one()

    alert = Alert(
        id=uuid.uuid4(),
        user_id=user.id,
        category_id=category.id,
        name="One Shot",
        isbn="9780000000001",
        include_keywords=["one shot"],
        exclude_keywords=[],
        variants=[],
        max_item_price_cents=2000,
        accepted_conditions=None,
        status=AlertStatus.ACTIVE,
    )
    db_session.add(alert)
    await db_session.flush()

    book = Book(
        id=uuid.uuid4(),
        title="One Shot",
        normalized_title="one shot",
        isbn13="9780000000001",
    )
    db_session.add(book)
    await db_session.flush()
    db_session.add(AlertBook(alert_id=alert.id, book_id=book.id))
    await db_session.commit()

    await persist_market_result(
        db_session,
        book.id,
        BookPricingResult(
            offers=[],
            lowest_delivered_price_cents=400,
            best_provider_name="test",
            best_source_url=None,
            confidence=MatchConfidence.HIGH,
            comparison_status=ComparisonStatus.AVAILABLE,
            cache_duration_hours=24,
            historical_low_1y_cents=350,
        ),
    )
    await db_session.commit()
    await db_session.refresh(alert)
    assert alert.series_total_cents is None


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


@pytest.mark.asyncio
async def test_the_catalogue_verdict_crosses_a_real_database() -> None:
    """Le verdict traverse la base : écrit par la liaison, relu par l'alerte.

    Les deux moitiés étaient prouvées séparément, sur des doubles : rien ne disait
    que la colonne persiste ce que la liaison a conclu. Ici, une vraie session, et
    les trois issues. Une série que personne ne catalogue laisse sa date ; une
    autorité muette ne laisse **rien** (un silence ne prouve rien, dans aucun sens) ;
    une notice arrivée l'efface, sans quoi l'alerte continuerait de dire une
    exhaustion que la réalité a démentie.
    """
    from app.domain.rules.catalogue_search import is_uncatalogued
    from app.services.book_metadata_service import BookMetadataService, CatalogueVerdict
    from app.use_cases import link_bd_catalog as catalog_linking

    notice = BookMetadata(
        title="Absolute Martian Manhunter — Tome 1",
        author="Deniz Camp",
        isbn13="9782369904151",
        source="bnf",
    )

    async with postgres_replayed_database() as database:
        session, maker = database.session, database.maker
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
            name="Absolute Martian Manhunter",
            isbn=None,
            include_keywords=["absolute martian manhunter"],
            exclude_keywords=[],
            variants=[],
            max_item_price_cents=5000,
            accepted_conditions=None,
            status=AlertStatus.ACTIVE,
        )
        session.add(alert)
        await session.commit()
        alert_id = alert.id

        async def _search(verdict: CatalogueVerdict) -> None:
            """Une recherche de fond dont le résultat est décidé par l'appelant."""
            with (
                patch(
                    "app.db.session.AsyncSessionLocal",
                    MagicMock(return_value=maker()),
                ),
                patch.object(
                    BookMetadataService,
                    "lookup_by_title_and_author",
                    new=AsyncMock(return_value=verdict),
                ),
                patch.object(catalog_linking, "schedule_book_pricing"),
            ):
                await catalog_linking.link_books_from_bnf(alert.name, alert_id)

        async def _reloaded() -> Alert:
            session.expire_all()
            return await session.get(Alert, alert_id)

        # Toutes les autorités ont répondu, aucune ne connaît l'ouvrage : la
        await _search(CatalogueVerdict(consulted=("bnf", "openlibrary")))
        exhausted = await _reloaded()
        assert is_uncatalogued(exhausted) is True
        assert isinstance(exhausted.book_search_exhausted_at, datetime)

        # Une autorité s'est tue : rien n'est établi, rien n'est écrit — et le
        await _search(CatalogueVerdict(consulted=("bnf",), unavailable=("bnf",)))
        silent = await _reloaded()
        assert is_uncatalogued(silent) is True, "un silence n'efface pas ce qu'on savait"

        # Une notice est arrivée : l'ouvrage est catalogué, le verdict s'efface.
        await _search(CatalogueVerdict(consulted=("bnf",), notices=(notice,)))
        found = await _reloaded()
        assert is_uncatalogued(found) is False
        assert found.book_search_exhausted_at is None

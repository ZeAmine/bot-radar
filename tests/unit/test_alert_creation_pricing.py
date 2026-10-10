"""La création prépare les cotes avant d'activer la surveillance."""

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from sqlalchemy import select

from app.bot.handlers.alerts_create import save_alert
from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.category import Category
from app.db.models.user import User
from app.use_cases import prepare_bd_alert as preparation
from tests.add_fakes import DraftState, cached_market_price
from tests.database import sqlite_only_database
from tests.screens import confirm_press


@pytest.fixture
async def preparation_db(monkeypatch):
    async with sqlite_only_database() as database:
        database.maker.configure(autoflush=False)
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        user = User(telegram_user_id=601, is_allowed=True)
        database.session.add_all([category, user])
        await database.session.commit()
        monkeypatch.setattr(preparation, "AsyncSessionLocal", database.maker)
        monkeypatch.setattr(preparation, "preparation_runner", preparation.PreparationRunner())
        yield database, user


@pytest.mark.parametrize("progress_fails", [False, True])
async def test_creation_waits_for_price_before_activating_and_confirming(
    preparation_db, monkeypatch, progress_fails
):
    database, user = preparation_db
    state = DraftState()
    state.data = {
        "category_slug": "bd_comics",
        "name": "Livre",
        "isbn": "9782369904151",
        "include_keywords": ["livre"],
    }
    callback, message = confirm_press()
    if progress_fails:
        message.edit_text.side_effect = [
            TelegramBadRequest(
                method=EditMessageText(chat_id=1, message_id=1, text="Préparation"),
                message="message is not modified",
            ),
            None,
        ]
    price = cached_market_price()

    async def collect(book_id, *, reason):
        assert reason == "alert_created"
        async with database.maker() as observer:
            book = (await observer.execute(select(Book))).scalar_one()
            assert book_id == book.id
            alert = (await observer.execute(select(Alert))).scalar_one()
            assert alert.status == AlertStatus.PAUSED
            assert alert.preparation["activation_requested"] is True
            price.book_id = book_id
            observer.add(price)
            await observer.commit()
        assert "Préparation des prix" in message.edit_text.call_args.args[0]
        assert state.data == {}
        state.data = {"name": "Nouveau brouillon"}
        return price

    monkeypatch.setattr(preparation, "price_book_now", collect)
    await save_alert(callback, state, user, database.session)
    # L'activation est désormais committée dans une session dédiée. Relire les
    # deux états durables vérifie la frontière, au lieu de compter les anciens mocks.
    async with database.maker() as observer:
        alert = (await observer.execute(select(Alert))).scalar_one()
        assert alert.status == AlertStatus.ACTIVE
        assert alert.preparation is None
    assert state.data == {"name": "Nouveau brouillon"}
    assert "<b>Prix du marché :</b> 18,00 €" in message.edit_text.call_args.args[0]
    assert "en cours de calcul" not in message.edit_text.call_args.args[0]


async def test_volume_creation_persists_number_and_confirmed_edition_language(
    preparation_db, monkeypatch
):
    from app.bot.handlers.alerts_helpers import draft_updates_from_search
    from app.services.book_metadata_service import BookMetadata, BookResolution
    from app.use_cases.book_search import result_from_resolution

    database, user = preparation_db
    notice = BookMetadata(
        title="The Sad Ghost Club", volume_title="The Sad Ghost Club Volume 4",
        isbn13="9781444977387", author="Lize Meddings", language="en",
    )
    result = result_from_resolution(BookResolution(best=notice), "The Sad Ghost Club Volume 4")
    state = DraftState()
    state.data = draft_updates_from_search(result, query_title="The Sad Ghost Club Volume 4")
    callback, message = confirm_press()
    price = cached_market_price()

    async def collect(book_id, *, reason):
        assert reason == "alert_created"
        async with database.maker() as observer:
            price.book_id = book_id
            observer.add(price)
            await observer.commit()
        return price

    monkeypatch.setattr(preparation, "price_book_now", collect)
    await save_alert(callback, state, user, database.session)
    async with database.maker() as observer:
        alert = (await observer.execute(select(Alert))).scalar_one()
        book = (await observer.execute(select(Book))).scalar_one()
        assert alert.name == "The Sad Ghost Club - Volume 4"
        assert alert.isbn == "9781444977387"
        assert book.language == "en"
    assert "The Sad Ghost Club - Volume 4" in message.edit_text.call_args.args[0]
    assert "<b>Langue :</b> Anglais" in message.edit_text.call_args.args[0]

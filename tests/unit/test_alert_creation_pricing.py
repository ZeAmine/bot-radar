"""La création prépare les cotes avant d'activer la surveillance."""

import uuid

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText
from sqlalchemy import select

from app.bot.handlers.alerts_create import save_alert
from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.category import Category
from app.db.models.user import User
from app.use_cases import prepare_bd_alert as preparation
from tests.add_fakes import DraftState as _DraftState
from tests.database import sqlite_only_database
from tests.screens import confirm_press


class DraftState(_DraftState):
    """Identité FSM locale, sans modifier le double partagé."""

    def __init__(self):
        super().__init__()
        self.storage = MemoryStorage()
        self.key = StorageKey(bot_id=1, chat_id=42, user_id=42)


def cached_market_price():
    """Offre courante admissible ; ancien minimum sans preuve annuelle."""
    from datetime import UTC, datetime, timedelta

    from app.core.constants import ComparisonStatus
    from app.db.models.book_market_cache import BookMarketCache

    now = datetime.now(UTC)
    return BookMarketCache(
        id=uuid.uuid4(),
        book_id=uuid.uuid4(),
        lowest_delivered_price_cents=1800,
        checked_at=now,
        expires_at=now + timedelta(days=1),
        comparison_status=ComparisonStatus.AVAILABLE,
        best_provider_name="synthetic",
        historical_low_1y_cents=1650,
        annual_low_cents=None,
        annual_low_observed_at=None,
        annual_low_snapshot_id=None,
        annual_low_provider=None,
        annual_first_at=None,
        annual_last_at=None,
        annual_count=0,
        annual_computed_at=None,
        annual_expires_at=None,
    )


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
    # L'activation est committée dans une session dédiée : les deux états durables le vérifient.
    async with database.maker() as observer:
        alert = (await observer.execute(select(Alert))).scalar_one()
        assert alert.status == AlertStatus.ACTIVE
        assert alert.preparation is None
    assert state.data == {"name": "Nouveau brouillon"}
    assert "(dernière offre connue : 18,00 €)" in message.edit_text.call_args.args[0]
    assert "en cours de calcul" not in message.edit_text.call_args.args[0]


async def test_volume_creation_persists_number_and_confirmed_edition_language(
    preparation_db, monkeypatch
):
    from app.bot.handlers.alerts_helpers import draft_updates_from_search
    from app.services.book_metadata_service import BookMetadata, BookResolution
    from app.use_cases.book_search import result_from_resolution

    database, user = preparation_db
    notice = BookMetadata(
        title="The Sad Ghost Club",
        volume_title="The Sad Ghost Club Volume 4",
        isbn13="9781444977387",
        author="Lize Meddings",
        language="en",
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

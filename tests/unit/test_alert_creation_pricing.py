"""La création prépare les cotes avant d'activer la surveillance."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from app.bot.handlers.alerts_create import save_alert
from app.core.constants import AlertStatus
from tests.add_fakes import DraftState, cached_market_price
from tests.add_journey import patched, repositories
from tests.screens import confirm_press


@pytest.mark.parametrize("progress_fails", [False, True])
async def test_creation_waits_for_price_before_activating_and_confirming(progress_fails):
    repos = repositories("9782369904151")
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
    session = AsyncMock()
    book = repos["book_repo"].get_by_id.return_value
    alert = repos["alert_repo"].create.return_value
    price = cached_market_price()

    async def collect(book_id, *, reason):
        assert book_id == book.id
        assert reason == "alert_created"
        assert alert.status == AlertStatus.PAUSED
        assert session.commit.await_count == 1
        assert "Préparation des prix" in message.edit_text.call_args.args[0]
        assert state.data == {}
        state.data = {"name": "Nouveau brouillon"}
        book.market_cache = price
        return price

    patches = patched(repos, MagicMock(), MagicMock())
    for entered in patches:
        entered.start()
    try:
        with patch("app.bot.handlers.alerts_create.price_book_now", new=collect, create=True):
            await save_alert(callback, state, MagicMock(id=uuid.uuid4()), session)
    finally:
        for entered in reversed(patches):
            entered.stop()
    assert alert.status == AlertStatus.ACTIVE
    assert session.commit.await_count == 2
    assert state.data == {"name": "Nouveau brouillon"}
    assert "<b>Prix du marché :</b> 18,00 €" in message.edit_text.call_args.args[0]
    assert "en cours de calcul" not in message.edit_text.call_args.args[0]

"""Les callbacks du brouillon A n'agissent pas sur le brouillon B."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, User

from app.bot.handlers._alerts_router import router


@pytest.mark.parametrize("action", ["alert_create_confirm", "alert_create_cancel"])
async def test_waiting_old_action_cannot_consume_replaced_draft(action, monkeypatch):
    from app.bot import creation_drafts
    from app.bot.states.alert_creation import AlertCreationState

    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=42, user_id=42))
    await state.set_state(AlertCreationState.confirm_creation)
    await state.set_data({"creation_token": "old", "name": "A"})
    callback = CallbackQuery(
        id="synthetic", from_user=User(id=42, is_bot=False, first_name="Test"),
        chat_instance="test", data=f"{action}@old",
    )
    reached = asyncio.Event()

    async def acknowledge(*_args, **_kwargs):
        reached.set()

    monkeypatch.setattr(CallbackQuery, "answer", acknowledge)
    handler = AsyncMock()
    middleware = router.callback_query.outer_middleware[0]
    async with creation_drafts.draft_guard(state):
        task = asyncio.create_task(middleware(handler, callback, {"state": state}))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            assert next(iter(creation_drafts._entries.values())).users == 2
            # Même transition protégée que /add : remplacer sous le verrou détenu.
            await state.set_data({"creation_token": "new", "name": "B"})
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    await asyncio.wait_for(task, 5)
    handler.assert_not_awaited()
    assert await state.get_data() == {"creation_token": "new", "name": "B"}
    assert callback.data == f"{action}@old"
    assert not creation_drafts._entries
    await storage.close()


@pytest.mark.parametrize("action", [
    "alert_create_cancel", "alert_create_confirm", "al_create_edition:0",
    "al_create_work:0", "al_create_edit:name", "cond_toggle:0", "add_cat:tech", "upd_alert_price:",
])
@pytest.mark.parametrize("suffix", ["@old-draft", ""])
async def test_old_creation_action_cannot_reach_handler(action, suffix, monkeypatch):
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=123456789, chat_id=42, user_id=42))
    await state.set_state("new-state")
    await state.set_data({"creation_token": "new-draft", "name": "B"})
    callback = CallbackQuery(
        id="synthetic", from_user=User(id=42, is_bot=False, first_name="Test"),
        chat_instance="test", data=action + suffix,
    ).as_(Bot(token="123456789:AAG_ci_test_dummy_token_abc123"))
    handler = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    middleware = router.callback_query.outer_middleware[0]
    await middleware(handler, callback, {"state": state})
    handler.assert_not_awaited()
    answer.assert_awaited_once()
    assert await state.get_data() == {"creation_token": "new-draft", "name": "B"}
    assert await state.get_state() == "new-state"
    await storage.close()


async def test_late_keyword_result_does_not_change_new_draft_for_same_title(monkeypatch):
    from app.bot.handlers.alerts_create import handle_create_ai_variants, keyword_variants_module

    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=42, user_id=42))
    draft = {"creation_token": "old", "name": "Console", "category_slug": "tech"}
    await state.set_data(draft)

    async def delayed(*_args, **_kwargs):
        await state.set_data({**draft, "creation_token": "new"})
        return SimpleNamespace(answered=True, variants=["ps5"], exclusions=[])

    monkeypatch.setattr(keyword_variants_module, "generate_variants_and_exclusions", delayed)
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    await handle_create_ai_variants(callback, state)
    assert await state.get_data() == {**draft, "creation_token": "new"}
    await storage.close()


async def test_current_creation_action_is_decoded_only_for_handler(monkeypatch):
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=42, user_id=42))
    await state.set_data({"creation_token": "current"})
    callback = CallbackQuery(
        id="synthetic", from_user=User(id=42, is_bot=False, first_name="Test"),
        chat_instance="test", data="alert_create_cancel@current",
    )
    handler = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    middleware = router.callback_query.outer_middleware[0]
    await middleware(handler, callback, {"state": state})
    assert handler.await_args.args[0].data == "alert_create_cancel"
    assert callback.data == "alert_create_cancel@current"
    answer.assert_awaited_once()
    await storage.close()


@pytest.mark.parametrize("raw", ["-1", "99", "invalid", "", "²", "0:extra",
                                 "999999999999999999999999"])
async def test_invalid_condition_index_does_not_modify_draft(raw):
    from app.bot.handlers.alerts_create_confirm import handle_creation_condition_toggle

    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=42, user_id=42))
    await state.set_data({"name": "B", "selected_conditions": ["Bon état"]})
    callback = MagicMock()
    callback.data = f"cond_toggle:{raw}"
    callback.answer = AsyncMock()
    await handle_creation_condition_toggle(callback, state)
    assert await state.get_data() == {"name": "B", "selected_conditions": ["Bon état"]}
    callback.answer.assert_awaited_once_with("Sélection invalide.", show_alert=True)
    await storage.close()


@pytest.mark.parametrize("raw", ["-1", "99", "bad", "", "0:extra"])
async def test_invalid_edit_index_is_rejected_before_repository_access(raw, monkeypatch):
    from app.bot.handlers import alerts_manage

    getter = MagicMock()
    monkeypatch.setattr(alerts_manage, "_get_alert_repo", getter)
    callback = MagicMock()
    callback.data = f"ed_c_tog:00000000-0000-0000-0000-000000000001:{raw}"
    callback.answer = AsyncMock()
    await alerts_manage.handle_edit_condition_toggle(callback, MagicMock(), AsyncMock())
    getter.assert_not_called()
    callback.answer.assert_awaited_once_with("Sélection invalide.", show_alert=True)

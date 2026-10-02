"""Tests unitaires pour l'Auto-Tuning, les variantes orthographiques et le bouton dressing."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.alerts_create import handle_create_ai_variants
from app.bot.handlers.alerts_manage import (
    _active_variant_alerts,
    prompt_new_field_value,
)
from app.bot.handlers.autotune import (
    _active_tuning_alerts,
    _pending_tuning,
    _pending_variants,
    clear_dislikes_for_alert,
    get_disliked_titles_for_alert,
    handle_autotune_command,
    handle_custom_exclusions_input,
    handle_tune_apply,
    handle_tune_back_to_suggestion,
    handle_tune_cancel,
    handle_tune_edit,
    handle_tune_hub_callback,
    handle_var_apply,
    parse_exclusions_input,
    process_autotune_for_alert,
    show_autotune_hub,
)
from app.bot.states.alert_creation import AutoTuneState
from app.db.models.alert import Alert
from app.db.models.user import User
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome

_GEN_PATH = (
    "app.services.ai.modules.keyword_variants."
    "keyword_variants_module.generate_variants_and_exclusions"
)


@pytest.mark.asyncio
async def test_handle_create_ai_variants():
    """Vérifie l'ajout de variantes et d'exclusions dans le flow /add avec indicateur."""
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state = MagicMock(spec=FSMContext)
    state.get_data = AsyncMock(
        return_value={
            "name": "One Piece",
            "include_keywords": ["One Piece"],
            "max_item_price_cents": 3500,
            "category_slug": "bd_comics",
        }
    )
    state.update_data = AsyncMock()

    with patch(
        _GEN_PATH,
        new_callable=AsyncMock,
        return_value=KeywordVariantsOutcome(
            variants=["onepiece", "one piece tomes"],
            exclusions=["figurine", "carte"],
            answered=True,
        ),
    ):
        await handle_create_ai_variants(callback, state)

        state.update_data.assert_called_once()
        updated_data = state.update_data.call_args[1]
        assert "onepiece" in updated_data["variants"]
        assert "one piece tomes" in updated_data["variants"]
        assert "figurine" in updated_data["exclude_keywords"]

        # Doit afficher le message de chargement puis le récapitulatif mis à jour
        assert callback.message.edit_text.call_count == 2
        last_text = callback.message.edit_text.call_args[0][0]
        assert "Variantes :" in last_text
        assert "onepiece" in last_text
        assert "Exclus :" in last_text
        assert "figurine" in last_text


@pytest.mark.asyncio
async def test_handle_create_ai_variants_reports_engine_decision_when_nothing_found():
    """Le moteur a répondu « rien de pertinent » : le récap le dit, et ne change rien."""
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state = MagicMock(spec=FSMContext)
    state.get_data = AsyncMock(
        return_value={
            "name": "One Piece",
            "include_keywords": ["One Piece"],
            "max_item_price_cents": 3500,
            "category_slug": "bd_comics",
        }
    )
    state.update_data = AsyncMock()

    with patch(
        _GEN_PATH,
        new_callable=AsyncMock,
        return_value=KeywordVariantsOutcome(answered=True),
    ):
        await handle_create_ai_variants(callback, state)

    state.update_data.assert_called_once()
    stored = state.update_data.call_args.kwargs
    assert "n'a trouvé aucune variante ni exclusion pertinente" in stored["keyword_note"]
    # Un avis du moteur ne se confond pas avec une panne.
    assert "pas répondu" not in stored["keyword_note"]
    callback.answer.assert_awaited_once()
    assert callback.message.edit_text.call_count == 2
    last_text = callback.message.edit_text.call_args[0][0]
    # La carte ne s'habille pas d'un en-tête de cas particulier : elle porte la note.
    assert last_text.startswith("🔢") or last_text.startswith("📌")
    assert "n'a trouvé aucune variante ni exclusion pertinente" in last_text


@pytest.mark.asyncio
async def test_handle_create_ai_variants_reports_silence_when_engine_failed():
    """Le moteur n'a pas répondu : le récap avoue l'échec au lieu de laisser croire à un avis."""
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state = MagicMock(spec=FSMContext)
    state.get_data = AsyncMock(
        return_value={
            "name": "One Piece",
            "include_keywords": ["One Piece"],
            "max_item_price_cents": 3500,
            "category_slug": "bd_comics",
        }
    )
    state.update_data = AsyncMock()

    with patch(
        _GEN_PATH,
        new_callable=AsyncMock,
        return_value=KeywordVariantsOutcome.unanswered(),
    ):
        await handle_create_ai_variants(callback, state)

    stored = state.update_data.call_args.kwargs
    assert "L'IA n'a pas répondu à temps" in stored["keyword_note"]
    last_text = callback.message.edit_text.call_args[0][0]
    assert "L'IA n'a pas répondu à temps" in last_text
    assert "Aucune modification" not in last_text


@pytest.mark.asyncio
async def test_handle_autotune_no_dislikes():
    """Vérifie le message d'aide si aucun faux positif n'a encore été signalé."""
    message = MagicMock(spec=Message)
    message.text = "/autotune"
    message.answer = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()

    mock_dislikes = patch(
        "app.bot.handlers.autotune.get_alerts_with_dislikes",
        new_callable=AsyncMock,
        return_value=[],
    )
    with mock_dislikes:
        await handle_autotune_command(message, db_user, db_session)

        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "Aucun faux positif signalé" in text


@pytest.mark.asyncio
async def test_handle_autotune_with_dislikes():
    """Vérifie la suggestion d'exclusions par DeepSeek pour une alerte avec dislikes."""
    message = MagicMock(spec=Message)
    message.text = "/autotune"
    message.answer = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()

    alert = Alert(
        id=uuid.uuid4(),
        name="One Piece",
        user_id=db_user.id,
        exclude_keywords=["poster"],
    )

    disliked_titles = ["Figurine One Piece Luffy", "T-shirt One Piece taille M"]

    with (
        patch(
            "app.bot.handlers.autotune.get_alerts_with_dislikes",
            new_callable=AsyncMock,
            return_value=[(alert, 2)],
        ),
        patch(
            "app.bot.handlers.autotune.get_disliked_titles_for_alert",
            new_callable=AsyncMock,
            return_value=disliked_titles,
        ),
        patch(
            "app.services.ai.modules.auto_tuning.auto_tuning_module.suggest_exclusions",
            new_callable=AsyncMock,
            return_value=(["figurine", "t-shirt"], "Termes parasites fréquents"),
        ),
    ):
        await handle_autotune_command(message, db_user, db_session)

        # Doit envoyer le message de chargement puis le résultat final
        assert message.answer.call_count == 2
        text = message.answer.call_args[0][0]
        assert "Auto-Tuning : « One Piece »" in text
        assert "figurine, t-shirt" in text
        assert str(alert.id) in _pending_tuning


@pytest.mark.asyncio
async def test_handle_autotune_multiple_alerts_menu():
    """Vérifie l'affichage du menu inline si plusieurs alertes ont des faux positifs."""
    message = MagicMock(spec=Message)
    message.text = "/autotune"
    message.answer = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()

    alert1 = Alert(id=uuid.uuid4(), name="Watchmen", user_id=db_user.id)
    alert2 = Alert(id=uuid.uuid4(), name="Persepolis", user_id=db_user.id)

    with patch(
        "app.bot.handlers.autotune.get_alerts_with_dislikes",
        new_callable=AsyncMock,
        return_value=[(alert1, 1), (alert2, 3)],
    ):
        await handle_autotune_command(message, db_user, db_session)

        message.answer.assert_called_once()
        text, kwargs = message.answer.call_args[0][0], message.answer.call_args[1]
        assert "Sélectionnez l'alerte à analyser" in text
        kb = kwargs["reply_markup"]
        assert len(kb.inline_keyboard) == 3
        assert "Watchmen (1 faux positif)" in kb.inline_keyboard[0][0].text
        assert "Persepolis (3 faux positifs)" in kb.inline_keyboard[1][0].text


@pytest.mark.asyncio
async def test_handle_autotune_with_target_query():
    """Vérifie le ciblage direct par argument (/autotune watchmen) sans limite de 10 alertes."""
    message = MagicMock(spec=Message)
    message.text = "/autotune watchmen"
    message.answer = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()

    alert = Alert(id=uuid.uuid4(), name="Watchmen", user_id=db_user.id)

    with (
        patch(
            "app.bot.handlers.autotune.AlertRepository.search_by_user",
            new_callable=AsyncMock,
            return_value=[alert],
        ),
        patch(
            "app.bot.handlers.autotune.get_disliked_titles_for_alert",
            new_callable=AsyncMock,
            return_value=["Watchmen les gardiens 6 tomes"],
        ),
        patch(
            "app.services.ai.modules.auto_tuning.auto_tuning_module.suggest_exclusions",
            new_callable=AsyncMock,
            return_value=(["gardiens"], "BD alternative"),
        ),
    ):
        await handle_autotune_command(message, db_user, db_session)

        assert message.answer.call_count == 2
        text = message.answer.call_args[0][0]
        assert "Auto-Tuning : « Watchmen »" in text
        assert "gardiens" in text


@pytest.mark.asyncio
async def test_handle_tune_apply():
    """Vérifie l'application effective des exclusions et le nettoyage des faux positifs."""
    alert_id = uuid.uuid4()
    _pending_tuning[str(alert_id)] = ["figurine", "t-shirt"]

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_tune_apply:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    db_session.commit = AsyncMock()

    alert = Alert(
        id=alert_id,
        name="One Piece",
        user_id=db_user.id,
        exclude_keywords=["poster"],
    )

    with (
        patch(
            "app.bot.handlers.autotune.AlertRepository.get_by_id",
            new_callable=AsyncMock,
            return_value=alert,
        ),
        patch(
            "app.bot.handlers.autotune.clear_dislikes_for_alert",
            new_callable=AsyncMock,
            return_value=2,
        ) as mock_clear,
    ):
        await handle_tune_apply(callback, db_user, db_session)

        assert "poster" in alert.exclude_keywords
        assert "figurine" in alert.exclude_keywords
        assert "t-shirt" in alert.exclude_keywords
        mock_clear.assert_called_once_with(alert.id, db_session)
        db_session.commit.assert_called_once()

        callback.answer.assert_called_once()
        callback.message.edit_text.assert_called_once()
        text = callback.message.edit_text.call_args[0][0]
        assert "retiré(s) de la liste" in text
        # Vérifie les boutons de retour : hub prioritaire et alerte secondaire
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == "ai_tune_hub"
        assert kb.inline_keyboard[1][0].callback_data == f"al_edit:{alert.id}"


@pytest.mark.asyncio
async def test_handle_var_apply():
    """Vérifie l'application effective des variantes orthographiques à alert.variants."""
    alert_id = uuid.uuid4()
    _pending_variants[str(alert_id)] = ["onepiece", "one piece tomes"]

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_var_apply:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    db_session.commit = AsyncMock()

    alert = Alert(
        id=alert_id,
        name="One Piece",
        user_id=db_user.id,
        include_keywords=["One Piece"],
        variants=[],
    )

    with patch(
        "app.bot.handlers.autotune.AlertRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=alert,
    ):
        await handle_var_apply(callback, db_user, db_session)

        # Les mots-clés obligatoires restent intacts
        assert alert.include_keywords == ["One Piece"]
        # Les variantes sont ajoutées à alert.variants
        assert "onepiece" in alert.variants
        assert "one piece tomes" in alert.variants
        db_session.commit.assert_called_once()

        callback.answer.assert_called_once()
        callback.message.edit_text.assert_called_once()
        # Bouton retour vers l'alerte
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == f"al_edit:{alert.id}"


@pytest.mark.asyncio
async def test_autotune_concurrency_lock():
    """Vérifie que deux clics simultanés sur Auto-Tuning pour la même alerte sont verrouillés."""
    alert_id = uuid.uuid4()
    alert = Alert(id=alert_id, name="Test Lock", user_id=uuid.uuid4())

    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()

    # Simuler une analyse déjà en cours
    _active_tuning_alerts.add(str(alert_id))
    try:
        await process_autotune_for_alert(callback, alert, MagicMock())
        lock_msg = "⏳ Une analyse Auto-Tuning est déjà en cours..."
        callback.answer.assert_called_once_with(lock_msg, show_alert=True)
    finally:
        _active_tuning_alerts.discard(str(alert_id))


@pytest.mark.asyncio
async def test_variants_concurrency_lock():
    """Vérifie que deux clics simultanés sur Suggérer variantes IA sont verrouillés."""
    alert_id = uuid.uuid4()
    user_id = uuid.uuid4()
    alert = Alert(id=alert_id, name="Test Lock", user_id=user_id)
    db_user = User(id=user_id, telegram_user_id=123456)

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ed_f:{alert_id}:ai_variants"
    callback.answer = AsyncMock()

    state = MagicMock(spec=FSMContext)
    state.clear = AsyncMock()

    _active_variant_alerts.add(str(alert_id))
    try:
        with patch("app.bot.handlers.alerts_manage._get_alert_repo") as mock_repo:
            mock_repo.return_value.get_by_id = AsyncMock(return_value=alert)
            await prompt_new_field_value(callback, state, db_user, MagicMock())
            lock_msg = "⏳ Recherche déjà en cours..."
            callback.answer.assert_called_once_with(lock_msg, show_alert=True)
    finally:
        _active_variant_alerts.discard(str(alert_id))


@pytest.mark.asyncio
async def test_process_autotune_zero_dislikes_callback():
    """Vérifie le message pédagogique et le bouton retour quand 0 faux positif n'est présent."""
    alert = Alert(id=uuid.uuid4(), name="Gunnar le vampire", user_id=uuid.uuid4())

    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    with patch(
        "app.bot.handlers.autotune.get_disliked_titles_for_alert",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await process_autotune_for_alert(callback, alert, MagicMock())

        assert callback.message.edit_text.call_count == 2
        last_call_args = callback.message.edit_text.call_args
        last_text = last_call_args[0][0]
        assert "Aucun faux positif enregistré pour cette alerte" in last_text
        assert "[👎 Faux positif]" in last_text
        kb = last_call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == "ai_tune_hub"
        assert kb.inline_keyboard[1][0].callback_data == f"al_edit:{alert.id}"


@pytest.mark.asyncio
async def test_process_autotune_all_exclusions_already_active():
    """Vérifie la purge automatique des faux positifs déjà couverts par les exclusions."""
    alert = Alert(
        id=uuid.uuid4(),
        name="Watchmen",
        user_id=uuid.uuid4(),
        exclude_keywords=["figurine", "poster"],
    )

    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()

    with (
        patch(
            "app.bot.handlers.autotune.get_disliked_titles_for_alert",
            new_callable=AsyncMock,
            return_value=["Figurine Watchmen Rorschach"],
        ),
        patch(
            "app.services.ai.modules.auto_tuning.auto_tuning_module.suggest_exclusions",
            new_callable=AsyncMock,
            return_value=(["figurine"], "Produit dérivé"),
        ),
        patch(
            "app.bot.handlers.autotune.clear_dislikes_for_alert",
            new_callable=AsyncMock,
            return_value=1,
        ) as mock_clear,
    ):
        await process_autotune_for_alert(callback, alert, mock_db)

        mock_clear.assert_called_once_with(alert.id, mock_db)
        mock_db.commit.assert_called_once()

        assert callback.message.edit_text.call_count == 2
        last_call_args = callback.message.edit_text.call_args
        last_text = last_call_args[0][0]
        assert "Toutes les exclusions pertinentes" in last_text
        assert "Figurine Watchmen Rorschach" in last_text
        assert "figurine, poster" in last_text
        assert "retirés de la liste" in last_text
        kb = last_call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == "ai_tune_hub"
        assert kb.inline_keyboard[1][0].callback_data == f"al_edit:{alert.id}"


@pytest.mark.asyncio
async def test_get_disliked_titles_for_alert_query():
    """Vérifie que get_disliked_titles_for_alert exécute la requête SQL attendue."""
    db_session = MagicMock()
    mock_result = MagicMock()
    mock_result.all.return_value = [("Watchmen tome 1",), ("Watchmen les gardiens",)]
    db_session.execute = AsyncMock(return_value=mock_result)

    alert_id = uuid.uuid4()
    titles = await get_disliked_titles_for_alert(alert_id, db_session)

    assert titles == ["Watchmen tome 1", "Watchmen les gardiens"]
    db_session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_clear_dislikes_for_alert_query():
    """Vérifie que clear_dislikes_for_alert exécute le delete SQL attendu."""
    db_session = MagicMock()
    mock_result = MagicMock()
    mock_result.rowcount = 4
    db_session.execute = AsyncMock(return_value=mock_result)

    alert_id = uuid.uuid4()
    count = await clear_dislikes_for_alert(alert_id, db_session)

    assert count == 4
    db_session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_handle_tune_cancel_preserves_dislikes():
    """Vérifie que l'annulation ne supprime pas les faux positifs et propose le retour hub."""
    alert_id = uuid.uuid4()
    _pending_tuning[str(alert_id)] = ["terme_inutile"]

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_tune_cancel:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()

    with patch(
        "app.bot.handlers.autotune.clear_dislikes_for_alert",
        new_callable=AsyncMock,
    ) as mock_clear:
        await handle_tune_cancel(callback, db_user, db_session)

        # Les faux positifs ne sont PAS supprimés lors d'une annulation
        mock_clear.assert_not_called()
        assert str(alert_id) not in _pending_tuning
        callback.answer.assert_called_once_with("Opération annulée.")
        callback.message.edit_text.assert_called_once()
        text = callback.message.edit_text.call_args[0][0]
        assert "conservés" in text
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == "ai_tune_hub"
        assert kb.inline_keyboard[1][0].callback_data == f"al_edit:{alert_id}"


@pytest.mark.asyncio
async def test_show_autotune_hub():
    """Vérifie l'affichage du hub avec liste mise à jour ou écran propre."""
    callback = MagicMock(spec=CallbackQuery)
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    db_session = MagicMock()
    user_id = uuid.uuid4()

    alert1 = Alert(id=uuid.uuid4(), name="Persepolis", user_id=user_id)

    # 1. Cas avec alertes restantes
    with patch(
        "app.bot.handlers.autotune.get_alerts_with_dislikes",
        new_callable=AsyncMock,
        return_value=[(alert1, 1)],
    ):
        await show_autotune_hub(callback, user_id, db_session)
        callback.message.edit_text.assert_called_once()
        text = callback.message.edit_text.call_args[0][0]
        assert "Sélectionnez l'alerte à analyser" in text
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == f"ai_tune_select:{alert1.id}"
        assert kb.inline_keyboard[1][0].callback_data == "ai_tune_close"

    # 2. Cas où toutes les alertes ont été traitées
    callback.message.edit_text.reset_mock()
    with patch(
        "app.bot.handlers.autotune.get_alerts_with_dislikes",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await show_autotune_hub(callback, user_id, db_session)
        callback.message.edit_text.assert_called_once()
        text = callback.message.edit_text.call_args[0][0]
        assert "Toutes vos alertes sont propres" in text
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == "ai_tune_close"


@pytest.mark.asyncio
async def test_handle_tune_hub_callback():
    """Vérifie que le callback ai_tune_hub appelle show_autotune_hub."""
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()

    with patch(
        "app.bot.handlers.autotune.show_autotune_hub",
        new_callable=AsyncMock,
    ) as mock_show:
        await handle_tune_hub_callback(callback, db_user, db_session)
        callback.answer.assert_called_once()
        mock_show.assert_called_once_with(callback, db_user.id, db_session)


def test_parse_exclusions_input():
    """Vérifie le découpage, nettoyage et déduplication des exclusions personnalisées."""
    raw = "- figurine, tome 1; • coffret\nposter, FIGURINE,  "
    parsed = parse_exclusions_input(raw)
    assert parsed == ["figurine", "tome 1", "coffret", "poster"]

    assert parse_exclusions_input("") == []
    assert parse_exclusions_input("   \n\n  ") == []


@pytest.mark.asyncio
async def test_handle_tune_edit():
    """Vérifie le passage en état d'édition avec proposition copiable."""
    alert_id = uuid.uuid4()
    _pending_tuning[str(alert_id)] = ["figurine", "tome 1"]

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_tune_edit:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    alert = Alert(id=alert_id, name="One Piece", user_id=db_user.id)

    state = MagicMock(spec=FSMContext)
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()

    with patch(
        "app.bot.handlers.autotune.AlertRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=alert,
    ):
        await handle_tune_edit(callback, db_user, db_session, state)

        state.set_state.assert_called_once_with(AutoTuneState.waiting_for_custom_exclusions)
        state.update_data.assert_called_once_with(tuning_alert_id=str(alert.id))
        callback.message.edit_text.assert_called_once()
        text = callback.message.edit_text.call_args[0][0]
        assert "Personnaliser les exclusions" in text
        assert "<code>figurine, tome 1</code>" in text
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == f"ai_tune_back_sug:{alert.id}"


@pytest.mark.asyncio
async def test_handle_tune_back_to_suggestion():
    """Vérifie le retour à la vue suggestion sans modifier les exclusions."""
    alert_id = uuid.uuid4()
    _pending_tuning[str(alert_id)] = ["figurine", "tome 1"]

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_tune_back_sug:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    alert = Alert(id=alert_id, name="One Piece", user_id=db_user.id)

    state = MagicMock(spec=FSMContext)
    state.clear = AsyncMock()

    with patch(
        "app.bot.handlers.autotune.AlertRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=alert,
    ):
        await handle_tune_back_to_suggestion(callback, db_user, db_session, state)

        state.clear.assert_called_once()
        callback.message.edit_text.assert_called_once()
        text = callback.message.edit_text.call_args[0][0]
        assert "Nouvelles exclusions suggérées :" in text
        assert "figurine, tome 1" in text
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == f"ai_tune_apply:{alert.id}"
        assert kb.inline_keyboard[1][0].callback_data == f"ai_tune_edit:{alert.id}"
        assert kb.inline_keyboard[2][0].callback_data == f"ai_tune_cancel:{alert.id}"


@pytest.mark.asyncio
async def test_handle_custom_exclusions_input_valid():
    """Vérifie la saisie valide d'exclusions personnalisées et l'affichage du récapitulatif."""
    alert_id = uuid.uuid4()
    _pending_tuning[str(alert_id)] = ["ancienne_suggestion"]

    message = MagicMock(spec=Message)
    message.text = "figurine, tome 2; coffret"
    message.answer = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    alert = Alert(id=alert_id, name="One Piece", user_id=db_user.id)

    state = MagicMock(spec=FSMContext)
    state.get_data = AsyncMock(return_value={"tuning_alert_id": str(alert_id)})
    state.clear = AsyncMock()

    with patch(
        "app.bot.handlers.autotune.AlertRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=alert,
    ):
        await handle_custom_exclusions_input(message, db_user, db_session, state)

        assert _pending_tuning[str(alert_id)] == ["figurine", "tome 2", "coffret"]
        state.clear.assert_called_once()
        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "Nouvelles exclusions à appliquer (personnalisées) :" in text
        assert "figurine, tome 2, coffret" in text
        kb = message.answer.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == f"ai_tune_apply:{alert_id}"
        assert kb.inline_keyboard[1][0].callback_data == f"ai_tune_edit:{alert_id}"
        assert kb.inline_keyboard[2][0].callback_data == f"ai_tune_cancel:{alert_id}"


@pytest.mark.asyncio
async def test_handle_custom_exclusions_input_cancel():
    """Vérifie l'annulation par commande /cancel lors de la saisie."""
    alert_id = uuid.uuid4()
    _pending_tuning[str(alert_id)] = ["suggestion"]

    message = MagicMock(spec=Message)
    message.text = "/cancel"
    message.answer = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()

    state = MagicMock(spec=FSMContext)
    state.get_data = AsyncMock(return_value={"tuning_alert_id": str(alert_id)})
    state.clear = AsyncMock()

    await handle_custom_exclusions_input(message, db_user, db_session, state)

    state.clear.assert_called_once()
    assert str(alert_id) not in _pending_tuning
    message.answer.assert_called_once()
    assert "Modification annulée" in message.answer.call_args[0][0]

"""Tests unitaires pour l'Auto-Tuning, les variantes orthographiques et le bouton dressing."""

import uuid
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.alerts_create import handle_create_ai_variants
from app.bot.handlers.alerts_helpers import RECAP_TITLE
from app.bot.handlers.alerts_manage import (
    _active_variant_alerts,
    prompt_new_field_value,
)
from app.bot.handlers.autotune import (
    _active_tuning_alerts,
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
    save_pending_variants,
    show_autotune_hub,
)
from app.bot.states.alert_creation import AutoTuneState
from app.db.models.alert import Alert
from app.db.models.user import User
from app.services.ai.modules.auto_tuning import (
    build_exclusion_proposal,
    get_pending_tuning,
    save_pending_tuning,
)
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from tests.fakes_llm import FakeLLM, FlakyLLM

_GEN_PATH = (
    "app.services.ai.modules.keyword_variants."
    "keyword_variants_module.generate_variants_and_exclusions"
)


async def _run_creation_keyword_retry(callback, state, *, client=None):
    from tests.add_journey import defer_task, drain

    tasks = []
    with (
        patch("app.services.ai.modules.keyword_variants.KeywordVariantsModule.is_enabled",
              new_callable=PropertyMock, return_value=True),
        patch("app.services.ai.modules.keyword_variants.keyword_variants_module.client",
              client or MagicMock(is_configured=True)),
        patch("app.bot.handlers.alerts_create.safe_create_task", defer_task(tasks)),
    ):
        await handle_create_ai_variants(callback, state)
        await drain(tasks)


@pytest.mark.asyncio
async def test_handle_create_ai_variants():
    """Vérifie l'ajout de variantes et d'exclusions dans le flow /add avec indicateur."""
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from tests.add_fakes import DraftState

    state = DraftState()
    await state.update_data(
        name="One Piece", include_keywords=["One Piece"], max_item_price_cents=3500,
        category_slug="bd_comics", keyword_analysis_status="failed",
    )
    await state.set_state("AlertCreationState:confirm_creation")

    with patch(
        _GEN_PATH,
        new_callable=AsyncMock,
        return_value=KeywordVariantsOutcome(
            variants=["onepiece", "one piece tomes"],
            exclusions=["figurine", "carte"],
            answered=True,
        ),
    ):
        await _run_creation_keyword_retry(callback, state)

        updated_data = state.data
        assert "onepiece" in updated_data["variants"]
        # « one piece tomes » répète le titre : le nettoyeur l'écarte, comme en amont.
        assert "one piece tomes" not in updated_data["variants"]
        assert "figurine" in updated_data["exclude_keywords"]

        assert callback.message.edit_text.call_count == 2
        last_text = callback.message.edit_text.call_args[0][0]
        assert "Variantes :" in last_text
        assert "onepiece" in last_text
        assert "Mots-clés exclus :" in last_text
        assert "figurine" in last_text


@pytest.mark.asyncio
async def test_handle_create_ai_variants_reports_engine_decision_when_nothing_found():
    """Le moteur a répondu « rien de pertinent » : le récap le dit, et ne change rien."""
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from tests.add_fakes import DraftState

    state = DraftState()
    await state.update_data(
        name="One Piece", include_keywords=["One Piece"], max_item_price_cents=3500,
        category_slug="bd_comics", keyword_analysis_status="failed",
    )
    await state.set_state("AlertCreationState:confirm_creation")

    with patch(
        _GEN_PATH,
        new_callable=AsyncMock,
        return_value=KeywordVariantsOutcome(answered=True),
    ):
        await _run_creation_keyword_retry(callback, state)

    stored = state.data
    assert "Aucune proposition supplémentaire pertinente retenue." in stored["keyword_note"]
    # Un avis du moteur ne se confond pas avec une panne.
    assert "pas répondu" not in stored["keyword_note"]
    callback.answer.assert_awaited_once()
    assert callback.message.edit_text.call_count == 2
    last_text = callback.message.edit_text.call_args[0][0]
    # La carte garde un titre unique et porte la note, sans en-tête de cas particulier.
    assert last_text.startswith(RECAP_TITLE)
    assert "Aucune proposition supplémentaire pertinente retenue." in last_text


@pytest.mark.asyncio
async def test_handle_create_ai_variants_reports_silence_when_engine_failed():
    """Le moteur n'a pas répondu : le récap avoue l'échec au lieu de laisser croire à un avis."""
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from tests.add_fakes import DraftState

    state = DraftState()
    await state.update_data(
        name="One Piece", include_keywords=["One Piece"], max_item_price_cents=3500,
        category_slug="bd_comics", keyword_analysis_status="failed",
    )
    await state.set_state("AlertCreationState:confirm_creation")

    with patch(
        _GEN_PATH,
        new_callable=AsyncMock,
        return_value=KeywordVariantsOutcome.unanswered(),
    ):
        await _run_creation_keyword_retry(callback, state)

    stored = state.data
    assert "L'IA n'a pas répondu" in stored["keyword_note"]
    # Le conseil nomme le bouton sous la carte, pas un détour par le menu de modification.
    assert "via « Modifier »" not in stored["keyword_note"]
    # Une recherche échouée n'est pas définitive : le bouton de relance reste.
    assert stored["keywords_finalized"] is False

    last_text = callback.message.edit_text.call_args[0][0]
    assert "L'IA n'a pas répondu" in last_text
    assert "Aucune modification" not in last_text

    keyboard = callback.message.edit_text.call_args[1]["reply_markup"]
    assert any(
        button.callback_data == "alert_create_ai_variants"
        for row in keyboard.inline_keyboard
        for button in row
    ), "le bouton de relance a disparu alors que le moteur n'a pas répondu"


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
    """Vérifie la suggestion d'exclusions par le moteur pour une alerte avec dislikes."""
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

        assert message.answer.call_count == 2
        text = message.answer.call_args[0][0]
        assert "🧹 Exclusions proposées" in text
        assert "Nom : « One Piece »" in text
        assert "figurine, t-shirt" in text
        assert await get_pending_tuning(alert.id) == ["figurine", "t-shirt"]


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
        assert "Sélectionne l'alerte à analyser" in text
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
        assert "🧹 Exclusions proposées" in text
        assert "Nom : « Watchmen »" in text
        assert "gardiens" in text


@pytest.mark.asyncio
async def test_build_exclusion_proposal_keeps_only_new_exclusions():
    """La règle partagée ne repropose jamais ce que l'alerte exclut déjà."""
    with patch(
        "app.services.ai.modules.auto_tuning.auto_tuning_module.suggest_exclusions",
        new_callable=AsyncMock,
        return_value=(["Figurine", "t-shirt", "cassette"], "Parasites détectés"),
    ):
        proposal = await build_exclusion_proposal(
            alert_name="One Piece",
            already_excluded=["figurine"],
            disliked_titles=["Figurine Luffy", "T-shirt One Piece", "Cassette VHS"],
        )

    assert proposal is not None
    # La casse est indifférenciée, l'ordre du moteur est conservé.
    assert proposal.exclusions == ["t-shirt", "cassette"]
    assert proposal.explanation == "Parasites détectés"


@pytest.mark.asyncio
async def test_build_exclusion_proposal_returns_empty_proposal_without_new_exclusions():
    with patch(
        "app.services.ai.modules.auto_tuning.auto_tuning_module.suggest_exclusions",
        new_callable=AsyncMock,
        return_value=(["figurine"], "déjà exclue"),
    ):
        proposal = await build_exclusion_proposal(
            alert_name="One Piece",
            already_excluded=["Figurine"],
            disliked_titles=["Figurine Luffy", "Poster One Piece", "Figurine Zoro"],
        )

    assert proposal is not None
    assert proposal.exclusions == []


@pytest.mark.asyncio
async def test_build_exclusion_proposal_does_not_call_engine_below_threshold():
    """Trop peu de faux positifs : le moteur n'est même pas sollicité."""
    mock_engine = AsyncMock(return_value=(["figurine"], None))

    with patch(
        "app.services.ai.modules.auto_tuning.auto_tuning_module.suggest_exclusions",
        new_callable=lambda: mock_engine,
    ):
        proposal = await build_exclusion_proposal(
            alert_name="One Piece",
            already_excluded=[],
            disliked_titles=["Figurine Luffy"],
        )

    assert proposal is None
    mock_engine.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_exclusion_proposal_waits_for_the_third_signal():
    """Deux faux positifs ne suffisent pas : le seuil partagé est trois."""
    mock_engine = AsyncMock(return_value=(["figurine"], None))

    with patch(
        "app.services.ai.modules.auto_tuning.auto_tuning_module.suggest_exclusions",
        new_callable=lambda: mock_engine,
    ):
        proposal = await build_exclusion_proposal(
            alert_name="One Piece",
            already_excluded=[],
            disliked_titles=["Figurine Luffy", "Poster One Piece"],
        )

    assert proposal is None
    mock_engine.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_tune_apply():
    """Vérifie l'application effective des exclusions et le nettoyage des faux positifs."""
    alert_id = uuid.uuid4()
    await save_pending_tuning(alert_id, ["figurine", "t-shirt"])

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
        assert "2 faux positifs traités retirés de la liste" in text
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == "ai_tune_hub"
        assert kb.inline_keyboard[1][0].callback_data == f"al_edit:{alert.id}"


@pytest.mark.asyncio
async def test_handle_tune_apply_keeps_suggestion_when_alert_missing():
    """Alerte introuvable : la suggestion n'est pas consommée, l'utilisateur peut réessayer."""
    alert_id = uuid.uuid4()
    await save_pending_tuning(alert_id, ["figurine", "t-shirt"])

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_tune_apply:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    db_session.commit = AsyncMock()

    with patch(
        "app.bot.handlers.autotune.AlertRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "app.bot.handlers.autotune.clear_dislikes_for_alert",
        new_callable=AsyncMock,
    ) as mock_clear:
        await handle_tune_apply(callback, db_user, db_session)

        callback.answer.assert_awaited_once_with("Alerte introuvable.", show_alert=True)
        mock_clear.assert_not_called()
        db_session.commit.assert_not_called()
        # La suggestion survit à l'échec : rien ne l'a consommée.
        assert await get_pending_tuning(alert_id) == ["figurine", "t-shirt"]


@pytest.mark.asyncio
async def test_handle_var_apply():
    """Vérifie l'application effective des variantes orthographiques à alert.variants."""
    alert_id = uuid.uuid4()
    save_pending_variants(alert_id, ["onepiece", "one piece tomes"])

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

        assert alert.include_keywords == ["One Piece"]
        # Celle qui répète le titre est écartée, comme le fait le module en amont.
        assert "onepiece" in alert.variants
        assert "one piece tomes" not in alert.variants
        db_session.commit.assert_called_once()

        callback.answer.assert_called_once()
        callback.message.edit_text.assert_called_once()
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == f"al_edit:{alert.id}"


@pytest.mark.asyncio
async def test_autotune_concurrency_lock():
    """Vérifie que deux clics simultanés sur Auto-Tuning pour la même alerte sont verrouillés."""
    alert_id = uuid.uuid4()
    alert = Alert(id=alert_id, name="Test Lock", user_id=uuid.uuid4())

    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()

    _active_tuning_alerts.add(str(alert_id))
    try:
        await process_autotune_for_alert(callback, alert, MagicMock())
        lock_msg = "Une analyse est déjà en cours pour cette alerte."
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
    state.storage = MemoryStorage()
    state.key = StorageKey(bot_id=1, chat_id=42, user_id=42)
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
        assert "Faux positif" in last_text
        kb = last_call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == "ai_tune_hub"
        assert kb.inline_keyboard[1][0].callback_data == f"al_edit:{alert.id}"


@pytest.mark.asyncio
async def test_process_autotune_all_exclusions_already_active_preserves_dislikes():
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

        mock_clear.assert_not_awaited()
        mock_db.commit.assert_not_awaited()

        assert callback.message.edit_text.call_count == 2
        last_call_args = callback.message.edit_text.call_args
        last_text = last_call_args[0][0]
        assert "aucune nouvelle exclusion" in last_text
        assert "Figurine Watchmen Rorschach" in last_text
        assert "figurine, poster" in last_text
        assert "retirés de la liste" not in last_text
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
    await save_pending_tuning(alert_id, ["terme_inutile"])

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

        # Une annulation ne supprime jamais les faux positifs.
        mock_clear.assert_not_called()
        assert await get_pending_tuning(alert_id) == []
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

    with patch(
        "app.bot.handlers.autotune.get_alerts_with_dislikes",
        new_callable=AsyncMock,
        return_value=[(alert1, 1)],
    ):
        await show_autotune_hub(callback, user_id, db_session)
        callback.message.edit_text.assert_called_once()
        text = callback.message.edit_text.call_args[0][0]
        assert "Sélectionne l'alerte à analyser" in text
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == f"ai_tune_select:{alert1.id}"
        assert kb.inline_keyboard[1][0].callback_data == "ai_tune_close"

    callback.message.edit_text.reset_mock()
    with patch(
        "app.bot.handlers.autotune.get_alerts_with_dislikes",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await show_autotune_hub(callback, user_id, db_session)
        callback.message.edit_text.assert_called_once()
        text = callback.message.edit_text.call_args[0][0]
        assert "Aucun faux positif signalé pour le moment" in text
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
    await save_pending_tuning(alert_id, ["figurine", "tome 1"])

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_tune_edit:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    alert = Alert(id=alert_id, name="One Piece", user_id=db_user.id)

    state = MagicMock(spec=FSMContext)
    state.storage = MemoryStorage()
    state.key = StorageKey(bot_id=1, chat_id=42, user_id=42)
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
        assert "Mots-clés exclus" in text
        assert "<code>figurine, tome 1</code>" in text
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == f"ai_tune_back_sug:{alert.id}"


@pytest.mark.asyncio
async def test_handle_tune_back_to_suggestion():
    """Vérifie le retour à la vue suggestion sans modifier les exclusions."""
    alert_id = uuid.uuid4()
    await save_pending_tuning(alert_id, ["figurine", "tome 1"])

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_tune_back_sug:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    alert = Alert(id=alert_id, name="One Piece", user_id=db_user.id)

    state = MagicMock(spec=FSMContext)
    state.storage = MemoryStorage()
    state.key = StorageKey(bot_id=1, chat_id=42, user_id=42)
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
        assert "Mots-clés exclus suggérés :" in text
        assert "figurine, tome 1" in text
        kb = callback.message.edit_text.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == f"ai_tune_apply:{alert.id}"
        assert kb.inline_keyboard[1][0].callback_data == f"ai_tune_edit:{alert.id}"
        assert kb.inline_keyboard[2][0].callback_data == f"ai_tune_cancel:{alert.id}"


@pytest.mark.asyncio
async def test_handle_custom_exclusions_input_valid():
    """Vérifie la saisie valide d'exclusions personnalisées et l'affichage du récapitulatif."""
    alert_id = uuid.uuid4()
    await save_pending_tuning(alert_id, ["ancienne_suggestion"])

    message = MagicMock(spec=Message)
    message.text = "figurine, tome 2; coffret"
    message.answer = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    alert = Alert(id=alert_id, name="One Piece", user_id=db_user.id)

    state = MagicMock(spec=FSMContext)
    state.storage = MemoryStorage()
    state.key = StorageKey(bot_id=1, chat_id=42, user_id=42)
    state.get_data = AsyncMock(return_value={"tuning_alert_id": str(alert_id)})
    state.clear = AsyncMock()

    with patch(
        "app.bot.handlers.autotune.AlertRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=alert,
    ):
        await handle_custom_exclusions_input(message, db_user, db_session, state)

        assert await get_pending_tuning(alert_id) == ["figurine", "tome 2", "coffret"]
        state.clear.assert_called_once()
        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "Mots-clés exclus suggérés (personnalisés) :" in text
        assert "figurine, tome 2, coffret" in text
        kb = message.answer.call_args[1]["reply_markup"]
        assert kb.inline_keyboard[0][0].callback_data == f"ai_tune_apply:{alert_id}"
        assert kb.inline_keyboard[1][0].callback_data == f"ai_tune_edit:{alert_id}"
        assert kb.inline_keyboard[2][0].callback_data == f"ai_tune_cancel:{alert_id}"


@pytest.mark.asyncio
async def test_edited_exclusions_are_applied_verbatim_and_confirmed():
    """La saisie éditée remplace la suggestion et rejoint les exclusions existantes.

    Le cas observé : cinq exclusions déjà enregistrées, quatre suggestions, puis une
    saisie `pacush, monstre, tome`. La fusion ne doit ni réutiliser l'ancienne
    suggestion ni tronquer les termes ajoutés au plafond des propositions.
    """
    alert_id = uuid.uuid4()
    await save_pending_tuning(alert_id, ["pacush", "ptiluc", "mythe", "relecture"])

    existing = ["junji ito", "mary shelley roman", "poche", "père", "manga"]
    alert = Alert(
        id=alert_id,
        name="Frankenstein",
        user_id=uuid.uuid4(),
        exclude_keywords=list(existing),
    )
    db_user = User(id=alert.user_id, telegram_user_id=123456)
    db_session = MagicMock()
    db_session.commit = AsyncMock()

    message = MagicMock(spec=Message)
    message.text = "pacush, monstre\ntome"
    message.answer = AsyncMock()
    state = MagicMock(spec=FSMContext)
    state.storage = MemoryStorage()
    state.key = StorageKey(bot_id=1, chat_id=42, user_id=42)
    state.get_data = AsyncMock(return_value={"tuning_alert_id": str(alert_id)})
    state.clear = AsyncMock()

    with patch(
        "app.bot.handlers.autotune.AlertRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=alert,
    ):
        await handle_custom_exclusions_input(message, db_user, db_session, state)

    assert await get_pending_tuning(alert_id) == ["pacush", "monstre", "tome"]

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_tune_apply:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    with (
        patch(
            "app.bot.handlers.autotune.AlertRepository.get_by_id",
            new_callable=AsyncMock,
            return_value=alert,
        ),
        patch(
            "app.bot.handlers.autotune.clear_dislikes_for_alert",
            new_callable=AsyncMock,
            return_value=3,
        ),
    ):
        await handle_tune_apply(callback, db_user, db_session)

    assert alert.exclude_keywords == [*existing, "pacush", "monstre", "tome"]
    confirmation = callback.message.edit_text.call_args[0][0]
    assert "Exclusions appliquées" in confirmation
    assert "monstre" in confirmation
    assert "tome" in confirmation
    assert "ptiluc" not in confirmation
    assert "3 faux positifs traités retirés de la liste" in confirmation


@pytest.mark.asyncio
async def test_handle_custom_exclusions_input_cancel():
    """Vérifie l'annulation par commande /cancel lors de la saisie."""
    alert_id = uuid.uuid4()
    await save_pending_tuning(alert_id, ["suggestion"])

    message = MagicMock(spec=Message)
    message.text = "/cancel"
    message.answer = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()

    state = MagicMock(spec=FSMContext)
    state.storage = MemoryStorage()
    state.key = StorageKey(bot_id=1, chat_id=42, user_id=42)
    state.get_data = AsyncMock(return_value={"tuning_alert_id": str(alert_id)})
    state.clear = AsyncMock()

    await handle_custom_exclusions_input(message, db_user, db_session, state)

    state.clear.assert_called_once()
    assert await get_pending_tuning(alert_id) == []
    message.answer.assert_called_once()
    assert "Modification annulée" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_create_ai_variants_reports_one_failed_attempt():
    from app.services.ai.modules.keyword_variants import keyword_variants_module

    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from tests.add_fakes import DraftState

    state = DraftState()
    await state.update_data(
        name="One Piece", include_keywords=["One Piece"], max_item_price_cents=3500,
        category_slug="bd_comics", keyword_analysis_status="failed",
    )
    await state.set_state("AlertCreationState:confirm_creation")

    engine = FlakyLLM(
        None,
        {
            "variants": ["onepiece"],
            "exclusions": ["figurine"],
            "rationale": "Orthographe courante en un seul mot.",
        },
    )

    with patch.object(keyword_variants_module, "client", engine):
        await _run_creation_keyword_retry(callback, state, client=engine)

    assert engine.calls == 1
    stored = state.data
    assert stored["keywords_finalized"] is False
    assert "L'IA n'a pas répondu" in stored["keyword_note"]

    last_text = callback.message.edit_text.call_args[0][0]
    assert "L'IA n'a pas répondu" in last_text
    keyboard = callback.message.edit_text.call_args[1]["reply_markup"]
    assert any(
        button.callback_data == "alert_create_ai_variants"
        for row in keyboard.inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {"suggested_exclusions": 42}])
async def test_autotuning_failure_is_not_an_empty_answer(response):
    from app.services.ai.modules.auto_tuning import auto_tuning_module

    client = FakeLLM(text=response)
    with patch.object(auto_tuning_module, "client", client):
        result = await auto_tuning_module.suggest_exclusions("Tintin", ["Figurine Tintin"])

    assert result is None
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"suggested_exclusions": []}])
async def test_autotuning_empty_answer_remains_an_answer(response):
    from app.services.ai.modules.auto_tuning import auto_tuning_module

    client = FakeLLM(text=response)
    with patch.object(auto_tuning_module, "client", client):
        result = await auto_tuning_module.suggest_exclusions("Tintin", ["Figurine Tintin"])

    assert result == ([], None)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_build_exclusion_proposal_preserves_engine_failure():
    with patch(
        "app.services.ai.modules.auto_tuning.auto_tuning_module.suggest_exclusions",
        AsyncMock(return_value=None),
    ):
        proposal = await build_exclusion_proposal(
            "Tintin", [], ["Figurine Tintin", "Poster Tintin", "T-shirt Tintin"]
        )

    assert proposal is None


@pytest.mark.asyncio
async def test_process_autotune_failure_preserves_dislikes_without_claiming_an_answer():
    alert = Alert(id=uuid.uuid4(), name="Tintin", exclude_keywords=[])
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    session = MagicMock()
    session.commit = AsyncMock()
    with (
        patch(
            "app.bot.handlers.autotune.get_disliked_titles_for_alert",
            AsyncMock(return_value=["Figurine Tintin"]),
        ),
        patch("app.bot.handlers.autotune.build_exclusion_proposal", AsyncMock(return_value=None)),
        patch("app.bot.handlers.autotune.clear_dislikes_for_alert", AsyncMock()) as clear,
        patch("app.bot.handlers.autotune.save_pending_tuning", AsyncMock()) as save,
    ):
        await process_autotune_for_alert(callback, alert, session)

    clear.assert_not_awaited()
    save.assert_not_awaited()
    session.commit.assert_not_awaited()
    text = callback.message.edit_text.call_args.args[0]
    assert "n'a pas abouti" in text
    assert "Toutes les exclusions pertinentes" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("suggestion", [None, ([], None), (["figurine"], "Déjà exclue")])
async def test_proactive_autotuning_without_new_exclusions_releases_claim(suggestion):
    from app.core.constants import AlertStatus
    from app.tasks.proactive_autotune import check_and_trigger_proactive_autotune

    alert = Alert(
        id=uuid.uuid4(), name="Tintin", status=AlertStatus.ACTIVE,
        exclude_keywords=["figurine"],
    )
    bot = AsyncMock()
    repository = MagicMock()
    repository.get_by_id = AsyncMock(return_value=alert)
    with (
        patch("app.tasks.proactive_autotune.claim_autotune_slot", AsyncMock(return_value=True)),
        patch("app.tasks.proactive_autotune.release_autotune_claim", AsyncMock()) as release,
        patch("app.tasks.proactive_autotune.AsyncSessionLocal"),
        patch("app.tasks.proactive_autotune.AlertRepository", return_value=repository),
        patch(
            "app.tasks.proactive_autotune.get_disliked_titles_for_alert",
            AsyncMock(return_value=["Figurine Tintin", "Poster Tintin", "T-shirt Tintin"]),
        ),
        patch(
            "app.services.ai.modules.auto_tuning.auto_tuning_module.suggest_exclusions",
            AsyncMock(return_value=suggestion),
        ) as suggest,
        patch("app.tasks.proactive_autotune.save_pending_tuning", AsyncMock()) as save,
    ):
        result = await check_and_trigger_proactive_autotune(alert.id, bot, chat_id=42)

    assert result is False
    suggest.assert_awaited_once()
    save.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    release.assert_awaited_once_with(alert.id)


@pytest.mark.asyncio
async def test_autotuning_unconfigured_engine_is_not_called():
    from app.services.ai.modules.auto_tuning import auto_tuning_module

    client = FakeLLM(enabled=False)
    with patch.object(auto_tuning_module, "client", client):
        result = await auto_tuning_module.suggest_exclusions("Tintin", ["Figurine Tintin"])

    assert result is None
    assert client.calls == []

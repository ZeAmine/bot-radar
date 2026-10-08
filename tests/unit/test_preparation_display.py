"""L'état de préparation est visible et chaque bouton porte l'intention adaptée."""

import uuid

import pytest

from app.bot.formatter import alert_status_text
from app.bot.keyboards.alerts import get_alert_card_keyboard
from app.db.models.alert import Alert
from app.domain.entities.alert_preparation import PreparationRequest
from app.domain.enums import AlertStatus


@pytest.mark.parametrize(
    ("state", "activate", "text", "action"),
    [
        ("pending", True, "Préparation en cours", "pause"),
        ("pending", False, "Pause volontaire", "resume"),
        ("blocked", True, "Préparation bloquée", "resume"),
    ],
)
def test_preparation_status_and_button_distinguish_work_pause_and_retry(
    state, activate, text, action
):
    alert = Alert(id=uuid.uuid4(), status=AlertStatus.PAUSED)
    alert.preparation = PreparationRequest(state=state, activation_requested=activate).stored()
    assert text in alert_status_text(alert)
    keyboard = get_alert_card_keyboard(alert.id, alert.status, 0, 1, preparation=alert.preparation)
    assert keyboard.inline_keyboard[0][1].callback_data == f"al_{action}:{alert.id}"

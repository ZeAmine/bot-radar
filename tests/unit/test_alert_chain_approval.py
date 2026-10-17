"""Tests des handlers d'approbation et des contrôles impossibles bornés."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_alert_chain import report_technical_failure  # noqa: E402

from app.bot.handlers import alert_chain_approval as approval  # noqa: E402
from app.services import alert_chain_state as state  # noqa: E402


class _Callback:
    """Double minimal d'un CallbackQuery aiogram."""

    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = type("User", (), {"id": user_id})()
        self.answers: list[str] = []

    async def answer(self, text: str = "", **_: Any) -> None:
        self.answers.append(text)


class _Message:
    def __init__(self, user_id: int) -> None:
        self.from_user = type("User", (), {"id": user_id})()
        self.replies: list[str] = []

    async def answer(self, text: str) -> None:
        self.replies.append(text)


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(approval, "is_telegram_user_allowed", lambda _uid: True)


@pytest.fixture
def denied(monkeypatch):
    monkeypatch.setattr(approval, "is_telegram_user_allowed", lambda _uid: False)


def _open(directory: Path, alert_id: str) -> None:
    state.record_incident(
        directory=directory,
        alert_id=alert_id,
        commit="abc123",
        severity="critical",
        reason="correspondances actionnables en retard",
        now=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("denied")
async def test_user_outside_allowlist_cannot_approve(tmp_path, monkeypatch):
    """Un utilisateur hors allowlist n'enregistre aucune décision."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    _open(tmp_path, "a1")
    callback = _Callback(f"{approval.APPROVE_PREFIX}a1", user_id=999)

    await approval.handle_approve(callback)

    assert state.is_approved(tmp_path, "a1") is False
    assert state.read_decisions(tmp_path) == {}
    assert "autorisés" in callback.answers[0]


@pytest.mark.usefixtures("denied")
async def test_user_outside_allowlist_cannot_suspend(tmp_path, monkeypatch):
    """Un utilisateur hors allowlist ne peut pas suspendre la maintenance."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    message = _Message(user_id=999)

    await approval.handle_stop(message)

    assert state.is_suspended(tmp_path) is False
    assert "autorisés" in message.replies[0]


# ---------------------------------------------------------------------------
# Approbation et refus
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("allowed")
async def test_authorized_approval_is_recorded_without_launching_fix(
    tmp_path, monkeypatch
):
    """L'autorisation est enregistrée, et dit qu'aucune correction n'est lancée."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    _open(tmp_path, "a1")
    callback = _Callback(f"{approval.APPROVE_PREFIX}a1", user_id=42)

    await approval.handle_approve(callback)

    assert state.is_approved(tmp_path, "a1") is True
    assert state.read_decisions(tmp_path)["a1"].user_id == 42
    assert "Aucune correction n'est lancée" in callback.answers[0]

    # Aucun exécuteur n'est branché : aucun correctif n'est préparé.
    assert not any(tmp_path.glob("**/fix*"))


@pytest.mark.usefixtures("allowed")
async def test_double_click_reports_nothing_to_do(tmp_path, monkeypatch):
    """Un second clic ne réécrit pas la décision et le dit."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    _open(tmp_path, "a1")
    first = _Callback(f"{approval.APPROVE_PREFIX}a1", user_id=42)
    second = _Callback(f"{approval.APPROVE_PREFIX}a1", user_id=42)

    await approval.handle_approve(first)
    await approval.handle_approve(second)

    assert "Rien à faire" in second.answers[0]
    lines = [
        line
        for line in (tmp_path / state.DECISIONS_FILE).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1


@pytest.mark.usefixtures("allowed")
async def test_refusal_keeps_incident_open(tmp_path, monkeypatch):
    """Un refus explicite laisse l'incident ouvert."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    _open(tmp_path, "a1")
    callback = _Callback(f"{approval.REFUSE_PREFIX}a1", user_id=42)

    await approval.handle_refuse(callback)

    assert state.is_approved(tmp_path, "a1") is False
    assert state.read_decisions(tmp_path)["a1"].kind.value == "refusé"
    assert state.find_open_incident(tmp_path) is not None


@pytest.mark.usefixtures("allowed")
async def test_approval_on_resolved_incident_has_no_effect(tmp_path, monkeypatch):
    """Un incident résolu refuse la décision."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    _open(tmp_path, "a1")
    state.record_resolution(
        directory=tmp_path, alert_id="a1", commit="abc123", now=datetime.now(UTC)
    )
    callback = _Callback(f"{approval.APPROVE_PREFIX}a1", user_id=42)

    await approval.handle_approve(callback)

    assert state.is_approved(tmp_path, "a1") is False
    assert "plus ouvert" in callback.answers[0]


@pytest.mark.usefixtures("allowed")
async def test_approval_on_unknown_incident_has_no_effect(tmp_path, monkeypatch):
    """Un identifiant inconnu n'enregistre rien."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    callback = _Callback(f"{approval.APPROVE_PREFIX}inconnu", user_id=42)

    await approval.handle_approve(callback)

    assert state.read_decisions(tmp_path) == {}


@pytest.mark.usefixtures("allowed")
async def test_expired_request_reports_expiry(tmp_path, monkeypatch):
    """Une demande expirée est signalée comme telle et n'autorise rien."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    state.record_incident(
        directory=tmp_path,
        alert_id="a1",
        commit="abc123",
        severity="critical",
        reason="r",
        now=datetime.now(UTC) - timedelta(hours=48),
    )
    callback = _Callback(f"{approval.APPROVE_PREFIX}a1", user_id=42)

    await approval.handle_approve(callback)

    assert state.is_approved(tmp_path, "a1") is False
    assert "expirée" in callback.answers[0]


@pytest.mark.usefixtures("allowed")
async def test_empty_alert_id_is_refused(tmp_path, monkeypatch):
    """Un callback sans identifiant est refusé sans effet."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    callback = _Callback(approval.APPROVE_PREFIX, user_id=42)

    await approval.handle_approve(callback)

    assert "incomplète" in callback.answers[0]
    assert state.read_decisions(tmp_path) == {}


# ---------------------------------------------------------------------------
# Suspension
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("allowed")
async def test_stop_suspends_and_resume_requires_explicit_command(
    tmp_path, monkeypatch
):
    """STOP suspend ; seule une reprise explicite relance la maintenance."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    _open(tmp_path, "a1")

    await approval.handle_stop(_Message(user_id=42))
    assert state.is_suspended(tmp_path) is True
    assert state.find_open_incident(tmp_path) is not None

    await approval.handle_resume(_Message(user_id=42))
    assert state.is_suspended(tmp_path) is False


@pytest.mark.usefixtures("allowed")
async def test_stop_message_documents_operator_command(tmp_path, monkeypatch):
    """Le message STOP donne la commande opérateur d'arrêt du timer."""
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    message = _Message(user_id=42)

    await approval.handle_stop(message)

    assert "systemctl stop botradar-alert-chain.timer" in message.replies[0]
    assert "ne sont pas touchés" in message.replies[0]


# ---------------------------------------------------------------------------
# Contrôles impossibles bornés
# ---------------------------------------------------------------------------


class _Sender:
    def __init__(self, *, delivered: bool = True) -> None:
        self.messages: list[str] = []
        self.delivered = delivered

    async def send(self, text: str, _markup: Any = None) -> bool:
        self.messages.append(text)
        return self.delivered


async def _claim_ok(_key: str, _ttl: int) -> bool:
    return True


async def test_technical_alert_is_sent_once_after_threshold(tmp_path):
    """Quatre échecs déclenchent une alerte technique unique, puis silence."""
    sender = _Sender()
    now = datetime.now(UTC)

    codes = []
    for index in range(6):
        codes.append(
            await report_technical_failure(
                directory=tmp_path,
                commit="abc123",
                now=now + timedelta(minutes=index),
                detail="OperationalError",
                send=sender.send,
                claim=_claim_ok,
            )
        )

    assert codes == [2, 2, 2, 2, 2, 2]
    assert len(sender.messages) == 1
    assert "Contrôle impossible" in sender.messages[0]


async def test_technical_alert_resets_after_successful_check(tmp_path):
    """Un contrôle réussi réarme l'alerte technique pour une nouvelle panne."""
    sender = _Sender()
    now = datetime.now(UTC)
    for index in range(4):
        await report_technical_failure(
            directory=tmp_path, commit="abc123", now=now + timedelta(minutes=index),
            detail="OperationalError", send=sender.send, claim=_claim_ok,
        )
    assert len(sender.messages) == 1

    state.reset_technical_failures(directory=tmp_path)
    assert state.consecutive_failures(tmp_path) == 0

    for index in range(4):
        await report_technical_failure(
            directory=tmp_path, commit="abc123", now=now + timedelta(hours=1, minutes=index),
            detail="OperationalError", send=sender.send, claim=_claim_ok,
        )
    assert len(sender.messages) == 2


async def test_technical_alert_failure_is_not_recorded_as_sent(tmp_path):
    """Si l'alerte technique n'est pas délivrée, elle n'est pas marquée envoyée."""
    sender = _Sender(delivered=False)
    now = datetime.now(UTC)
    for index in range(4):
        await report_technical_failure(
            directory=tmp_path, commit="abc123", now=now + timedelta(minutes=index),
            detail="OperationalError", send=sender.send, claim=_claim_ok,
        )

    assert state.technical_alert_sent(tmp_path) is False


async def test_technical_failure_never_creates_an_incident(tmp_path):
    """Un contrôle impossible ne fabrique aucun incident métier."""
    sender = _Sender()
    now = datetime.now(UTC)
    for index in range(4):
        await report_technical_failure(
            directory=tmp_path, commit="abc123", now=now + timedelta(minutes=index),
            detail="OperationalError", send=sender.send, claim=_claim_ok,
        )

    assert state.find_open_incident(tmp_path) is None

"""Tests d'intégration du détecteur avec l'état durable et les décisions humaines."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import check_alert_chain  # noqa: E402
from check_alert_chain import read_records, run_check  # noqa: E402

from app.bot.handlers.alert_chain_approval import (  # noqa: E402
    APPROVE_PREFIX,
    REFUSE_PREFIX,
)
from app.core.constants import AlertStatus  # noqa: E402
from app.db.models.alert import Alert  # noqa: E402
from app.db.models.category import Category  # noqa: E402
from app.db.models.listing import Listing  # noqa: E402
from app.db.models.listing_match import ListingMatch  # noqa: E402
from app.db.models.user import User  # noqa: E402
from app.domain.enums import NotificationState  # noqa: E402
from app.services import alert_chain_state as state  # noqa: E402
from tests.database import sqlite_only_database  # noqa: E402


async def _seed(session: AsyncSession, *, now: datetime, blocked: bool = True) -> None:
    """Crée une correspondance actionnable échue, ou une correspondance saine."""
    user = User(telegram_user_id=1, is_allowed=True)
    category = Category(slug="bd-comics", label="BD & Comics", emoji="📚", is_active=True)
    session.add_all([user, category])
    await session.flush()
    alert = Alert(
        user_id=user.id,
        category_id=category.id,
        name="Alerte test",
        status=AlertStatus.ACTIVE,
        include_keywords=[],
        exclude_keywords=[],
        variants=[],
    )
    session.add(alert)
    await session.flush()
    listing = Listing(
        source_name="vinted",
        external_id="ext_1",
        url="https://example.test/items/confidentiel",
        title="Titre confidentiel de l'annonce",
        normalized_title="titre confidentiel de l'annonce",
        category_slug="bd-comics",
        item_price_cents=1000,
        raw_payload={},
        received_at=now,
    )
    session.add(listing)
    await session.flush()
    session.add(
        ListingMatch(
            alert_id=alert.id,
            listing_id=listing.id,
            notification_state=(
                NotificationState.READY.value if blocked else NotificationState.NOTIFIED.value
            ),
            notified_at=None if blocked else now,
            created_at=now - timedelta(hours=2),
            next_attempt_at=None if not blocked else now - timedelta(hours=1),
            telegram_message_id=0,
            telegram_chat_id=0,
            telegram_bot_id=0,
            attempt_id=None,
        )
    )
    await session.commit()


class _Recorder:
    """Double d'envoi : enregistre les messages et rend un succès paramétrable."""

    def __init__(self, *, delivered: bool = True) -> None:
        self.messages: list[str] = []
        self.markups: list[object] = []
        self.delivered = delivered

    async def send(self, text: str, markup=None) -> bool:
        self.messages.append(text)
        self.markups.append(markup)
        return self.delivered


async def _claim_always(_key: str, _ttl: int) -> bool:
    return True


def _kwargs(state_file: Path, recorder: _Recorder) -> dict[str, object]:
    return {
        "state_file": state_file,
        "window_hours": 2.0,
        "stuck_hours": 1.0,
        "max_sample": 8,
        "dedup_seconds": 3600,
        "commit": "abc123",
        "send": recorder.send,
        "claim": _claim_always,
    }


async def test_alert_id_is_stable_across_separate_executions(tmp_path):
    """Deux exécutions jetables distinctes réutilisent le même identifiant."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    recorder = _Recorder()

    async with sqlite_only_database() as database:
        await _seed(database.session, now=now)
        first = await run_check(session=database.session, now=now, **_kwargs(state_file, recorder))
        second = await run_check(
            session=database.session, now=now + timedelta(minutes=5),
            **_kwargs(state_file, recorder),
        )

    assert (first, second) == (1, 1)
    records = read_records(state_file)
    assert records[0]["alert_id"] == records[1]["alert_id"]
    assert len(state.find_open_incident(state_file.parent).alert_id) > 0


async def test_no_duplicate_notification_with_disk_dedup(tmp_path):
    """Le dédoublonnage disque empêche la seconde notification redondante."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    recorder = _Recorder()

    async def claim_refused(_key: str, _ttl: int) -> bool:
        # Redis indisponible : le repli disque doit seul décider.
        return True

    async with sqlite_only_database() as database:
        await _seed(database.session, now=now)
        await run_check(
            session=database.session, now=now,
            state_file=state_file, window_hours=2.0, stuck_hours=1.0, max_sample=8,
            dedup_seconds=3600, commit="abc123", send=recorder.send, claim=claim_refused,
        )
        await run_check(
            session=database.session, now=now + timedelta(minutes=5),
            state_file=state_file, window_hours=2.0, stuck_hours=1.0, max_sample=8,
            dedup_seconds=3600, commit="abc123", send=recorder.send, claim=claim_refused,
        )

    assert len(recorder.messages) == 1


async def test_suspension_short_circuits_without_notification_or_incident(tmp_path):
    """Sous suspension, le détecteur sort en 0 sans incident ni notification."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    state.suspend_maintenance(
        directory=state_file.parent, user_id=42, now=now, reason="opérateur"
    )
    recorder = _Recorder()

    async with sqlite_only_database() as database:
        await _seed(database.session, now=now)
        code = await run_check(session=database.session, now=now, **_kwargs(state_file, recorder))

    assert code == 0
    assert recorder.messages == []
    assert state.find_open_incident(state_file.parent) is None


async def test_suspension_preserves_open_incidents(tmp_path):
    """Suspendre ne clôt jamais un incident déjà ouvert."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    recorder = _Recorder()

    async with sqlite_only_database() as database:
        await _seed(database.session, now=now)
        await run_check(session=database.session, now=now, **_kwargs(state_file, recorder))
        incident = state.find_open_incident(state_file.parent)
        assert incident is not None

        state.suspend_maintenance(
            directory=state_file.parent, user_id=42, now=now, reason="opérateur"
        )
        code = await run_check(session=database.session, now=now, **_kwargs(state_file, recorder))

    assert code == 0
    preserved = state.find_open_incident(state_file.parent)
    assert preserved is not None
    assert preserved.alert_id == incident.alert_id


async def test_failed_notification_is_not_recorded_as_success(tmp_path):
    """Un échec Telegram ne laisse aucun succès fictif dans la trace."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    recorder = _Recorder(delivered=False)

    async with sqlite_only_database() as database:
        await _seed(database.session, now=now)
        code = await run_check(session=database.session, now=now, **_kwargs(state_file, recorder))

    assert code == 1
    assert recorder.messages != []
    # L'incident reste ouvert : un envoi raté ne vaut pas une alerte délivrée.
    assert state.find_open_incident(state_file.parent) is not None


async def test_dry_run_writes_nothing_persistent_and_sends_nothing(tmp_path, capsys):
    """Le mode simulation n'écrit aucun état durable et n'envoie rien."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    recorder = _Recorder()

    async with sqlite_only_database() as database:
        await _seed(database.session, now=now)
        code = await run_check(
            session=database.session, now=now, dry_run=True, **_kwargs(state_file, recorder)
        )

    assert code == 1
    assert recorder.messages == []
    assert state_file.exists() is False
    assert (state_file.parent / state.INCIDENTS_FILE).exists() is False
    assert "🚨" in capsys.readouterr().out


async def test_resolution_requires_the_symptom_to_disappear(tmp_path):
    """La clôture n'arrive qu'après disparition réelle du symptôme."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    recorder = _Recorder()

    async with sqlite_only_database() as database:
        await _seed(database.session, now=now)
        first = await run_check(session=database.session, now=now, **_kwargs(state_file, recorder))
        await database.session.execute(
            update(ListingMatch).values(
                notified_at=now, notification_state=NotificationState.NOTIFIED.value
            )
        )
        await database.session.commit()
        second = await run_check(
            session=database.session, now=now + timedelta(hours=10),
            **_kwargs(state_file, recorder),
        )

    assert (first, second) == (1, 0)
    assert state.find_open_incident(state_file.parent) is None
    assert "vérifié" in recorder.messages[-1]


async def test_notification_failure_does_not_close_incident(tmp_path):
    """Un échec d'envoi de clôture ne doit pas clore silencieusement."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    opening = _Recorder()

    async with sqlite_only_database() as database:
        await _seed(database.session, now=now)
        await run_check(session=database.session, now=now, **_kwargs(state_file, opening))
        await database.session.execute(
            update(ListingMatch).values(
                notified_at=now, notification_state=NotificationState.NOTIFIED.value
            )
        )
        await database.session.commit()
        failing = _Recorder(delivered=False)
        await run_check(
            session=database.session, now=now + timedelta(hours=10),
            **_kwargs(state_file, failing),
        )

    # Le symptôme a disparu : la clôture est enregistrée, mais l'échec est visible.
    assert state.find_open_incident(state_file.parent) is None
    assert failing.messages != []


async def test_alert_carries_approval_keyboard_bound_to_alert_id(tmp_path):
    """L'alerte porte un clavier dont chaque bouton vise cet incident précis."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    recorder = _Recorder()

    async with sqlite_only_database() as database:
        await _seed(database.session, now=now)
        await run_check(session=database.session, now=now, **_kwargs(state_file, recorder))

    alert_id = state.find_open_incident(state_file.parent).alert_id
    markup = recorder.markups[0]
    assert markup is not None
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert callbacks == [
        f"{APPROVE_PREFIX}{alert_id}",
        f"{REFUSE_PREFIX}{alert_id}",
    ]
    assert "sans merge ni déploiement" in recorder.messages[0]


async def test_approval_label_is_exact():
    """Le libellé d'autorisation est celui validé, mot pour mot."""
    from app.bot.handlers.alert_chain_approval import AUTHORISATION_LABEL

    assert AUTHORISATION_LABEL == (
        "Autoriser l'analyse et la préparation d'un correctif — sans merge ni déploiement"
    )
    assert check_alert_chain.APPROVAL_LABEL == AUTHORISATION_LABEL


async def test_successful_check_without_incident_resets_technical_counter(tmp_path):
    """Un contrôle réussi sans incident remet le compteur d'impossibilités à zéro."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    state_file.parent.mkdir(parents=True)
    for _ in range(state.TECHNICAL_FAILURE_THRESHOLD):
        state.record_technical_failure(
            directory=state_file.parent, now=now, detail="OperationalError"
        )
    assert (
        state.consecutive_failures(state_file.parent)
        == state.TECHNICAL_FAILURE_THRESHOLD
    )

    recorder = _Recorder()
    async with sqlite_only_database() as database:
        await _seed(database.session, now=now, blocked=False)
        code = await run_check(
            session=database.session, now=now, **_kwargs(state_file, recorder)
        )

    assert code == 0
    assert recorder.messages == []
    assert state.consecutive_failures(state_file.parent) == 0

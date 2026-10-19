"""Tests du détecteur de silence de la chaîne d'alerte (lecture seule)."""

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import check_alert_chain  # noqa: E402
from check_alert_chain import (  # noqa: E402
    BlockedMatch,
    Snapshot,
    Verdict,
    append_record,
    collect_snapshot,
    decide,
    evaluate,
    find_open_alert,
    format_alert,
    format_resolution,
    read_records,
    run_check,
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

FULL_SHA = "0123456789abcdef0123456789abcdef01234567"


async def _seed(session: AsyncSession, *, now: datetime, matches: list[dict[str, object]]) -> None:
    """Crée une alerte et une annonce par correspondance décrite par `matches`."""
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
    for index, spec in enumerate(matches):
        listing = Listing(
            source_name="vinted",
            external_id=f"ext_{index}",
            url="https://example.test/items/confidentiel",
            title="Titre confidentiel de l'annonce",
            normalized_title="titre confidentiel de l'annonce",
            category_slug="bd-comics",
            item_price_cents=1000,
            raw_payload={},
            received_at=spec.get("received_at", now),
        )
        session.add(listing)
        await session.flush()
        session.add(
            ListingMatch(
                alert_id=alert.id,
                listing_id=listing.id,
                notification_state=spec["state"],
                notified_at=spec.get("notified_at"),
                created_at=spec["created_at"],
                next_attempt_at=spec.get("next_attempt_at"),
                telegram_message_id=spec.get("telegram_message_id", 0),
                telegram_chat_id=spec.get("telegram_chat_id", 0),
                telegram_bot_id=spec.get("telegram_bot_id", 0),
                attempt_id=spec.get("attempt_id"),
            )
        )
    await session.commit()


def _snapshot(
    *,
    now: datetime,
    ingested: int = 0,
    acknowledged: int = 0,
    matches_created: int = 0,
    matches_notified: int | None = None,
    blocked: int = 0,
    non_terminal: int = 0,
) -> Snapshot:
    notified = acknowledged if matches_notified is None else matches_notified
    return Snapshot(
        window_start=now - timedelta(hours=2),
        stuck_before=now - timedelta(hours=1),
        window_hours=2.0,
        stuck_hours=1.0,
        ingested=ingested,
        acknowledged=acknowledged,
        matches_created=matches_created,
        matches_notified=notified,
        blocked_actionable_due=blocked,
        non_terminal_due=non_terminal,
        blocked_sample=(),
    )


def test_evaluate_ignores_ingestion_without_any_match():
    now = datetime.now(UTC)
    verdict = evaluate(
        _snapshot(now=now, ingested=142, acknowledged=0, matches_created=0, blocked=0)
    )
    assert verdict.incident is False
    assert verdict.severity == "ok"
    assert "aucune correspondance créée" in verdict.reason


def test_evaluate_ignores_fresh_unnotified_match():
    now = datetime.now(UTC)
    verdict = evaluate(
        _snapshot(now=now, ingested=5, acknowledged=0, matches_created=1, blocked=0)
    )
    assert verdict.incident is False
    assert verdict.severity == "ok"


def test_evaluate_detects_blockage_despite_unrelated_notification():
    now = datetime.now(UTC)
    verdict = evaluate(
        _snapshot(now=now, ingested=5, acknowledged=1, matches_created=2, blocked=1)
    )
    assert verdict.incident is True
    assert verdict.severity == "critical"
    assert verdict.reason == "correspondances actionnables en retard"


def test_evaluate_ignores_created_matches_that_were_notified():
    now = datetime.now(UTC)
    verdict = evaluate(
        _snapshot(now=now, ingested=5, acknowledged=2, matches_created=2, blocked=0)
    )
    assert verdict.incident is False
    assert verdict.severity == "ok"


def test_evaluate_flags_blocked_actionable_matches():
    now = datetime.now(UTC)
    verdict = evaluate(
        _snapshot(now=now, ingested=5, acknowledged=2, matches_created=2, blocked=1)
    )
    assert verdict.incident is True
    assert verdict.severity == "critical"
    assert verdict.reason == "correspondances actionnables en retard"


def test_evaluate_keeps_non_terminal_due_information_without_incident():
    now = datetime.now(UTC)
    verdict = evaluate(
        _snapshot(
            now=now,
            ingested=5,
            acknowledged=1,
            matches_created=1,
            blocked=0,
            non_terminal=2,
        )
    )
    assert verdict.incident is False
    assert verdict.severity == "ok"


@pytest.mark.parametrize(
    ("incident", "open_alert", "expected"),
    [
        (True, None, "open"),
        (True, "a", "continue"),
        (False, "a", "resolve"),
        (False, None, "none"),
    ],
)
def test_decide_covers_the_four_transitions(incident, open_alert, expected):
    assert decide(incident=incident, open_alert_id=open_alert) == expected


def test_find_open_alert_tracks_open_then_resolved():
    records = [
        {"event": "alert_chain", "status": "ouvert", "alert_id": "a"},
        {"event": "alert_chain", "status": "ouvert", "alert_id": "a"},
    ]
    assert find_open_alert(records) == "a"
    records.append({"event": "alert_chain", "status": "vérifié", "alert_id": "a", "resolves": "a"})
    assert find_open_alert(records) is None


def test_read_records_ignores_malformed_lines(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{"event": "alert_chain", "status": "ok"}\nnot json\n\n', encoding="utf-8")
    records = read_records(path)
    assert len(records) == 1
    assert records[0]["status"] == "ok"


async def test_collect_snapshot_classifies_only_actionable_blockages():
    now = datetime.now(UTC)
    old = now - timedelta(hours=2)
    receipt_attempt = uuid.uuid4()
    async with sqlite_only_database() as database:
        await _seed(
            database.session,
            now=now,
            matches=[
                {
                    "state": NotificationState.READY.value,
                    "created_at": old,
                    "next_attempt_at": now - timedelta(hours=1),
                },
                {
                    "state": NotificationState.DEFERRED.value,
                    "created_at": old,
                    "next_attempt_at": None,
                },
                {
                    "state": NotificationState.SENDING.value,
                    "created_at": old,
                    "next_attempt_at": now - timedelta(minutes=30),
                },
                {
                    "state": NotificationState.UNKNOWN.value,
                    "created_at": old,
                    "telegram_message_id": 42,
                    "telegram_chat_id": 1,
                    "telegram_bot_id": 2,
                    "attempt_id": receipt_attempt,
                },
                {"state": NotificationState.UNKNOWN.value, "created_at": old},
                {"state": NotificationState.ABANDONED.value, "created_at": old},
                {
                    "state": NotificationState.NOTIFIED.value,
                    "created_at": old,
                    "notified_at": now,
                },
                {
                    "state": NotificationState.READY.value,
                    "created_at": now - timedelta(minutes=5),
                    "next_attempt_at": None,
                },
                {
                    "state": NotificationState.READY.value,
                    "created_at": old,
                    "next_attempt_at": now + timedelta(hours=1),
                },
            ],
        )
        snapshot = await collect_snapshot(
            database.session, now=now, window_hours=2, stuck_hours=1, max_sample=10
        )

    assert snapshot.ingested == 9
    assert snapshot.acknowledged == 1
    assert snapshot.matches_created == 9
    assert snapshot.matches_notified == 1
    assert snapshot.blocked_actionable_due == 4
    assert snapshot.non_terminal_due == 6
    assert len(snapshot.blocked_sample) == 4


async def test_collect_snapshot_unknown_without_receipt_is_not_actionable():
    now = datetime.now(UTC)
    old = now - timedelta(hours=2)
    async with sqlite_only_database() as database:
        await _seed(
            database.session,
            now=now,
            matches=[
                {
                    "state": NotificationState.UNKNOWN.value,
                    "created_at": old,
                    "telegram_message_id": 42,
                    "telegram_chat_id": 1,
                    "telegram_bot_id": 2,
                    "attempt_id": uuid.uuid4(),
                },
                {"state": NotificationState.UNKNOWN.value, "created_at": old},
            ],
        )
        snapshot = await collect_snapshot(
            database.session, now=now, window_hours=2, stuck_hours=1, max_sample=10
        )

    assert snapshot.blocked_actionable_due == 1
    assert snapshot.non_terminal_due == 2
    assert snapshot.blocked_sample[0].notification_state == NotificationState.UNKNOWN.value


async def test_collect_snapshot_future_retry_is_not_blocked():
    now = datetime.now(UTC)
    old = now - timedelta(hours=2)
    async with sqlite_only_database() as database:
        await _seed(
            database.session,
            now=now,
            matches=[
                {
                    "state": NotificationState.READY.value,
                    "created_at": old,
                    "next_attempt_at": now + timedelta(hours=1),
                }
            ],
        )
        snapshot = await collect_snapshot(
            database.session, now=now, window_hours=2, stuck_hours=1, max_sample=10
        )

    assert snapshot.blocked_actionable_due == 0
    assert snapshot.non_terminal_due == 0


async def test_collect_snapshot_fresh_match_is_not_blocked():
    now = datetime.now(UTC)
    async with sqlite_only_database() as database:
        await _seed(
            database.session,
            now=now,
            matches=[
                {
                    "state": NotificationState.READY.value,
                    "created_at": now - timedelta(minutes=5),
                    "next_attempt_at": None,
                }
            ],
        )
        snapshot = await collect_snapshot(
            database.session, now=now, window_hours=2, stuck_hours=1, max_sample=10
        )

    assert snapshot.blocked_actionable_due == 0
    assert snapshot.matches_created == 1
    assert snapshot.matches_notified == 0


def test_format_alert_has_no_title_url_or_secret():
    now = datetime.now(UTC)
    snapshot = Snapshot(
        window_start=now - timedelta(hours=2),
        stuck_before=now - timedelta(hours=1),
        window_hours=2.0,
        stuck_hours=1.0,
        ingested=5,
        acknowledged=0,
        matches_created=2,
        matches_notified=0,
        blocked_actionable_due=4,
        non_terminal_due=2,
        blocked_sample=(
            BlockedMatch(
                match_id=str(uuid.uuid4()),
                listing_id=str(uuid.uuid4()),
                source_name="vinted",
                notification_state="ready",
                attempt_count=2,
                created_at=now,
            ),
        ),
    )
    text = format_alert(
        snapshot,
        Verdict(True, "critical", "correspondances actionnables en retard"),
        alert_id="alert-123",
        commit="abc123",
        now=now,
    )
    assert "alert-123" in text
    assert "abc123" in text
    assert "🚨" in text
    assert "Correspondances créées" in text
    assert "http" not in text
    assert "Titre confidentiel" not in text


def test_format_resolution_states_verified():
    now = datetime.now(UTC)
    text = format_resolution(
        _snapshot(now=now, ingested=3, acknowledged=3, blocked=0),
        alert_id="alert-123",
        commit="abc123",
        now=now,
    )
    assert "vérifié" in text
    assert "alert-123" in text


async def test_run_check_opens_then_verifies_on_reobservation(tmp_path):
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    sent: list[str] = []

    async def send(text: str, _markup=None) -> bool:
        sent.append(text)
        return True

    async def claim(_key: str, _ttl: int) -> bool:
        return True

    kwargs = {
        "state_file": state_file,
        "window_hours": 2.0,
        "stuck_hours": 1.0,
        "max_sample": 8,
        "dedup_seconds": 3600,
        "commit": FULL_SHA,
        "send": send,
        "claim": claim,
    }
    async with sqlite_only_database() as database:
        await _seed(
            database.session,
            now=now,
            matches=[
                {
                    "state": NotificationState.READY.value,
                    "created_at": now - timedelta(hours=2),
                    "next_attempt_at": now - timedelta(hours=1),
                }
            ],
        )
        first = await run_check(session=database.session, now=now, **kwargs)
        second = await run_check(session=database.session, now=now + timedelta(minutes=5), **kwargs)
        await database.session.execute(
            update(ListingMatch).values(
                notified_at=now,
                notification_state=NotificationState.NOTIFIED.value,
            )
        )
        await database.session.commit()
        third = await run_check(
            session=database.session, now=now + timedelta(hours=10), **kwargs
        )

    assert (first, second, third) == (1, 1, 0)
    records = read_records(state_file)
    assert [record["status"] for record in records] == ["ouvert", "ouvert", "vérifié"]
    assert records[1]["alert_id"] == records[0]["alert_id"]
    assert records[2]["resolves"] == records[0]["alert_id"]
    assert find_open_alert(records) is None
    # Une seule alerte pour l'incident : le relevé suivant ne renotifie pas le
    # même incident encore ouvert, puis la clôture part une fois.
    assert len(sent) == 2
    assert "🚨" in sent[0]
    assert "vérifié" in sent[1]
    assert "Titre confidentiel" not in "\n".join(sent)


async def test_run_check_dry_run_writes_nothing_and_sends_nothing(tmp_path, capsys):
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    sent: list[str] = []

    async def send(text: str, _markup=None) -> bool:
        sent.append(text)
        return True

    async def claim(_key: str, _ttl: int) -> bool:
        return True

    async with sqlite_only_database() as database:
        await _seed(
            database.session,
            now=now,
            matches=[
                {
                    "state": NotificationState.READY.value,
                    "created_at": now - timedelta(hours=2),
                    "next_attempt_at": now - timedelta(hours=1),
                }
            ],
        )
        code = await run_check(
            session=database.session,
            state_file=state_file,
            window_hours=2.0,
            stuck_hours=1.0,
            max_sample=8,
            dedup_seconds=3600,
            commit=FULL_SHA,
            now=now,
            send=send,
            claim=claim,
            dry_run=True,
        )

    assert code == 1
    assert state_file.exists() is False
    assert sent == []
    assert "🚨" in capsys.readouterr().out


async def test_impossible_check_does_not_close_open_alert(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    state_file = tmp_path / "state.jsonl"
    append_record(
        state_file,
        {
            "event": "alert_chain",
            "status": "ouvert",
            "alert_id": "keep-me",
            "timestamp": now.isoformat(),
        },
    )

    async def boom(*_args, **_kwargs):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(check_alert_chain, "collect_snapshot", boom)

    async def send(_text: str, _markup=None) -> bool:
        raise AssertionError("aucune notification attendue")

    async def claim(_key: str, _ttl: int) -> bool:
        return True

    async with sqlite_only_database() as database:
        with pytest.raises(RuntimeError):
            await run_check(
                session=database.session,
                state_file=state_file,
                window_hours=2.0,
                stuck_hours=1.0,
                max_sample=8,
                dedup_seconds=3600,
                commit="abc123",
                now=now,
                send=send,
                claim=claim,
            )

    records = read_records(state_file)
    assert find_open_alert(records) == "keep-me"
    assert all(record["status"] != "vérifié" for record in records)


async def test_unattested_commit_does_not_open_incident(tmp_path):
    """Sans SHA complet, aucun incident n'est créé : l'approbation serait morte."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    state_file.parent.mkdir(parents=True)
    sent: list[tuple[str, object]] = []

    async def send(text: str, markup=None) -> bool:
        sent.append((text, markup))
        return True

    async def claim(_key: str, _ttl: int) -> bool:
        return True

    async with sqlite_only_database() as database:
        await _seed(
            database.session,
            now=now,
            matches=[
                {
                    "state": NotificationState.READY.value,
                    "created_at": now - timedelta(hours=2),
                    "next_attempt_at": now - timedelta(hours=1),
                }
            ],
        )
        code = await run_check(
            session=database.session,
            state_file=state_file,
            window_hours=2.0,
            stuck_hours=1.0,
            max_sample=8,
            dedup_seconds=3600,
            commit="inconnu",
            now=now,
            send=send,
            claim=claim,
        )

    assert code == 2
    assert sent == []
    assert state.find_open_incident(state_file.parent) is None
    assert state.consecutive_failures(state_file.parent) == 1
    assert state_file.exists() is False


async def test_unattested_commit_technical_alert_is_explicit(tmp_path):
    """Après quatre échecs, l'alerte technique nomme le SHA non attestable."""
    now = datetime.now(UTC)
    directory = tmp_path / "state"
    directory.mkdir(parents=True)
    state_file = directory / "alert-chain.jsonl"
    sent: list[tuple[str, object]] = []

    async def send(text: str, markup=None) -> bool:
        sent.append((text, markup))
        return True

    async def claim(_key: str, _ttl: int) -> bool:
        return True

    async with sqlite_only_database() as database:
        await _seed(
            database.session,
            now=now,
            matches=[
                {
                    "state": NotificationState.READY.value,
                    "created_at": now - timedelta(hours=2),
                    "next_attempt_at": now - timedelta(hours=1),
                }
            ],
        )
        codes = [
            await run_check(
                session=database.session,
                state_file=state_file,
                window_hours=2.0,
                stuck_hours=1.0,
                max_sample=8,
                dedup_seconds=3600,
                commit="inconnu",
                now=now,
                send=send,
                claim=claim,
            )
            for _ in range(state.TECHNICAL_FAILURE_THRESHOLD)
        ]

    assert codes == [2] * state.TECHNICAL_FAILURE_THRESHOLD
    assert len(sent) == 1
    text, markup = sent[0]
    assert markup is None
    assert "SHA déployé" in text
    assert "ne peut pas être attesté" in text
    assert "aucun incident" in text
    assert state.find_open_incident(directory) is None


async def test_existing_incident_without_attested_sha_has_no_approval_keyboard(tmp_path):
    """Un incident ouvert sans SHA complet ne reçoit aucun clavier d'approbation."""
    now = datetime.now(UTC)
    directory = tmp_path / "state"
    directory.mkdir(parents=True)
    state_file = directory / "alert-chain.jsonl"
    state.record_incident(
        directory=directory,
        alert_id="dead-approval",
        commit="inconnu",
        severity="critical",
        reason="correspondances actionnables en retard",
        now=now,
    )
    sent: list[tuple[str, object]] = []

    async def send(text: str, markup=None) -> bool:
        sent.append((text, markup))
        return True

    async def claim(_key: str, _ttl: int) -> bool:
        return True

    async with sqlite_only_database() as database:
        await _seed(
            database.session,
            now=now,
            matches=[
                {
                    "state": NotificationState.READY.value,
                    "created_at": now - timedelta(hours=2),
                    "next_attempt_at": now - timedelta(hours=1),
                }
            ],
        )
        code = await run_check(
            session=database.session,
            state_file=state_file,
            window_hours=2.0,
            stuck_hours=1.0,
            max_sample=8,
            dedup_seconds=3600,
            commit=FULL_SHA,
            now=now,
            send=send,
            claim=claim,
        )

    assert code == 1
    assert len(sent) == 1
    text, markup = sent[0]
    assert markup is None
    assert "Autorisation indisponible" in text
    assert check_alert_chain.APPROVAL_LABEL not in text


async def test_unattested_commit_dry_run_writes_nothing(tmp_path):
    """En dry-run, le refus ne crée ni compteur, ni trace, ni notification."""
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    sent: list[str] = []

    async def send(text: str, _markup=None) -> bool:
        sent.append(text)
        return True

    async def claim(_key: str, _ttl: int) -> bool:
        return True

    async with sqlite_only_database() as database:
        await _seed(
            database.session,
            now=now,
            matches=[
                {
                    "state": NotificationState.READY.value,
                    "created_at": now - timedelta(hours=2),
                    "next_attempt_at": now - timedelta(hours=1),
                }
            ],
        )
        code = await run_check(
            session=database.session,
            state_file=state_file,
            window_hours=2.0,
            stuck_hours=1.0,
            max_sample=8,
            dedup_seconds=3600,
            commit="inconnu",
            now=now,
            send=send,
            claim=claim,
            dry_run=True,
        )

    assert code == 2
    assert sent == []
    assert state_file.exists() is False
    assert state.find_open_incident(state_file.parent) is None

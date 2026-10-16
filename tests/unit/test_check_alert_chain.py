"""Tests du détecteur de silence de la chaîne d'alerte (lecture seule)."""

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_alert_chain import (  # noqa: E402
    BlockedMatch,
    Snapshot,
    Verdict,
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
from tests.database import sqlite_only_database  # noqa: E402


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


def _snapshot(*, now: datetime, ingested: int, acknowledged: int, blocked: int) -> Snapshot:
    return Snapshot(
        window_start=now - timedelta(hours=2),
        stuck_before=now - timedelta(hours=1),
        window_hours=2.0,
        stuck_hours=1.0,
        ingested=ingested,
        acknowledged=acknowledged,
        blocked_total=blocked,
        blocked_sample=(),
    )


def test_evaluate_ignores_silence_without_ingestion():
    now = datetime.now(UTC)
    verdict = evaluate(_snapshot(now=now, ingested=0, acknowledged=0, blocked=12), min_stuck=3)
    assert verdict.incident is False
    assert verdict.severity == "ok"


def test_evaluate_flags_missing_acknowledgement_as_critical():
    now = datetime.now(UTC)
    verdict = evaluate(_snapshot(now=now, ingested=5, acknowledged=0, blocked=1), min_stuck=3)
    assert verdict.incident is True
    assert verdict.severity == "critical"


def test_evaluate_flags_persistent_blockage_as_warning():
    now = datetime.now(UTC)
    verdict = evaluate(_snapshot(now=now, ingested=5, acknowledged=2, blocked=3), min_stuck=3)
    assert verdict.incident is True
    assert verdict.severity == "warning"


def test_evaluate_below_threshold_is_not_an_incident():
    now = datetime.now(UTC)
    verdict = evaluate(_snapshot(now=now, ingested=5, acknowledged=2, blocked=2), min_stuck=3)
    assert verdict.incident is False


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
    assert snapshot.blocked_total == 4
    assert len(snapshot.blocked_sample) == 4


def test_format_alert_has_no_title_url_or_secret():
    now = datetime.now(UTC)
    snapshot = Snapshot(
        window_start=now - timedelta(hours=2),
        stuck_before=now - timedelta(hours=1),
        window_hours=2.0,
        stuck_hours=1.0,
        ingested=5,
        acknowledged=0,
        blocked_total=4,
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
        Verdict(True, "critical", "des annonces ont été ingérées sans aucun acquittement"),
        alert_id="alert-123",
        commit="abc123",
        now=now,
    )
    assert "alert-123" in text
    assert "abc123" in text
    assert "🚨" in text
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

    async def send(text: str) -> None:
        sent.append(text)

    async def claim(_key: str, _ttl: int) -> bool:
        return True

    kwargs = {
        "state_file": state_file,
        "window_hours": 2.0,
        "stuck_hours": 1.0,
        "min_stuck": 3,
        "max_sample": 8,
        "dedup_seconds": 3600,
        "commit": "abc123",
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
        third = await run_check(
            session=database.session, now=now + timedelta(hours=10), **kwargs
        )

    assert (first, second, third) == (1, 1, 0)
    records = read_records(state_file)
    assert [record["status"] for record in records] == ["ouvert", "ouvert", "vérifié"]
    assert records[1]["alert_id"] == records[0]["alert_id"]
    assert records[2]["resolves"] == records[0]["alert_id"]
    assert find_open_alert(records) is None
    assert len(sent) == 3
    assert "🚨" in sent[0]
    assert "vérifié" in sent[2]
    assert "Titre confidentiel" not in "\n".join(sent)


async def test_run_check_dry_run_writes_nothing_and_sends_nothing(tmp_path, capsys):
    now = datetime.now(UTC)
    state_file = tmp_path / "state" / "alert-chain.jsonl"
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

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
            min_stuck=3,
            max_sample=8,
            dedup_seconds=3600,
            commit="abc123",
            now=now,
            send=send,
            claim=claim,
            dry_run=True,
        )

    assert code == 1
    assert state_file.exists() is False
    assert sent == []
    assert "🚨" in capsys.readouterr().out

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import check_alert_chain as detector

from app.services import alert_chain_state as state
from tests.unit.test_alert_fix_prepare import _load, run_case
from tests.unit.test_check_alert_chain import _snapshot


async def check(tmp_path, monkeypatch, *, send, claim, dry_run=False):
    now = datetime.now(UTC)

    async def snapshot(*_args, **_kwargs):
        await asyncio.sleep(0)
        return _snapshot(now=now, blocked=1)

    monkeypatch.setattr(detector, "collect_snapshot", snapshot)
    return await detector.run_check(
        session=None,
        state_file=tmp_path / "state" / "alert-chain.jsonl",
        window_hours=2,
        stuck_hours=1,
        max_sample=8,
        dedup_seconds=3600,
        commit="a" * 40,
        now=now,
        send=send,
        claim=claim,
        dry_run=dry_run,
    )


async def test_consultation_never_claims_or_creates_artifacts(tmp_path, monkeypatch):
    async def forbidden(*_args):
        pytest.fail("side effect")

    await check(tmp_path, monkeypatch, send=forbidden, claim=forbidden, dry_run=True)
    assert not (tmp_path / "state").exists()


async def test_concurrent_detectors_share_incident_and_notification(tmp_path, monkeypatch):
    sent = []

    async def claim(*_args):
        await asyncio.sleep(0)
        return True

    async def send(message, _markup):
        sent.append(message)
        return True

    await asyncio.gather(*(check(tmp_path, monkeypatch, send=send, claim=claim) for _ in range(2)))
    opened = [
        r
        for r in state.read_incidents(tmp_path / "state").values()
        if r.status == state.IncidentStatus.OPEN
    ]
    assert len(opened) == 1
    assert len(sent) == 1
    records = detector.read_records(tmp_path / "state" / "alert-chain.jsonl")
    assert len({r["alert_id"] for r in records}) == 1


async def test_rejected_notification_claim_prevents_first_send(tmp_path, monkeypatch):
    async def claim(*_args):
        return False

    async def send(*_args):
        pytest.fail("unreserved notification")

    await check(tmp_path, monkeypatch, send=send, claim=claim)


@pytest.mark.parametrize(
    ("result", "expected"), [(True, "accepted"), (False, "failed"), (None, "uncertain")]
)
async def test_notification_result_is_persisted(tmp_path, monkeypatch, result, expected):
    async def claim(*_args):
        return True

    async def send(*_args):
        if result is None:
            raise TimeoutError()
        return result

    await check(tmp_path, monkeypatch, send=send, claim=claim)
    records = detector.read_records(tmp_path / "state" / "alert-chain.jsonl")
    assert records[-1]["notification"] == expected
    events = [
        json.loads(x) for x in (tmp_path / "state" / "notifications.jsonl").read_text().splitlines()
    ]
    assert events[0]["status"] == "reserved"
    assert events[-1]["status"] == expected
    assert events[-1]["observation_id"] == records[-1]["observation_id"]


def test_corrupt_suspension_blocks_maintenance(tmp_path):
    (tmp_path / "suspension.json").write_text("{")
    with pytest.raises(ValueError, match=".+"):
        state.is_suspended(tmp_path)


def test_recorded_proposals_are_not_ai_calls(tmp_path):
    code, path, _, _ = run_case(tmp_path)
    assert code == 0
    report = _load(path)
    assert report["llm"]["calls"] == 0
    assert report["llm"]["proposals"] == 2
    assert report["provenance"] == "unverified"
    assert "ticket synthétique : aucune preuve de production" not in report["limitations"]


def test_context_reads_do_not_grant_write_permission():
    from dataclasses import replace

    from app.services.alert_fix import core, policy
    from tests.alert_fix_fixtures import build_ticket

    ticket = build_ticket(base_sha="a" * 40)
    ticket = replace(
        ticket,
        evidence_files=("app/utils/other_context.py",),
        evidence_hashes={"app/utils/other_context.py": "b" * 64},
    )
    ticket = replace(ticket, ticket_hash=ticket.compute_hash())
    loaded = core.Ticket.from_dict(ticket.to_dict())
    assert "app/utils/other_context.py" not in policy.validate_allowed_files(loaded, ())
    patch = (
        "diff --git a/app/utils/other_context.py b/app/utils/other_context.py\n"
        "--- a/app/utils/other_context.py\n+++ b/app/utils/other_context.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    with pytest.raises(core.PolicyError, match=".+"):
        policy.validate_diff(
            patch=patch,
            allowed_files=loaded.allowed_files,
            test_file=loaded.test_file,
            limits=loaded.limits,
        )


def task_fixture(tmp_path):
    from app.services.alert_fix import exchange

    now = datetime.now(UTC)
    state.record_incident(
        directory=tmp_path,
        alert_id="incident",
        commit="a" * 40,
        severity="critical",
        reason="blocked",
        now=now,
    )
    state.request_approval(
        directory=tmp_path, alert_id="incident", user_id=42, now=now, ttl_seconds=86400
    )
    args = {
        "directory": tmp_path,
        "alert_id": "incident",
        "now": now,
        "key": b"k" * 32,
        "allowed_user_ids": {42},
        "base_sha": "a" * 40,
        "allowed_files": ("app/utils/text.py", "tests/unit/test_fix_repro.py"),
        "evidence_files": ("app/utils/text.py",),
        "test_file": "tests/unit/test_fix_repro.py",
        "trace": {"blocked_actionable_due": 1},
        "approval_valid_seconds": 3600,
        "ticket_ttl_seconds": 600,
    }
    args["evidence_hashes"] = {"app/utils/text.py": "d" * 64}
    args["confirmed_scope_hash"] = exchange.scope_digest(
        **{
            k: args[k]
            for k in ("base_sha", "allowed_files", "evidence_files", "evidence_hashes", "test_file")
        }
    )
    envelope = exchange.export_task(**args)
    return exchange, args, envelope


def test_approval_export_reserves_once_and_checks_origin(tmp_path):
    exchange, args, envelope = task_fixture(tmp_path)
    ticket = exchange.verify_envelope(envelope, key=args["key"], now=args["now"])
    assert ticket.base_sha == "a" * 40
    with pytest.raises(ValueError, match=".+"):
        exchange.export_task(**args)
    with pytest.raises(ValueError, match=".+"):
        exchange.verify_envelope(envelope, key=b"wrong" * 8, now=args["now"])


def test_task_start_requires_fresh_authority_and_explicit_interruption(tmp_path):
    exchange, args, envelope = task_fixture(tmp_path)
    task_id = envelope["task_id"]
    attempt = exchange.start_task(
        directory=tmp_path,
        envelope=envelope,
        key=args["key"],
        now=args["now"],
        allowed_user_ids={42},
    )
    with pytest.raises(ValueError, match=".+"):
        exchange.start_task(
            directory=tmp_path,
            envelope=envelope,
            key=args["key"],
            now=args["now"],
            allowed_user_ids={42},
        )
    exchange.interrupt_task(
        directory=tmp_path,
        task_id=task_id,
        attempt=attempt,
        now=args["now"],
        confirmed_stopped=True,
    )
    with pytest.raises(ValueError, match=".+"):
        exchange.resume_task(
            directory=tmp_path,
            task_id=task_id,
            now=args["now"],
            authorize_retry=False,
            key=args["key"],
            allowed_user_ids={42},
        )
    exchange.resume_task(
        directory=tmp_path,
        task_id=task_id,
        now=args["now"],
        authorize_retry=True,
        key=args["key"],
        allowed_user_ids={42},
    )
    next_attempt = exchange.start_task(
        directory=tmp_path,
        envelope=envelope,
        key=args["key"],
        now=args["now"],
        allowed_user_ids={42},
    )
    assert next_attempt != attempt


@pytest.mark.parametrize("failure", ["suspended", "corrupt", "closed", "expired", "unauthorized"])
def test_task_start_fails_closed(tmp_path, failure):
    from datetime import timedelta

    exchange, args, envelope = task_fixture(tmp_path)
    now = args["now"]
    if failure == "suspended":
        state.suspend_maintenance(directory=tmp_path, user_id=42, now=now, reason="stop")
    elif failure == "corrupt":
        (tmp_path / "decisions.jsonl").write_text("{bad")
    elif failure == "closed":
        state.record_resolution(directory=tmp_path, alert_id="incident", commit="a" * 40, now=now)
    elif failure == "expired":
        now += timedelta(seconds=601)
    with pytest.raises(ValueError, match=".+"):
        exchange.start_task(
            directory=tmp_path,
            envelope=envelope,
            key=args["key"],
            now=now,
            allowed_user_ids=set() if failure == "unauthorized" else {42},
        )


def test_notification_retry_and_interruption_are_durable(tmp_path):
    from datetime import timedelta

    now = datetime.now(UTC)
    args = {
        "directory": tmp_path,
        "key": "incident",
        "alert_id": "a",
        "observation_id": "o",
        "now": now,
        "ttl_seconds": 3600,
    }
    first = state.reserve_notification(**args)
    state.finish_notification(directory=tmp_path, reservation_id=first, status="failed", now=now)
    assert state.reserve_notification(**args) is None
    args["now"] = now + timedelta(seconds=61)
    second = state.reserve_notification(**args)
    assert second
    assert second != first
    args["now"] = now + timedelta(seconds=3662)
    assert state.reserve_notification(**args)
    events = state.authoritative_records(tmp_path / "notifications.jsonl")
    assert "interrupted" in [e["status"] for e in events]


def _observe_child(directory, ready, out):
    ready.wait(10)
    action, alert_id = state.observe_incident(
        directory=Path(directory),
        incident=True,
        commit="a" * 40,
        severity="critical",
        reason="blocked",
        now=datetime.now(UTC),
    )
    out.put((action, alert_id))


def test_two_processes_open_only_one_incident(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    out = ctx.Queue()
    children = [
        ctx.Process(target=_observe_child, args=(str(tmp_path), ready, out)) for _ in range(2)
    ]
    try:
        for child in children:
            child.start()
        ready.set()
        results = [out.get(timeout=20) for _ in children]
        assert sorted(action for action, _ in results) == ["continue", "open"]
        assert len({alert_id for _, alert_id in results}) == 1
    finally:
        for child in children:
            child.join(timeout=20)
            if child.is_alive():
                child.terminate()
                child.join()
        out.close()
    assert all(child.exitcode == 0 for child in children)


async def test_cancelled_send_leaves_uncertain_evidence(tmp_path, monkeypatch):
    async def claim(*_args):
        return True

    async def send(*_args):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await check(tmp_path, monkeypatch, send=send, claim=claim)
    records = detector.read_records(tmp_path / "state" / "alert-chain.jsonl")
    assert records[-1]["notification"] == "uncertain"
    observations = state.authoritative_records(tmp_path / "state" / "observations.jsonl")
    assert observations[-1]["observation_id"] == records[-1]["observation_id"]


async def test_network_uncertainty_is_not_a_confirmed_rejection(monkeypatch):
    from unittest.mock import AsyncMock

    from app.services import telegram_service

    monkeypatch.setattr(telegram_service.settings, "TELEGRAM_ALLOWED_USER_IDS", [42])
    bot = type("BotDouble", (), {"send_message": AsyncMock(side_effect=TimeoutError())})()
    assert await telegram_service.notify_admins_outcome("incident", bot=bot) == "uncertain"


def test_corrupt_decision_does_not_record_an_approval(tmp_path):
    now = datetime.now(UTC)
    state.record_incident(
        directory=tmp_path,
        alert_id="a",
        commit="a" * 40,
        severity="critical",
        reason="blocked",
        now=now,
    )
    path = tmp_path / "decisions.jsonl"
    path.write_text("{broken")
    result = state.request_approval(
        directory=tmp_path, alert_id="a", user_id=42, now=now, ttl_seconds=86400
    )
    assert result.outcome == state.DecisionOutcome.WRONG_STATE
    assert path.read_text() == "{broken"


def test_approval_and_ticket_have_distinct_expiry(tmp_path):
    from datetime import timedelta

    exchange, args, envelope = task_fixture(tmp_path)
    assert envelope["approval_expires_at"] != envelope["ticket"]["expires_at"]
    with pytest.raises(ValueError, match="expiré"):
        exchange.verify_envelope(envelope, key=args["key"], now=args["now"] + timedelta(minutes=11))


def test_source_recorded_does_not_authorize_host_execution(tmp_path, monkeypatch):
    from app.services.alert_fix import core, prepare
    from tests.alert_fix_fixtures import write_ticket
    from tests.unit.test_alert_fix_prepare import _ticket, make_repo

    repo, sha = make_repo(tmp_path)
    ticket = _ticket(base_sha=sha)
    ticket_path = tmp_path / "ticket.json"
    write_ticket(ticket_path, ticket)

    def forbidden(*_args, **_kwargs):
        pytest.fail("host runner must not start")

    monkeypatch.setattr(prepare.runner_module, "LocalRunner", forbidden)
    code = prepare.run_prepare(
        ticket_path=ticket_path,
        repo_path=repo,
        state_dir=tmp_path / "state",
        confirm_prepare=True,
        confirm_ticket_hash=ticket.ticket_hash,
    )
    assert code == core.exit_code_for(core.Status.RUNNER_ERROR)
    assert not (tmp_path / "state" / "used_tickets.jsonl").exists()


def test_absent_valid_and_corrupt_state_are_distinct(tmp_path):
    path = tmp_path / "suspension.json"
    assert state.authoritative_object(path) is None
    path.write_text('{"suspended": false}')
    assert state.authoritative_object(path) == {"suspended": False}
    path.write_text('{"suspended": "false"}')
    with pytest.raises(ValueError, match="invalide"):
        state.is_suspended(tmp_path)


async def test_telegram_send_is_outside_file_lock(tmp_path, monkeypatch):
    import fcntl

    async def claim(*_args):
        return True

    async def send(*_args):
        with (tmp_path / "state" / ".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return True

    await check(tmp_path, monkeypatch, send=send, claim=claim)
    assert (
        detector.read_records(tmp_path / "state" / "alert-chain.jsonl")[-1]["notification"]
        == "accepted"
    )


def test_late_receipt_does_not_replace_a_new_reservation(tmp_path):
    from datetime import timedelta

    now = datetime.now(UTC)
    args = {
        "directory": tmp_path,
        "key": "k",
        "alert_id": "a",
        "observation_id": "o",
        "now": now,
        "ttl_seconds": 10,
    }
    old = state.reserve_notification(**args)
    args["now"] += timedelta(seconds=11)
    new = state.reserve_notification(**args)
    state.finish_notification(
        directory=tmp_path, reservation_id=old, status="accepted", now=args["now"]
    )
    assert state.reserve_notification(**args) is None
    events = state.authoritative_records(tmp_path / "notifications.jsonl")
    assert events[-1]["late_result"] is True
    assert new != old


def test_completed_task_is_bound_to_candidate_and_cannot_restart(tmp_path):
    exchange, args, envelope = task_fixture(tmp_path)
    attempt = exchange.start_task(
        directory=tmp_path,
        envelope=envelope,
        key=args["key"],
        now=args["now"],
        allowed_user_ids={42},
    )
    exchange.complete_task(
        directory=tmp_path,
        task_id=envelope["task_id"],
        attempt=attempt,
        now=args["now"],
        result="ready_for_review",
        report_digest="b" * 64,
        candidate_digest="c" * 64,
    )
    with pytest.raises(ValueError, match="incompatible"):
        exchange.start_task(
            directory=tmp_path,
            envelope=envelope,
            key=args["key"],
            now=args["now"],
            allowed_user_ids={42},
        )
    latest = state.authoritative_records(tmp_path / "tasks.jsonl")[-1]
    assert latest["candidate_digest"] == "c" * 64
    assert latest["base_sha"] == "a" * 40


async def test_corrupt_maintenance_status_is_reported_without_stopping_bot(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from app.bot.handlers import alert_chain_approval as approval

    monkeypatch.setattr(approval, "_authorized", lambda _user_id: True)
    monkeypatch.setattr(approval, "_state_dir", lambda: tmp_path)
    (tmp_path / "suspension.json").write_text("{")
    message = type(
        "MessageDouble",
        (),
        {
            "from_user": type("UserDouble", (), {"id": 42})(),
            "answer": AsyncMock(),
        },
    )()
    await approval.handle_status(message)
    assert "illisible" in message.answer.call_args.args[0]


def test_v1_ticket_is_refused_explicitly():
    from app.services.alert_fix import core
    from tests.alert_fix_fixtures import build_ticket

    payload = build_ticket(base_sha="a" * 40).to_dict()
    payload["schema_version"] = 1
    with pytest.raises(core.TicketError, match="ancien|schéma"):
        core.Ticket.from_dict(payload)


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "app/../secrets.py", "app/.env", "docs/private/key.md"]
)
def test_context_contract_rejects_unsafe_paths(path):
    from dataclasses import replace

    from app.services.alert_fix import core
    from tests.alert_fix_fixtures import build_ticket

    ticket = build_ticket(base_sha="a" * 40)
    ticket = replace(ticket, evidence_files=(path,), evidence_hashes={path: "b" * 64})
    with pytest.raises(core.TicketError, match="contexte"):
        core.Ticket.from_dict(ticket.to_dict())


def test_changed_context_is_refused_before_proposals(tmp_path):
    from tests.unit.test_alert_fix_prepare import _ticket, make_repo

    repo, sha = make_repo(tmp_path)
    ticket = _ticket(base_sha=sha)
    ticket = __import__("dataclasses").replace(ticket, evidence_hashes={"app/example.py": "0" * 64})
    ticket = __import__("dataclasses").replace(ticket, ticket_hash=ticket.compute_hash())
    code, path, _, _ = run_case(tmp_path, repo=repo, ticket=ticket)
    assert code != 0
    assert _load(path)["reason_code"] == "CONTEXT_INVALID"
    assert _load(path)["llm"]["calls"] == 0


def test_parent_symlink_cannot_supply_context(tmp_path):
    from dataclasses import replace

    from app.services.alert_fix import core, prepare
    from tests.alert_fix_fixtures import build_ticket

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "proof.py").write_text("private")
    workspace = tmp_path / "workspace"
    (workspace / "app").mkdir(parents=True)
    (workspace / "app" / "link").symlink_to(outside, target_is_directory=True)
    ticket = build_ticket(base_sha="a" * 40)
    ticket = replace(
        ticket,
        evidence_files=("app/link/proof.py",),
        evidence_hashes={"app/link/proof.py": core.sha256_text("private")},
    )
    with pytest.raises(core.PolicyError, match="contexte|preuve"):
        prepare._evidence(ticket, workspace)


def test_context_lists_and_hashes_are_authenticated(tmp_path):
    import copy

    exchange, args, envelope = task_fixture(tmp_path)
    altered = copy.deepcopy(envelope)
    altered["ticket"]["evidence_hashes"]["app/utils/text.py"] = "f" * 64
    with pytest.raises(ValueError, match="origine"):
        exchange.verify_envelope(altered, key=args["key"], now=args["now"])
    args["evidence_hashes"] = {"app/utils/text.py": "e" * 64}
    with pytest.raises(ValueError, match="confirmés"):
        exchange.export_task(**args)


def test_context_total_is_bounded(tmp_path):
    from dataclasses import replace

    from app.services.alert_fix import core, prepare
    from tests.alert_fix_fixtures import build_ticket

    (tmp_path / "app").mkdir()
    paths = tuple(f"app/context{i}.py" for i in range(4))
    for name in paths:
        (tmp_path / name).write_text("a" * 17000)
    ticket = replace(
        build_ticket(base_sha="a" * 40),
        evidence_files=paths,
        evidence_hashes={name: core.sha256_text("a" * 17000) for name in paths},
    )
    with pytest.raises(core.PolicyError, match="total"):
        prepare._evidence(ticket, tmp_path)


def test_context_redacts_session_cookie(tmp_path):
    from dataclasses import replace

    from app.services.alert_fix import core, prepare
    from tests.alert_fix_fixtures import build_ticket

    (tmp_path / "docs").mkdir()
    raw = "Cookie: session=fake-private-session-value"
    (tmp_path / "docs" / "context.md").write_text(raw)
    ticket = replace(
        build_ticket(base_sha="a" * 40),
        evidence_files=("docs/context.md",),
        evidence_hashes={"docs/context.md": core.sha256_text(raw)},
    )
    evidence = prepare._evidence(ticket, tmp_path)
    assert "fake-private-session-value" not in evidence["user_prompt"]


def test_rehearsal_accepts_existing_branch_but_rejects_new_one(monkeypatch, tmp_path):
    import subprocess

    import alert_fix_rehearsal as rehearsal

    monkeypatch.setattr(
        rehearsal,
        "_git",
        lambda *_args: subprocess.CompletedProcess([], 0, "refs/heads/fix/existing\n", ""),
    )
    rehearsal.check_branches(tmp_path, "refs/heads/fix/existing")
    with pytest.raises(SystemExit, match="branche"):
        rehearsal.check_branches(tmp_path, "refs/heads/main")

"""Tests du garde hook (entrées JSON conformes à la doc Cursor)."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import task_guard as tg  # noqa: E402
import task_verdict as tv  # noqa: E402

CONV = "conv-test"


@pytest.fixture
def active_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tv.TaskState:
    autonomy = tmp_path / "autonomy"
    autonomy.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "allowed.txt").write_text("ok\n", encoding="utf-8")
    task_path = tmp_path / "task.toml"
    task_path.write_text(
        "\n".join(
            [
                'id = "g1"',
                'base_sha = "abc"',
                'objective = "o"',
                'allowed_write = ["allowed.txt"]',
                'proof = ["ruff check ."]',
                "allow_subagents = false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = tv.load_task(task_path)
    hashes = tv.trust_script_hashes(ROOT)
    state = tv.TaskState(
        task_id="g1",
        fingerprint=task.fingerprint(task_path),
        base_sha="abc",
        trust_root=str(ROOT),
        worktree_path=str(worktree),
        task_path=str(task_path),
        status="active",
        started_at=tv.utc_now_iso(),
        approved_at=tv.utc_now_iso(),
        trust_script_hashes=hashes,
        conversation_id=CONV,
    )
    tv.save_state(tv.state_path(autonomy, "g1"), state)
    approval = tv.ApprovalRecord(
        task_id="g1",
        fingerprint=state.fingerprint,
        base_sha="abc",
        task_path=str(task_path),
        trust_root=str(ROOT),
        approved_at=state.approved_at,
        trust_script_hashes=hashes,
    )
    approval_path = tv.approval_path(autonomy, "g1")
    approval_path.parent.mkdir(parents=True, exist_ok=True)
    approval_path.write_text(
        json.dumps(approval.to_json(), indent=2) + "\n",
        encoding="utf-8",
    )
    pointer = tv.active_pointer_path(autonomy)
    pointer.write_text(json.dumps({"task_id": "g1"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(tv, "default_autonomy_home", lambda: autonomy)
    return state


def autonomy_home() -> Path:
    return tv.default_autonomy_home()


def hook_payload(state: tv.TaskState, event: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "hook_event_name": event,
        "conversation_id": CONV,
        "workspace_roots": [state.worktree_path],
        "cwd": state.worktree_path,
    }
    payload.update(extra)
    return payload


@pytest.mark.usefixtures("active_state")
def test_hook_denies_ssh(active_state: tv.TaskState) -> None:
    payload = hook_payload(active_state, "beforeShellExecution", command="ssh -V")
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"


@pytest.mark.usefixtures("active_state")
def test_hook_denies_approve_subcommand(active_state: tv.TaskState) -> None:
    payload = hook_payload(
        active_state,
        "beforeShellExecution",
        command="python scripts/task_verdict.py approve /tmp/task.toml",
    )
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"


@pytest.mark.usefixtures("active_state")
def test_hook_allows_git_status(active_state: tv.TaskState) -> None:
    payload = hook_payload(active_state, "beforeShellExecution", command="git status --short")
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "allow"


@pytest.mark.usefixtures("active_state")
def test_hook_write_outside_allowed(active_state: tv.TaskState) -> None:
    payload = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Write",
        tool_input={"file_path": str(Path(active_state.worktree_path) / "secret.txt")},
        tool_use_id="tu-1",
    )
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"


@pytest.mark.usefixtures("active_state")
def test_hook_write_allowed_relative(active_state: tv.TaskState) -> None:
    payload = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Write",
        tool_input={"file_path": "allowed.txt"},
        tool_use_id="tu-2",
    )
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "allow"


@pytest.mark.usefixtures("active_state")
def test_hook_denies_subagent(active_state: tv.TaskState) -> None:
    payload = hook_payload(
        active_state,
        "subagentStart",
        subagent_type="generalPurpose",
        parent_conversation_id=CONV,
        tool_call_id="tc-1",
    )
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"


@pytest.mark.usefixtures("active_state")
def test_hook_denies_env_read(active_state: tv.TaskState) -> None:
    payload = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Read",
        tool_input={"file_path": str(Path(active_state.worktree_path) / ".env")},
        tool_use_id="tu-3",
    )
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"


@pytest.mark.usefixtures("active_state")
def test_hook_stopped_allows_only_status(active_state: tv.TaskState) -> None:
    state_path = tv.state_path(autonomy_home(), "g1")
    state = tv.load_state(state_path)
    state.status = "stopped"
    tv.save_state(state_path, state)
    deny = tg.handle_hook(
        hook_payload(active_state, "beforeShellExecution", command="make test"),
        autonomy_home(),
    )
    assert deny["permission"] == "deny"
    allow = tg.handle_hook(
        hook_payload(
            active_state,
            "beforeShellExecution",
            command=f"python {ROOT / 'scripts' / 'task_verdict.py'} status g1",
        ),
        autonomy_home(),
    )
    assert allow["permission"] == "allow"


@pytest.mark.usefixtures("active_state")
def test_main_reads_stdin_json(active_state: tv.TaskState, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(hook_payload(active_state, "beforeShellExecution", command="ssh -V"))

    class _Stdin:
        @staticmethod
        def read() -> str:
            return payload

    monkeypatch.setattr(sys, "stdin", _Stdin())
    assert tg.main() == 0


def test_unknown_event_denied_for_linked_session(active_state: tv.TaskState) -> None:
    payload = hook_payload(active_state, "beforeShellExecution")
    payload["hook_event_name"] = "before_shell_execution"
    payload["command"] = "git status"
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"


def test_legacy_aliases_denied(active_state: tv.TaskState) -> None:
    payload = {
        "event": "beforeShellExecution",
        "command": "git status",
        "conversation_id": CONV,
        "workspace_roots": [active_state.worktree_path],
        "cwd": active_state.worktree_path,
    }
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"


def test_unknown_tool_and_mcp_denied(active_state: tv.TaskState) -> None:
    unknown = hook_payload(
        active_state,
        "preToolUse",
        tool_name="WebFetch",
        tool_input={"url": "https://example.test"},
        tool_use_id="tu-x",
    )
    mcp = hook_payload(
        active_state,
        "preToolUse",
        tool_name="MCP:browser",
        tool_input={"url": "https://example.test"},
        tool_use_id="tu-m",
    )
    assert tg.handle_hook(unknown, autonomy_home())["permission"] == "deny"
    assert tg.handle_hook(mcp, autonomy_home())["permission"] == "deny"


def test_missing_tool_input_denied(active_state: tv.TaskState) -> None:
    payload = hook_payload(active_state, "preToolUse", tool_name="Write", tool_use_id="tu-z")
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"


def test_other_session_is_not_blocked(active_state: tv.TaskState) -> None:
    payload = {
        "hook_event_name": "beforeShellExecution",
        "conversation_id": "autre-session",
        "workspace_roots": ["/tmp/autre-projet"],
        "cwd": "/tmp/autre-projet",
        "command": "ssh -V",
    }
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "allow"
    state = tv.load_state(tv.state_path(autonomy_home(), active_state.task_id))
    assert state.tool_call_count == 0


def test_cwd_outside_worktree_denied(active_state: tv.TaskState) -> None:
    payload = hook_payload(active_state, "beforeShellExecution", command="pwd", cwd="/tmp")
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"


def test_second_conversation_denied(active_state: tv.TaskState) -> None:
    state_path = tv.state_path(autonomy_home(), "g1")
    state = tv.load_state(state_path)
    state.conversation_id = None
    tv.save_state(state_path, state)
    first = hook_payload(active_state, "beforeShellExecution", command="git status")
    first["conversation_id"] = "conv-a"
    assert tg.handle_hook(first, autonomy_home())["permission"] == "allow"
    second = hook_payload(active_state, "beforeShellExecution", command="git status")
    second["conversation_id"] = "conv-b"
    assert tg.handle_hook(second, autonomy_home())["permission"] == "deny"


def test_modified_task_or_missing_approval_denied(active_state: tv.TaskState) -> None:
    path = Path(active_state.task_path)
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    denied = tg.handle_hook(
        hook_payload(active_state, "beforeShellExecution", command="git status"),
        autonomy_home(),
    )
    assert denied["permission"] == "deny"
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
    tv.approval_path(autonomy_home(), "g1").unlink()
    denied_approval = tg.handle_hook(
        hook_payload(active_state, "beforeShellExecution", command="git status"),
        autonomy_home(),
    )
    assert denied_approval["permission"] == "deny"


def test_tool_cap_stops_before_action(active_state: tv.TaskState) -> None:
    state_path = tv.state_path(autonomy_home(), "g1")
    state = tv.load_state(state_path)
    task = tv.load_task(Path(state.task_path))
    state.tool_call_count = task.max_tool_calls
    tv.save_state(state_path, state)
    payload = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Write",
        tool_input={"file_path": "allowed.txt"},
        tool_use_id="tu-cap",
    )
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"
    saved = tv.load_state(state_path)
    assert saved.status == "stopped"
    assert saved.tool_call_count == task.max_tool_calls


def test_shell_pair_counts_once(active_state: tv.TaskState) -> None:
    pre = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Shell",
        tool_input={"command": "git status --short"},
        tool_use_id="tu-shell",
    )
    before = hook_payload(active_state, "beforeShellExecution", command="git status --short")
    assert tg.handle_hook(pre, autonomy_home())["permission"] == "allow"
    assert tg.handle_hook(before, autonomy_home())["permission"] == "allow"
    state = tv.load_state(tv.state_path(autonomy_home(), "g1"))
    assert state.tool_call_count == 1


def test_bypass_strings_are_not_executed(active_state: tv.TaskState) -> None:
    samples = (
        "ssh -V",
        "rsync --version",
        "docker --version",
        "git status && ssh -V",
        "cd /tmp && pwd",
        "pytest tests/unit/foo.py; rm -rf /",
        "git status # ssh -V",
    )
    for command in samples:
        assert not tv.shell_command_allowed(
            command,
            Path(active_state.trust_root),
            task_id=active_state.task_id,
            worktree=Path(active_state.worktree_path),
        )


def test_ready_for_review_blocks_writes(active_state: tv.TaskState) -> None:
    state_path = tv.state_path(autonomy_home(), "g1")
    state = tv.load_state(state_path)
    state.status = "ready_for_review"
    state.review_digest = "abc"
    tv.save_state(state_path, state)
    write = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Write",
        tool_input={"file_path": "allowed.txt"},
        tool_use_id="tu-ready",
    )
    assert tg.handle_hook(write, autonomy_home())["permission"] == "deny"
    status = hook_payload(active_state, "beforeShellExecution", command="git status --short")
    assert tg.handle_hook(status, autonomy_home())["permission"] == "allow"


def test_linked_session_denies_missing_or_changed_roots(active_state: tv.TaskState) -> None:
    missing = hook_payload(active_state, "beforeShellExecution", command="git status")
    missing.pop("workspace_roots")
    assert tg.handle_hook(missing, autonomy_home())["permission"] == "deny"
    changed = hook_payload(
        active_state,
        "beforeShellExecution",
        command="git status",
        workspace_roots=["/tmp/autre-projet"],
    )
    assert tg.handle_hook(changed, autonomy_home())["permission"] == "deny"


def test_repeated_tool_use_counts_once(active_state: tv.TaskState) -> None:
    payload = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Write",
        tool_input={"file_path": "allowed.txt"},
        tool_use_id="tu-repeat",
    )
    assert tg.handle_hook(payload, autonomy_home())["permission"] == "allow"
    assert tg.handle_hook(payload, autonomy_home())["permission"] == "allow"
    state = tv.load_state(tv.state_path(autonomy_home(), "g1"))
    assert state.tool_call_count == 1
    assert state.status == "active"


def test_one_slot_allows_a_single_call(active_state: tv.TaskState) -> None:
    state_path = tv.state_path(autonomy_home(), "g1")
    state = tv.load_state(state_path)
    task = tv.load_task(Path(state.task_path))
    state.tool_call_count = task.max_tool_calls - 1
    tv.save_state(state_path, state)
    first = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Write",
        tool_input={"file_path": "allowed.txt"},
        tool_use_id="tu-last",
    )
    second = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Write",
        tool_input={"file_path": "allowed.txt"},
        tool_use_id="tu-overflow",
    )
    assert tg.handle_hook(first, autonomy_home())["permission"] == "allow"
    assert tg.handle_hook(second, autonomy_home())["permission"] == "deny"
    saved = tv.load_state(state_path)
    assert saved.tool_call_count == task.max_tool_calls
    assert saved.status == "stopped"


def test_last_shell_call_stays_allowed_for_its_pair(active_state: tv.TaskState) -> None:
    state_path = tv.state_path(autonomy_home(), "g1")
    state = tv.load_state(state_path)
    task = tv.load_task(Path(state.task_path))
    state.tool_call_count = task.max_tool_calls - 1
    tv.save_state(state_path, state)
    command = "git status --short"
    pre = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Shell",
        tool_input={"command": command},
        tool_use_id="tu-pair",
    )
    before = hook_payload(active_state, "beforeShellExecution", command=command)
    assert tg.handle_hook(pre, autonomy_home())["permission"] == "allow"
    assert tg.handle_hook(before, autonomy_home())["permission"] == "allow"
    saved = tv.load_state(state_path)
    assert saved.tool_call_count == task.max_tool_calls
    assert saved.status == "active"


def test_grep_and_rg_reject_secret_and_escaping_link(active_state: tv.TaskState) -> None:
    worktree = Path(active_state.worktree_path)
    outside = worktree.parent / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")
    link = worktree / "escape"
    link.symlink_to(outside)
    secret = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Grep",
        tool_input={"pattern": "token", "path": str(worktree / ".env")},
        tool_use_id="tu-grep-env",
    )
    escaping = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Grep",
        tool_input={"pattern": "x", "path": str(link)},
        tool_use_id="tu-grep-link",
    )
    assert tg.handle_hook(secret, autonomy_home())["permission"] == "deny"
    assert tg.handle_hook(escaping, autonomy_home())["permission"] == "deny"
    trust = Path(active_state.trust_root)
    assert not tv.shell_command_allowed(
        "rg token .env",
        trust,
        task_id=active_state.task_id,
        worktree=worktree,
    )
    assert not tv.shell_command_allowed(
        "rg token escape",
        trust,
        task_id=active_state.task_id,
        worktree=worktree,
    )


def test_shell_rejects_pytest_outside_the_approved_proof(active_state: tv.TaskState) -> None:
    trust = Path(active_state.trust_root)
    venv_pytest = trust / ".venv" / "bin" / "pytest"
    foreign = trust / "other" / "pytest"
    approved = f"{venv_pytest} tests/unit/foo.py -q"
    missing = f"{venv_pytest} tests/unit/other.py -q"
    bare = "pytest tests/unit/foo.py -q"
    alien = f"{foreign} tests/unit/foo.py -q"
    for command in (approved, missing, bare, alien):
        payload = hook_payload(active_state, "beforeShellExecution", command=command)
        assert tg.handle_hook(payload, autonomy_home())["permission"] == "deny"


def test_failed_status_rejects_modification(active_state: tv.TaskState) -> None:
    state_path = tv.state_path(autonomy_home(), "g1")
    state = tv.load_state(state_path)
    state.status = "failed"
    state.tool_call_count = 4
    tv.save_state(state_path, state)
    payload = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Write",
        tool_input={"file_path": "allowed.txt"},
        tool_use_id="tu-failed",
    )
    assert tg.handle_hook(payload, autonomy_home())["permission"] == "deny"
    saved = tv.load_state(state_path)
    assert saved.status == "failed"
    assert saved.tool_call_count == 4


def test_git_metadata_write_is_denied(
    active_state: tv.TaskState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = tv.load_task(Path(active_state.task_path))
    task.allowed_write = ["allowed.txt", ".git/config"]
    monkeypatch.setattr(tg, "load_task", lambda _path: task)
    monkeypatch.setattr(tv, "load_task", lambda _path: task)
    payload = hook_payload(
        active_state,
        "preToolUse",
        tool_name="Write",
        tool_input={"file_path": ".git/config"},
        tool_use_id="tu-git",
    )
    result = tg.handle_hook(payload, autonomy_home())
    assert result["permission"] == "deny"


def test_one_remaining_slot_is_reserved_once(active_state: tv.TaskState) -> None:
    state_path = tv.state_path(autonomy_home(), "g1")
    state = tv.load_state(state_path)
    task = tv.load_task(Path(state.task_path))
    state.tool_call_count = task.max_tool_calls - 1
    tv.save_state(state_path, state)
    results: list[str] = []
    barrier = threading.Barrier(2)

    def call(tool_use_id: str) -> None:
        barrier.wait()
        payload = hook_payload(
            active_state,
            "preToolUse",
            tool_name="Write",
            tool_input={"file_path": "allowed.txt"},
            tool_use_id=tool_use_id,
        )
        results.append(tg.handle_hook(payload, autonomy_home())["permission"])

    threads = [
        threading.Thread(target=call, args=(f"tu-race-{index}",))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    saved = tv.load_state(state_path)
    assert results.count("allow") == 1
    assert saved.tool_call_count == task.max_tool_calls
    assert saved.status == "stopped"


@pytest.mark.usefixtures("active_state")
def test_locked_state_keeps_every_increment() -> None:
    home = autonomy_home()

    def bump() -> None:
        for _ in range(40):
            with tv.locked_state(home, "g1") as state:
                state.tool_call_count += 1

    threads = [threading.Thread(target=bump) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state = tv.load_state(tv.state_path(home, "g1"))
    assert state.tool_call_count == 80

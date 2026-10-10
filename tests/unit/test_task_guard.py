"""Tests du garde hook (entrées JSON synthétiques)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import task_guard as tg  # noqa: E402
import task_verdict as tv  # noqa: E402


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
    state = tv.TaskState(
        task_id="g1",
        fingerprint=tv.load_task(task_path).fingerprint(task_path),
        base_sha="abc",
        trust_root=str(ROOT),
        worktree_path=str(worktree),
        task_path=str(task_path),
        status="active",
        started_at=tv.utc_now_iso(),
        approved_at=tv.utc_now_iso(),
    )
    tv.save_state(tv.state_path(autonomy, "g1"), state)
    pointer = tv.active_pointer_path(autonomy)
    pointer.write_text(json.dumps({"task_id": "g1"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(tv, "default_autonomy_home", lambda: autonomy)
    return state


def autonomy_home() -> Path:
    return tv.default_autonomy_home()


@pytest.mark.usefixtures("active_state")
def test_hook_denies_ssh() -> None:
    payload = {"event": "beforeShellExecution", "command": "ssh -V"}
    result = tg.handle_hook("beforeShellExecution", payload, autonomy_home())
    assert result["permission"] == "deny"


@pytest.mark.usefixtures("active_state")
def test_hook_denies_approve_subcommand() -> None:
    payload = {
        "event": "beforeShellExecution",
        "command": "python scripts/task_verdict.py approve /tmp/task.toml",
    }
    result = tg.handle_hook("beforeShellExecution", payload, autonomy_home())
    assert result["permission"] == "deny"


@pytest.mark.usefixtures("active_state")
def test_hook_allows_git_status() -> None:
    payload = {"event": "beforeShellExecution", "command": "git status --short"}
    result = tg.handle_hook("beforeShellExecution", payload, autonomy_home())
    assert result["permission"] == "allow"


@pytest.mark.usefixtures("active_state")
def test_hook_write_outside_allowed(active_state: tv.TaskState) -> None:
    payload = {
        "event": "preToolUse",
        "tool_name": "Write",
        "path": str(Path(active_state.worktree_path) / "secret.txt"),
    }
    result = tg.handle_hook("preToolUse", payload, autonomy_home())
    assert result["permission"] == "deny"


@pytest.mark.usefixtures("active_state")
def test_hook_write_allowed_relative() -> None:
    payload = {"event": "preToolUse", "tool_name": "Write", "path": "allowed.txt"}
    result = tg.handle_hook("preToolUse", payload, autonomy_home())
    assert result["permission"] == "allow"


@pytest.mark.usefixtures("active_state")
def test_hook_denies_subagent() -> None:
    payload = {"event": "subagentStart", "tool_name": "Task"}
    result = tg.handle_hook("subagentStart", payload, autonomy_home())
    assert result["permission"] == "deny"


@pytest.mark.usefixtures("active_state")
def test_hook_denies_env_read() -> None:
    payload = {"event": "preToolUse", "tool_name": "Read", "path": "/proj/.env"}
    result = tg.handle_hook("preToolUse", payload, autonomy_home())
    assert result["permission"] == "deny"


@pytest.mark.usefixtures("active_state")
def test_hook_stopped_allows_only_status() -> None:
    state_path = tv.state_path(autonomy_home(), "g1")
    state = tv.load_state(state_path)
    state.status = "stopped"
    tv.save_state(state_path, state)
    deny = tg.handle_hook(
        "beforeShellExecution",
        {"event": "beforeShellExecution", "command": "make test"},
        autonomy_home(),
    )
    assert deny["permission"] == "deny"
    allow = tg.handle_hook(
        "beforeShellExecution",
        {"event": "beforeShellExecution", "command": "python scripts/task_verdict.py status g1"},
        autonomy_home(),
    )
    assert allow["permission"] == "allow"


@pytest.mark.usefixtures("active_state")
def test_main_reads_stdin_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"event": "beforeShellExecution", "command": "ssh -V"})

    class _Stdin:
        @staticmethod
        def read() -> str:
            return payload

    monkeypatch.setattr(sys, "stdin", _Stdin())
    assert tg.main() == 0

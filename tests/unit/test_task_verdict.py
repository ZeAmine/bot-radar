"""Tests du garde-fou d'autonomie (dépôts jetables, sans réseau)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import task_verdict as tv  # noqa: E402


def init_git_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    (path / "notes.md").write_text("note\n", encoding="utf-8")
    tests = path / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_old.py").write_text(
        "def test_old() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md", "notes.md", "tests"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def write_task_toml(path: Path, base_sha: str, allowed: list[str], proof: list[str]) -> None:
    lines = [
        'id = "t1"',
        f'base_sha = "{base_sha}"',
        'objective = "test"',
        "allowed_write = [",
        *[f'  "{item}",' for item in allowed],
        "]",
        "proof = [",
        *[f'  "{item}",' for item in proof],
        "]",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def approve_args(env: dict[str, Path]) -> SimpleNamespace:
    return SimpleNamespace(task_file=env["task_file"], trust_root=env["trust"])


def start_args(env: dict[str, Path], consumption: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(task_file=env["task_file"], consumption_before=consumption)


@pytest.fixture
def autonomy_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    trust = tmp_path / "trust"
    trust.mkdir()
    init_git_repo(trust)
    (trust / "scripts").mkdir()
    (trust / "scripts" / "task_verdict.py").write_text(
        (ROOT / "scripts" / "task_verdict.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (trust / "scripts" / "task_guard.py").write_text(
        (ROOT / "scripts" / "task_guard.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "scripts"], cwd=trust, check=True)
    subprocess.run(["git", "commit", "-m", "scripts"], cwd=trust, check=True, capture_output=True)
    base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=trust, text=True).strip()

    autonomy = tmp_path / "autonomy"
    tasks_root = tmp_path / "tasks"
    monkeypatch.setattr(tv, "default_autonomy_home", lambda: autonomy)
    monkeypatch.setattr(tv, "default_tasks_root", lambda: tasks_root)
    monkeypatch.setattr(tv, "REPO_ROOT", trust)

    task_file = tmp_path / "task.toml"
    write_task_toml(task_file, base_sha, allowed=["README.md"], proof=["ruff check ."])
    return {
        "trust": trust,
        "autonomy": autonomy,
        "tasks_root": tasks_root,
        "task_file": task_file,
        "base_sha": base_sha,
    }


def boot_task(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tv, "collect_test_count", lambda _wt: 10)
    tv.cmd_approve(approve_args(env))
    tv.cmd_start(start_args(env))


def test_proof_whitelist_rejects_shell_chaining() -> None:
    assert tv.proof_command_allowed("pytest tests/unit/foo.py -v")
    assert not tv.proof_command_allowed("pytest foo; rm -rf /")
    assert not tv.proof_command_allowed("pytest tests/unit/foo.py -p evil")
    assert not tv.proof_command_allowed("mypy app scripts")


def test_shell_allowed_and_forbidden() -> None:
    assert tv.shell_command_allowed("git status", ROOT)
    assert not tv.shell_command_allowed("ssh -V", ROOT)
    assert not tv.shell_command_allowed("python scripts/task_verdict.py approve task.toml", ROOT)
    assert not tv.shell_command_allowed("git status && ssh -V", ROOT)


def test_resolve_under_root_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    link = root / "link"
    link.symlink_to(outside)
    assert tv.resolve_under_root(root, "link") is None


def test_approve_and_start_creates_worktree(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    state = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    assert state.verify_count == 0
    assert Path(state.worktree_path).is_dir()


def test_resume_keeps_counters(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    state_path = tv.state_path(autonomy_env["autonomy"], "t1")
    state = tv.load_state(state_path)
    state.verify_count = 2
    state.tool_call_count = 5
    state.status = "stopped"
    tv.save_state(state_path, state)
    assert tv.cmd_resume(SimpleNamespace(task_id="t1")) == 0
    resumed = tv.load_state(state_path)
    assert resumed.verify_count == 2
    assert resumed.tool_call_count == 5


def test_verify_detects_out_of_scope_file(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    state = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    worktree = Path(state.worktree_path)
    evil = worktree / "scripts" / "evil.py"
    evil.parent.mkdir(parents=True, exist_ok=True)
    evil.write_text("# x\n", encoding="utf-8")
    task = tv.load_task(autonomy_env["task_file"])
    ok, errors = tv.verify_task(state, task, autonomy_env["autonomy"])
    assert not ok
    assert any("interdit" in err for err in errors)


def test_verify_detects_trust_script_tampering(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    state_path = tv.state_path(autonomy_env["autonomy"], "t1")
    state = tv.load_state(state_path)
    state.trust_script_hashes["scripts/task_verdict.py"] = "deadbeef"
    task = tv.load_task(autonomy_env["task_file"])
    ok, errors = tv.verify_task(state, task, autonomy_env["autonomy"])
    assert not ok
    assert any("confiance" in err for err in errors)


def test_verify_lists_untracked_and_rename(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    state = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    worktree = Path(state.worktree_path)
    (worktree / "new.txt").write_text("n\n", encoding="utf-8")
    subprocess.run(["git", "mv", "README.md", "README-renamed.md"], cwd=worktree, check=True)
    changes = tv.list_worktree_changes(worktree, state.base_sha)
    paths = {rel for _status, rel in changes}
    assert "new.txt" in paths
    assert "README.md" in paths
    assert "README-renamed.md" in paths


def test_repeat_error_stops_on_second_verify(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    monkeypatch.setattr(tv, "verify_task", lambda *_a, **_k: (False, ["même problème"]))
    assert tv.cmd_verify(SimpleNamespace(task_id="t1")) == 1
    assert tv.cmd_verify(SimpleNamespace(task_id="t1")) == 1
    state = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    assert state.status == "stopped"
    assert state.repeat_error_count >= 2


def test_fingerprint_change_invalidates_task(autonomy_env: dict[str, Path]) -> None:
    task = tv.load_task(autonomy_env["task_file"])
    fp1 = task.fingerprint(autonomy_env["task_file"])
    path = autonomy_env["task_file"]
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    fp2 = task.fingerprint(path)
    assert fp1 != fp2


def test_load_task_rejects_empty_allowed_write(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(
        'id="x"\nbase_sha="abc"\nobjective="o"\nallowed_write=[]\nproof=["ruff check ."]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="allowed_write"):
        tv.load_task(path)


def test_rename_source_and_deletion_are_in_scope(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    state = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    worktree = Path(state.worktree_path)
    subprocess.run(["git", "mv", "README.md", "README-renamed.md"], cwd=worktree, check=True)
    (worktree / "notes.md").unlink()
    changes = tv.list_worktree_changes(worktree, state.base_sha)
    paths = {rel for _status, rel in changes}
    assert "README.md" in paths
    assert "README-renamed.md" in paths
    task = tv.load_task(autonomy_env["task_file"])
    ok, errors = tv.verify_task(state, task, autonomy_env["autonomy"])
    assert not ok
    joined = "\n".join(errors)
    assert "README-renamed.md" in joined
    assert "notes.md" in joined


def test_untracked_binary_and_secret_fail_closed(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    state_path = tv.state_path(autonomy_env["autonomy"], "t1")
    state = tv.load_state(state_path)
    worktree = Path(state.worktree_path)
    (worktree / "blob.bin").write_bytes(b"\x00\x01")
    (worktree / "leak.txt").write_text("sk-abcdefghij\n", encoding="utf-8")
    task = tv.load_task(autonomy_env["task_file"])
    task.allowed_write = ["README.md", "blob.bin", "leak.txt"]
    ok, errors = tv.verify_task(state, task, autonomy_env["autonomy"])
    assert not ok
    assert any("secret" in err for err in errors)
    assert any("binaire" in err or "diff" in err for err in errors)


def test_existing_test_edit_stops(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    state = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    worktree = Path(state.worktree_path)
    target = worktree / "tests" / "unit" / "test_old.py"
    target.write_text("def test_old() -> None:\n    assert False\n", encoding="utf-8")
    task = tv.load_task(autonomy_env["task_file"])
    task.allowed_write = ["README.md", "tests/unit/test_old.py"]
    ok, errors = tv.verify_task(state, task, autonomy_env["autonomy"])
    assert not ok
    assert any("revue humaine" in err for err in errors)
    assert tv.cmd_verify(SimpleNamespace(task_id="t1")) == 1
    saved = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    assert saved.status == "stopped"


def test_untracked_symlink_outside_is_rejected(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    state = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    worktree = Path(state.worktree_path)
    outside = worktree.parent / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")
    (worktree / "link").symlink_to(outside)
    task = tv.load_task(autonomy_env["task_file"])
    task.allowed_write = ["README.md", "link"]
    ok, errors = tv.verify_task(state, task, autonomy_env["autonomy"])
    assert not ok
    assert any("lien symbolique" in err for err in errors)


def test_static_failure_does_not_collect_pytest(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    state = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    worktree = Path(state.worktree_path)
    marker = worktree / "sentinel.txt"
    conf = worktree / "tests" / "conftest.py"
    conf.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def explode(_worktree: Path) -> int:
        calls.append("collect")
        return 10

    def explode_proof(*_args: object, **_kwargs: object) -> list[tuple[str, int, str]]:
        calls.append("proof")
        return []

    monkeypatch.setattr(tv, "collect_test_count", explode)
    monkeypatch.setattr(tv, "run_proof_commands", explode_proof)
    task = tv.load_task(autonomy_env["task_file"])
    ok, errors = tv.verify_task(state, task, autonomy_env["autonomy"])
    assert not ok
    assert errors
    assert calls == []
    assert not marker.exists()


def test_run_bounded_reports_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["git", "status"], timeout=1)

    monkeypatch.setattr(tv.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="timeout"):
        tv.run_bounded(["git", "status"], tmp_path)


def test_ready_digest_invalidation_and_report(
    autonomy_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_task(autonomy_env, monkeypatch)
    monkeypatch.setattr(
        tv,
        "run_proof_commands",
        lambda *_a, **_k: [("ruff check .", 0, "all good")],
    )
    assert tv.cmd_verify(SimpleNamespace(task_id="t1")) == 0
    state = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    assert state.status == "ready_for_review"
    assert state.evidence
    first_digest = state.review_digest
    worktree = Path(state.worktree_path)
    readme = worktree / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "suite\n", encoding="utf-8")
    assert tv.cmd_verify(SimpleNamespace(task_id="t1")) == 0
    refreshed = tv.load_state(tv.state_path(autonomy_env["autonomy"], "t1"))
    assert refreshed.review_digest != first_digest
    journal = tv.journal_path(autonomy_env["autonomy"], "t1").read_text(encoding="utf-8")
    assert "invalid" in journal
    assert tv.cmd_report(SimpleNamespace(task_id="t1", consumption_after=None)) == 0
    report = tv.report_path(autonomy_env["autonomy"], "t1").read_text(encoding="utf-8")
    assert "ruff check ." in report
    assert "0" in report
    readme.write_text(readme.read_text(encoding="utf-8") + "encore\n", encoding="utf-8")
    assert tv.cmd_report(SimpleNamespace(task_id="t1", consumption_after=None)) == 0
    stale = tv.report_path(autonomy_env["autonomy"], "t1").read_text(encoding="utf-8")
    assert "périmée" in stale

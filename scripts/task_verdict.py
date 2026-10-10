#!/usr/bin/env python
"""Garde-fou d'autonomie : fiche, worktree, vérification et rapport."""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
SUBPROCESS_TIMEOUT_SECONDS = 120

CI_TOKENS = {
    "TELEGRAM_BOT_TOKEN": "123456789:AAG_ci_test_dummy_token_abc123",
    "TELEGRAM_WEBHOOK_SECRET": "ci_webhook_secret_test",
    "INGEST_API_TOKEN": "ci_ingest_token_test",
    "INTERNAL_API_TOKEN": "ci_internal_token_test",
}

DEFAULT_FORBIDDEN: tuple[str, ...] = (
    ".env",
    ".env.*",
    "scripts/**",
    "alembic/**",
    ".github/**",
    "Makefile",
    "pyproject.toml",
    "requirements.lock",
    "vendor/**",
    ".cursor/**",
    "app/services/vinted_scraper.py",
    "app/providers/chasse_aux_livres.py",
    "app/services/price_calculator.py",
    "CONTEXT.md",
    "memory.md",
    "subjects.md",
    ".git",
    ".git/**",
    "**/conftest.py",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
)

DEFAULT_MAX_MINUTES = 45
DEFAULT_MAX_CYCLES = 4
DEFAULT_MAX_TOOL_CALLS = 120
DEFAULT_MAX_DIFF_LINES = 300

TRUST_SCRIPTS = ("scripts/task_verdict.py", "scripts/task_guard.py")

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)(password|secret|token)=\S+"),
)

SHELL_META = set(";|&`$<>\n")
PYTEST_FLAGS = frozenset({"-v", "-q", "-x", "--tb=short", "--tb=line", "--tb=no"})
GIT_OPTIONS = {
    "status": frozenset({"--short", "--porcelain", "-sb", "-s"}),
    "diff": frozenset({"--stat", "--name-status", "--numstat", "--name-only"}),
    "log": frozenset({"--oneline", "--stat"}),
    "show": frozenset({"--stat", "--name-only", "--format=medium"}),
}
RG_OPTIONS = frozenset(
    {"-n", "-i", "-F", "-w", "-l", "--line-number", "--fixed-strings", "--word-regexp"}
)
LS_OPTIONS = frozenset({"-l", "-a", "-la", "-al", "-1", "-h"})
VERDICT_SUBCOMMANDS = frozenset({"verify", "status", "note", "report"})
READONLY_VERDICT = frozenset({"status", "note", "report"})

AGENT_BLOCKED_SUBCOMMANDS = frozenset({"approve", "start", "resume", "reset-counters"})
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
READ_FORBIDDEN_RE = re.compile(r"(^|/)\.env(\.|$)|/\.ssh/|(^|/)\.git(/|$)")
CONTROL_MARKERS = (
    "hors périmètre",
    "secret",
    "binaire",
    "revue humaine",
    "lien symbolique",
    "confiance",
    "métadonnées git",
    "contenu modifié",
)
Status = Literal["active", "stopped", "ready_for_review", "failed"]
ShellMode = Literal["active", "review", "stopped"]


def default_autonomy_home() -> Path:
    env = os.environ.get("BOTRADAR_AUTONOMY_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cursor/projects/Users-aminezegmou-Projets-bot-radar/autonomy"


def default_tasks_root() -> Path:
    env = os.environ.get("BOTRADAR_TASKS_ROOT")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Projets/bot-radar-tasks"


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[secret]", redacted)
    return redacted


@dataclass
class TaskSpec:
    id: str
    base_sha: str
    objective: str
    allowed_write: list[str]
    proof: list[str]
    non_goals: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    max_minutes: int = DEFAULT_MAX_MINUTES
    max_cycles: int = DEFAULT_MAX_CYCLES
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_diff_lines: int = DEFAULT_MAX_DIFF_LINES
    escalate_on: str = ""
    human_review: list[str] = field(default_factory=list)
    allow_subagents: bool = False

    def all_forbidden(self) -> list[str]:
        return list(DEFAULT_FORBIDDEN) + list(self.forbidden)

    def fingerprint(self, task_path: Path) -> str:
        return sha256_file(task_path)


@dataclass
class TaskState:
    task_id: str
    fingerprint: str
    base_sha: str
    trust_root: str
    worktree_path: str
    task_path: str
    status: Status
    started_at: str
    approved_at: str
    verify_count: int = 0
    tool_call_count: int = 0
    last_error_signature: str | None = None
    repeat_error_count: int = 0
    consumption_before: str | None = None
    consumption_after: str | None = None
    stopped_reason: str | None = None
    trust_script_hashes: dict[str, str] = field(default_factory=dict)
    baseline_test_count: int = 0
    conversation_id: str | None = None
    review_digest: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    seen_tool_ids: list[str] = field(default_factory=list)
    reserved_shell_command: str | None = None
    reserved_shell_pending: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TaskState:
        evidence = data.get("evidence", [])
        seen = data.get("seen_tool_ids", [])
        return cls(
            task_id=str(data["task_id"]),
            fingerprint=str(data["fingerprint"]),
            base_sha=str(data["base_sha"]),
            trust_root=str(data["trust_root"]),
            worktree_path=str(data["worktree_path"]),
            task_path=str(data["task_path"]),
            status=data["status"],
            started_at=str(data["started_at"]),
            approved_at=str(data["approved_at"]),
            verify_count=int(data.get("verify_count", 0)),
            tool_call_count=int(data.get("tool_call_count", 0)),
            last_error_signature=data.get("last_error_signature"),
            repeat_error_count=int(data.get("repeat_error_count", 0)),
            consumption_before=data.get("consumption_before"),
            consumption_after=data.get("consumption_after"),
            stopped_reason=data.get("stopped_reason"),
            trust_script_hashes=dict(data.get("trust_script_hashes", {})),
            baseline_test_count=int(data.get("baseline_test_count", 0)),
            conversation_id=data.get("conversation_id"),
            review_digest=data.get("review_digest"),
            evidence=list(evidence) if isinstance(evidence, list) else [],
            seen_tool_ids=list(seen) if isinstance(seen, list) else [],
            reserved_shell_command=data.get("reserved_shell_command"),
            reserved_shell_pending=bool(data.get("reserved_shell_pending", False)),
        )


_deadline: contextvars.ContextVar[tuple[TaskState, TaskSpec] | None] = contextvars.ContextVar(
    "botradar_deadline",
    default=None,
)


@dataclass
class ApprovalRecord:
    task_id: str
    fingerprint: str
    base_sha: str
    task_path: str
    trust_root: str
    approved_at: str
    trust_script_hashes: dict[str, str]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GuardDecision:
    allowed: bool
    reason: str = ""

    def deny(self) -> bool:
        return not self.allowed


def _require_string_list(raw: dict[str, Any], key: str) -> list[str]:
    value = raw[key]
    items_ok = isinstance(value, list) and bool(value)
    items_ok = items_ok and all(isinstance(item, str) and item for item in value)
    if not items_ok:
        raise ValueError(f"{key} doit être une liste de chaînes non vides")
    return list(value)


def _optional_string_list(raw: dict[str, Any], key: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} doit être une liste de chaînes")
    return list(value)


def _positive_int(raw: dict[str, Any], key: str, default: int) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} doit être un entier positif")
    return value


def load_task(path: Path) -> TaskSpec:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    required = ("id", "base_sha", "objective", "allowed_write", "proof")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"champs manquants dans la fiche : {', '.join(missing)}")
    task_id = raw["id"]
    if not isinstance(task_id, str) or TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("identifiant de tâche invalide")
    allowed_write = _require_string_list(raw, "allowed_write")
    proof = [item.strip() for item in _require_string_list(raw, "proof")]
    for command in proof:
        if not proof_command_allowed(command):
            raise ValueError(f"commande de preuve non autorisée : {command}")
    if "allow_subagents" in raw and not isinstance(raw["allow_subagents"], bool):
        raise ValueError("allow_subagents doit être un booléen")
    objective = raw["objective"]
    base_sha = raw["base_sha"]
    if not isinstance(objective, str) or not objective:
        raise ValueError("objective doit être une chaîne non vide")
    if not isinstance(base_sha, str) or not base_sha:
        raise ValueError("base_sha doit être une chaîne non vide")
    return TaskSpec(
        id=task_id,
        base_sha=base_sha,
        objective=objective,
        allowed_write=allowed_write,
        proof=proof,
        non_goals=_optional_string_list(raw, "non_goals"),
        forbidden=_optional_string_list(raw, "forbidden"),
        max_minutes=_positive_int(raw, "max_minutes", DEFAULT_MAX_MINUTES),
        max_cycles=_positive_int(raw, "max_cycles", DEFAULT_MAX_CYCLES),
        max_tool_calls=_positive_int(raw, "max_tool_calls", DEFAULT_MAX_TOOL_CALLS),
        max_diff_lines=_positive_int(raw, "max_diff_lines", DEFAULT_MAX_DIFF_LINES),
        escalate_on=str(raw.get("escalate_on", "")),
        human_review=_optional_string_list(raw, "human_review"),
        allow_subagents=raw.get("allow_subagents", False) is True,
    )


def split_command(command: str) -> list[str] | None:
    stripped = command.strip()
    if not stripped or any(char in stripped for char in SHELL_META):
        return None
    try:
        argv = shlex.split(stripped)
    except ValueError:
        return None
    if not argv:
        return None
    return argv


def proof_command_allowed(command: str) -> bool:
    argv = split_command(command)
    if argv is None:
        return False
    if argv[0] == "pytest":
        return pytest_args_ok(argv[1:], None)
    if argv[0] == "ruff":
        return ruff_args_ok(argv[1:])
    return argv == ["mypy", "app"]


def pytest_args_ok(args: list[str], worktree: Path | None) -> bool:
    if not args:
        return False
    saw_path = False
    for arg in args:
        if arg in PYTEST_FLAGS:
            continue
        if arg.startswith("-") or arg.startswith("/") or ".." in Path(arg).parts:
            return False
        if arg != "tests" and not arg.startswith("tests/"):
            return False
        if worktree is not None and resolve_under_root(worktree, arg) is None:
            return False
        saw_path = True
    return saw_path


def ruff_args_ok(args: list[str]) -> bool:
    if len(args) < 2 or args[0] != "check":
        return False
    for arg in args[1:]:
        if not arg or arg.startswith("-") or arg.startswith("/") or ".." in Path(arg).parts:
            return False
    return True


def git_argv_allowed(argv: list[str]) -> bool:
    if len(argv) < 2 or argv[0] != "git" or argv[1] not in GIT_OPTIONS:
        return False
    subcommand = argv[1]
    options = GIT_OPTIONS[subcommand]
    index = 2
    while index < len(argv):
        arg = argv[index]
        if arg in options or arg in {"HEAD", "--"}:
            index += 1
            continue
        if subcommand == "log" and re.fullmatch(r"-n\d+", arg):
            index += 1
            continue
        next_is_number = index + 1 < len(argv) and argv[index + 1].isdigit()
        if subcommand == "log" and arg == "-n" and next_is_number:
            index += 2
            continue
        if subcommand in {"diff", "show", "log"} and re.fullmatch(r"[0-9a-fA-F]{7,40}", arg):
            index += 1
            continue
        return False
    return True


def _plain_arg_ok(arg: str) -> bool:
    return not arg.startswith("-") and ".." not in Path(arg).parts and not arg.startswith("/")


def rg_argv_allowed(argv: list[str], worktree: Path | None = None) -> bool:
    if not argv or argv[0] != "rg":
        return False
    positional: list[str] = []
    for arg in argv[1:]:
        if arg in RG_OPTIONS or arg == "--":
            continue
        if not _plain_arg_ok(arg):
            return False
        positional.append(arg)
    return all(blocked_read_reason(worktree, path) is None for path in positional[1:])


def ls_argv_allowed(argv: list[str]) -> bool:
    if not argv or argv[0] != "ls":
        return False
    for arg in argv[1:]:
        if arg in LS_OPTIONS:
            continue
        if not _plain_arg_ok(arg):
            return False
    return True


def _interpreters(trust_root: Path) -> set[str]:
    venv = trust_root / ".venv" / "bin"
    return {
        "python",
        "python3",
        str(venv / "python"),
        str(venv / "python3"),
    }


def verdict_argv_allowed(
    argv: list[str],
    trust_root: Path,
    task_id: str | None,
    *,
    readonly: bool,
) -> bool:
    if len(argv) < 4 or argv[0] not in _interpreters(trust_root):
        return False
    try:
        script = str(Path(argv[1]).resolve())
    except OSError:
        return False
    expected = str((trust_root / "scripts" / "task_verdict.py").resolve())
    if script != expected:
        return False
    subcommand = argv[2]
    if subcommand in AGENT_BLOCKED_SUBCOMMANDS:
        return False
    allowed = READONLY_VERDICT if readonly else VERDICT_SUBCOMMANDS
    if subcommand not in allowed:
        return False
    if task_id is not None and argv[3] != task_id:
        return False
    if task_id is None:
        return False
    extra = argv[4:]
    if subcommand == "note":
        return len(extra) == 1
    if subcommand == "report":
        return extra == [] or (len(extra) == 2 and extra[0] == "--consumption-after")
    return extra == []


def approved_proof_argv(
    argv: list[str],
    trust_root: Path,
    worktree: Path | None,
    proof: list[str],
) -> bool:
    if not argv or not Path(argv[0]).is_absolute():
        return False
    name = Path(argv[0]).name
    if name not in {"pytest", "ruff", "mypy"}:
        return False
    try:
        given = Path(argv[0]).resolve()
        expected = (trust_root / ".venv" / "bin" / name).resolve()
    except OSError:
        return False
    if given != expected:
        return False
    for item in proof:
        spec = split_command(item)
        if spec is None or spec[0] != name or spec[1:] != argv[1:]:
            continue
        if not proof_command_allowed(item):
            return False
        if name == "pytest":
            return pytest_args_ok(argv[1:], worktree)
        if name == "ruff":
            return ruff_args_ok(argv[1:])
        return argv[1:] == ["app"]
    return False


def shell_command_allowed(
    command: str,
    trust_root: Path,
    *,
    task_id: str | None = None,
    worktree: Path | None = None,
    mode: ShellMode = "active",
    proof: list[str] | None = None,
) -> bool:
    argv = split_command(command)
    if argv is None:
        return False
    if mode == "stopped":
        return verdict_argv_allowed(argv, trust_root, task_id, readonly=True)
    readonly = argv == ["pwd"] or git_argv_allowed(argv) or ls_argv_allowed(argv)
    readonly = readonly or rg_argv_allowed(argv, worktree)
    if readonly:
        return mode in {"active", "review"}
    if verdict_argv_allowed(argv, trust_root, task_id, readonly=mode != "active"):
        return True
    if mode != "active":
        return False
    return approved_proof_argv(argv, trust_root, worktree, proof or [])


def task_dir(autonomy_home: Path, task_id: str) -> Path:
    return autonomy_home / task_id


def state_path(autonomy_home: Path, task_id: str) -> Path:
    return task_dir(autonomy_home, task_id) / "state.json"


def approval_path(autonomy_home: Path, task_id: str) -> Path:
    return task_dir(autonomy_home, task_id) / "approval.json"


def active_pointer_path(autonomy_home: Path) -> Path:
    return autonomy_home / "active.json"


def journal_path(autonomy_home: Path, task_id: str) -> Path:
    return task_dir(autonomy_home, task_id) / "journal.md"


def report_path(autonomy_home: Path, task_id: str) -> Path:
    return task_dir(autonomy_home, task_id) / "report.md"


def load_state(path: Path) -> TaskState:
    return TaskState.from_json(json.loads(path.read_text(encoding="utf-8")))


def save_state(path: Path, state: TaskState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_json(), indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


@contextlib.contextmanager
def locked_state(autonomy_home: Path, task_id: str) -> Iterator[TaskState]:
    path = state_path(autonomy_home, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / "state.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = load_state(path)
            yield state
            save_state(path, state)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def task_guard_increment_tool_call(autonomy_home: Path, task_id: str) -> None:
    reason = reserve_tool_call(autonomy_home, task_id, tool_use_id=None, command=None)
    if reason:
        raise RuntimeError(reason)


def reserve_tool_call(
    autonomy_home: Path,
    task_id: str,
    *,
    tool_use_id: str | None,
    command: str | None,
) -> str | None:
    with locked_state(autonomy_home, task_id) as state:
        if state.status != "active":
            return f"modification refusée : statut {state.status}"
        task = load_task(Path(state.task_path))
        time_error = check_time_budget(state, task)
        if time_error:
            state.status = "stopped"
            state.stopped_reason = time_error
            return time_error
        if tool_use_id and tool_use_id in state.seen_tool_ids:
            return None
        stripped = command.strip() if command else None
        paired = bool(
            stripped
            and state.reserved_shell_pending
            and state.reserved_shell_command == stripped
        )
        if paired:
            state.reserved_shell_pending = False
            _remember_tool_use(state, tool_use_id)
            return None
        if state.tool_call_count >= task.max_tool_calls:
            state.status = "stopped"
            state.stopped_reason = f"plafond d'appels d'outils atteint ({task.max_tool_calls})"
            return state.stopped_reason
        state.tool_call_count += 1
        _remember_tool_use(state, tool_use_id)
        if stripped:
            state.reserved_shell_command = stripped
            state.reserved_shell_pending = True
        return None


def _remember_tool_use(state: TaskState, tool_use_id: str | None) -> None:
    if not tool_use_id or tool_use_id in state.seen_tool_ids:
        return
    state.seen_tool_ids.append(tool_use_id)
    del state.seen_tool_ids[:-32]


def append_journal(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_now_iso()} {line}\n")


def trust_script_hashes(trust_root: Path) -> dict[str, str]:
    return {rel: sha256_file(trust_root / rel) for rel in TRUST_SCRIPTS}


def path_matches_glob(relative: str, pattern: str) -> bool:
    rel = PurePosixPath(relative)
    if rel.match(pattern):
        return True
    return fnmatch.fnmatch(rel.as_posix(), pattern)


def matches_any(relative: str, patterns: list[str]) -> bool:
    return any(path_matches_glob(relative, pattern) for pattern in patterns)


def run_bounded(
    argv: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    armed = _deadline.get()
    if armed is not None:
        chosen = remaining_timeout(armed[0], armed[1])
        if timeout is not None:
            chosen = min(float(timeout), chosen)
    else:
        chosen = float(SUBPROCESS_TIMEOUT_SECONDS if timeout is None else timeout)
    if chosen <= 0:
        raise RuntimeError(f"échéance dépassée : {' '.join(argv)}")
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=text,
            env=env,
            check=False,
            timeout=chosen,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"timeout : {' '.join(argv)}") from exc


def git_in(repo: Path, *args: str) -> str:
    proc = run_bounded(["git", *args], repo)
    if proc.returncode != 0:
        detail = str(proc.stderr).strip() or str(proc.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} : {detail}")
    return str(proc.stdout).strip()


def is_ancestor(candidate: str, descendant: str, repo: Path) -> bool:
    proc = run_bounded(["git", "merge-base", "--is-ancestor", candidate, descendant], repo)
    return proc.returncode == 0


def resolve_under_root(root: Path, candidate: str) -> Path | None:
    root_resolved = root.resolve()
    try:
        target = (root / candidate).resolve()
    except OSError:
        return None
    try:
        target.relative_to(root_resolved)
    except ValueError:
        return None
    if (root / candidate).is_symlink():
        try:
            target.relative_to(root_resolved)
        except ValueError:
            return None
    return target


def path_is_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _split_null(payload: str) -> list[str]:
    return [item for item in payload.split("\0") if item]


def untracked_paths(worktree: Path) -> list[str]:
    proc = run_bounded(["git", "ls-files", "-z", "--others", "--exclude-standard"], worktree)
    if proc.returncode != 0:
        raise RuntimeError(str(proc.stderr).strip() or "ls-files a échoué")
    return _split_null(str(proc.stdout))


def list_worktree_changes(worktree: Path, base_sha: str) -> list[tuple[str, str]]:
    proc = run_bounded(["git", "diff", "--name-status", "-z", "-M", base_sha], worktree)
    if proc.returncode != 0:
        raise RuntimeError(str(proc.stderr).strip() or "diff --name-status a échoué")
    tokens = str(proc.stdout).split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    changes: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            continue
        if status[0] in {"R", "C"}:
            if index + 1 >= len(tokens):
                break
            source = tokens[index]
            destination = tokens[index + 1]
            index += 2
            changes.append((status, source))
            changes.append((status, destination))
            continue
        if index >= len(tokens):
            break
        changes.append((status, tokens[index]))
        index += 1
    for path in untracked_paths(worktree):
        changes.append(("?", path))
    return changes


def count_changed_lines(worktree: Path, base_sha: str) -> tuple[int, str | None]:
    proc = run_bounded(["git", "diff", "--numstat", base_sha], worktree)
    if proc.returncode != 0:
        raise RuntimeError(str(proc.stderr).strip() or "diff --numstat a échoué")
    total = 0
    binary_reason: str | None = None
    for line in str(proc.stdout).splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        if parts[0] == "-" or parts[1] == "-":
            binary_reason = "diff binaire non comptable"
            continue
        if parts[0].isdigit() and parts[1].isdigit():
            total += int(parts[0]) + int(parts[1])
    for relative in untracked_paths(worktree):
        full = worktree / relative
        if full.is_symlink() or not full.is_file():
            continue
        data = full.read_bytes()
        if b"\0" in data:
            binary_reason = "fichier binaire non suivi"
            continue
        total += data.count(b"\n")
    return total, binary_reason


def resolve_tool(trust_root: Path, name: str) -> list[str]:
    local = trust_root / ".venv" / "bin" / name
    if local.is_file():
        return [str(local)]
    raise RuntimeError(f"exécutable non approuvé : {name}")


def ci_env(trust_root: Path) -> dict[str, str]:
    return {
        "PATH": str((trust_root / ".venv" / "bin").resolve()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": tempfile.gettempdir(),
        **CI_TOKENS,
    }


def blocked_read_reason(worktree: Path | None, path_str: str) -> str | None:
    normalized = path_str.replace("\\", "/")
    if READ_FORBIDDEN_RE.search(normalized):
        return "lecture de secrets ou de métadonnées git interdite"
    if worktree is None:
        return None
    if Path(path_str).is_absolute():
        if not path_is_under(worktree, Path(path_str)):
            return "lecture hors worktree"
        relative = str(Path(path_str).resolve().relative_to(worktree.resolve()))
    else:
        resolved = resolve_under_root(worktree, path_str)
        if resolved is None:
            return "lecture hors worktree"
        relative = str(resolved.relative_to(worktree.resolve()))
    if ".git" in PurePosixPath(relative).parts or READ_FORBIDDEN_RE.search(relative):
        return "lecture de secrets ou de métadonnées git interdite"
    return None


def remaining_timeout(state: TaskState, task: TaskSpec) -> float:
    started = datetime.fromisoformat(state.started_at)
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    remain = task.max_minutes * 60 - (utc_now() - started).total_seconds()
    if remain < 0:
        remain = 0
    return min(float(SUBPROCESS_TIMEOUT_SECONDS), remain)


@contextlib.contextmanager
def arm_call_timeout(state: TaskState, task: TaskSpec) -> Iterator[None]:
    token = _deadline.set((state, task))
    try:
        yield
    finally:
        _deadline.reset(token)


def read_tracked_file_at(worktree: Path, base_sha: str, relative: str) -> str | None:
    proc = run_bounded(["git", "show", f"{base_sha}:{relative}"], worktree)
    if proc.returncode != 0:
        return None
    return str(proc.stdout)


def preexisting_test_path(worktree: Path, base_sha: str, relative: str) -> bool:
    if not relative.startswith("tests/") or not relative.endswith(".py"):
        return False
    return read_tracked_file_at(worktree, base_sha, relative) is not None


def detect_test_weakening(worktree: Path, base_sha: str, relative: str) -> str | None:
    if not relative.startswith("tests/") or not relative.endswith(".py"):
        return None
    if preexisting_test_path(worktree, base_sha, relative):
        return None
    before = read_tracked_file_at(worktree, base_sha, relative)
    after_path = worktree / relative
    if before is None or not after_path.is_file():
        return None
    after = after_path.read_text(encoding="utf-8")
    for pattern in (r"^\s*def test_", r"^\s*assert\b"):
        before_count = len(re.findall(pattern, before, re.MULTILINE))
        after_count = len(re.findall(pattern, after, re.MULTILINE))
        if after_count < before_count:
            return f"{relative} : suppression de tests ou d'assertions"
    forbidden_additions = ("@pytest.mark.skip", "@pytest.mark.xfail", "# type: ignore", "# noqa")
    for token in forbidden_additions:
        if token in after and token not in before:
            return f"{relative} : ajout de {token.strip()}"
    return None


def detect_secrets_in_diff(worktree: Path, base_sha: str) -> str | None:
    proc = run_bounded(["git", "diff", base_sha], worktree)
    if proc.returncode != 0:
        raise RuntimeError(str(proc.stderr).strip() or "diff a échoué")
    chunks = [str(proc.stdout)]
    for relative in untracked_paths(worktree):
        chunks.append(relative)
        full = worktree / relative
        if full.is_symlink() or not full.is_file():
            continue
        chunks.append(full.read_text(encoding="utf-8", errors="replace"))
    blob = "\n".join(chunks)
    for pattern in SECRET_PATTERNS:
        if pattern.search(blob):
            return "motif de secret détecté dans le diff"
    return None


def check_scope(task: TaskSpec, worktree: Path, base_sha: str) -> list[str]:
    errors: list[str] = []
    root = worktree.resolve()
    for status, relative in list_worktree_changes(worktree, base_sha):
        full = worktree / relative
        if status == "?" and full.is_symlink():
            try:
                full.resolve().relative_to(root)
            except (OSError, ValueError):
                errors.append(f"lien symbolique hors worktree : {relative}")
        if matches_any(relative, task.all_forbidden()):
            errors.append(f"hors périmètre (interdit) : {status} {relative}")
        elif not matches_any(relative, task.allowed_write):
            errors.append(f"hors périmètre (non autorisé) : {status} {relative}")
        if preexisting_test_path(worktree, base_sha, relative):
            errors.append(f"revue humaine : test existant modifié : {relative}")
    return errors


def check_trust_scripts(state: TaskState) -> str | None:
    trust_root = Path(state.trust_root)
    current = trust_script_hashes(trust_root)
    if current != state.trust_script_hashes:
        return "les scripts de confiance ont été modifiés depuis l'approbation"
    return None


def check_time_budget(state: TaskState, task: TaskSpec) -> str | None:
    started = datetime.fromisoformat(state.started_at)
    elapsed = utc_now() - started
    if elapsed.total_seconds() > task.max_minutes * 60:
        return f"plafond de durée dépassé ({task.max_minutes} min)"
    return None


def validate_task_context(autonomy_home: Path, state: TaskState) -> str | None:
    try:
        approval_file = approval_path(autonomy_home, state.task_id)
        if not approval_file.is_file():
            return "approbation manquante"
        approval = ApprovalRecord(**json.loads(approval_file.read_text(encoding="utf-8")))
        task = load_task(Path(state.task_path))
        if not Path(state.worktree_path).is_dir():
            return "worktree introuvable"
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return f"validation impossible : {exc}"
    if approval.task_id != state.task_id:
        return "approbation incohérente"
    if approval.fingerprint != state.fingerprint or approval.base_sha != state.base_sha:
        return "approbation incohérente"
    if approval.trust_root != state.trust_root:
        return "approbation incohérente"
    if approval.trust_script_hashes != state.trust_script_hashes:
        return "approbation incohérente"
    if task.fingerprint(Path(state.task_path)) != state.fingerprint:
        return "fiche modifiée depuis l'approbation"
    if state.status not in {"active", "stopped", "ready_for_review", "failed"}:
        return "statut invalide"
    current_hashes = trust_script_hashes(Path(state.trust_root))
    if state.trust_script_hashes and current_hashes != state.trust_script_hashes:
        return "scripts de confiance modifiés"
    return None


ProofRow = tuple[str, int, str, float]


def _normalize_proofs(results: list[tuple[Any, ...]]) -> list[ProofRow]:
    rows: list[ProofRow] = []
    for item in results:
        duration = float(item[3]) if len(item) > 3 else 0.0
        rows.append((str(item[0]), int(item[1]), str(item[2]), duration))
    return rows


def run_proof_commands(task: TaskSpec, worktree: Path, trust_root: Path) -> list[ProofRow]:
    results: list[ProofRow] = []
    for command in task.proof:
        argv = split_command(command)
        if argv is None or not proof_command_allowed(command):
            raise RuntimeError(f"commande inconnue : {command}")
        executable = resolve_tool(trust_root, argv[0])
        started = time.monotonic()
        proc = run_bounded([*executable, *argv[1:]], worktree, env=ci_env(trust_root))
        duration = time.monotonic() - started
        tail = redact_text((str(proc.stdout) + str(proc.stderr)).strip()[-2000:])
        results.append((command, int(proc.returncode), tail, duration))
    return results


def compute_review_digest(worktree: Path, base_sha: str) -> str:
    head = git_in(worktree, "rev-parse", "HEAD")
    diff = run_bounded(["git", "diff", "--binary", base_sha], worktree, text=False)
    if diff.returncode != 0:
        raise RuntimeError(str(diff.stderr).strip() or "diff binaire a échoué")
    hasher = hashlib.sha256()
    hasher.update(head.encode())
    stdout = diff.stdout if isinstance(diff.stdout, bytes) else b""
    hasher.update(stdout)
    for relative in sorted(untracked_paths(worktree)):
        hasher.update(relative.encode())
        full = worktree / relative
        if full.is_symlink():
            hasher.update(os.readlink(full).encode())
        elif full.is_file():
            hasher.update(full.read_bytes())
    return hasher.hexdigest()


def _record_evidence(
    state: TaskState,
    worktree: Path,
    base_sha: str,
    static_errors: list[str],
    proofs: list[ProofRow],
) -> None:
    try:
        head = git_in(worktree, "rev-parse", "HEAD")
    except RuntimeError:
        head = ""
    try:
        digest = compute_review_digest(worktree, base_sha)
    except RuntimeError:
        digest = ""
    state.evidence.append(
        {
            "at": utc_now_iso(),
            "head_sha": head,
            "review_digest": digest,
            "static_errors": list(static_errors),
            "proofs": [
                {
                    "argv": command,
                    "returncode": code,
                    "duration_s": duration,
                    "tail": tail,
                }
                for command, code, tail, duration in proofs
            ],
        }
    )


def verify_task(
    state: TaskState,
    task: TaskSpec,
    _autonomy_home: Path,
) -> tuple[bool, list[str]]:
    with arm_call_timeout(state, task):
        return _verify_task_bounded(state, task)


def _verify_task_bounded(state: TaskState, task: TaskSpec) -> tuple[bool, list[str]]:
    errors: list[str] = []
    worktree = Path(state.worktree_path)
    if not worktree.is_dir():
        return False, ["worktree introuvable"]
    if state.fingerprint != task.fingerprint(Path(state.task_path)):
        return False, ["empreinte de la fiche modifiée"]
    if state.base_sha != task.base_sha:
        return False, ["base_sha de la fiche modifiée"]
    try:
        if not is_ancestor(state.base_sha, "HEAD", worktree):
            return False, ["HEAD du worktree n'est pas un descendant du base_sha"]
        trust_error = check_trust_scripts(state)
        if trust_error:
            errors.append(trust_error)
        time_error = check_time_budget(state, task)
        if time_error:
            errors.append(time_error)
        if state.tool_call_count > task.max_tool_calls:
            errors.append(f"plafond d'appels d'outils dépassé ({task.max_tool_calls})")
        errors.extend(check_scope(task, worktree, state.base_sha))
        changed_lines, binary_reason = count_changed_lines(worktree, state.base_sha)
        if binary_reason:
            errors.append(binary_reason)
        elif changed_lines > task.max_diff_lines:
            errors.append(f"diff trop grand ({changed_lines} > {task.max_diff_lines} lignes)")
        secret_error = detect_secrets_in_diff(worktree, state.base_sha)
        if secret_error:
            errors.append(secret_error)
        for status, relative in list_worktree_changes(worktree, state.base_sha):
            if status.startswith(("M", "R", "C", "A")) or status == "?":
                weaken = detect_test_weakening(worktree, state.base_sha, relative)
                if weaken:
                    errors.append(weaken)
    except RuntimeError as exc:
        return False, [str(exc)]

    if errors:
        _record_evidence(state, worktree, state.base_sha, errors, [])
        return False, errors

    try:
        collected = collect_test_count(worktree, Path(state.trust_root))
    except RuntimeError as exc:
        errors.append(str(exc))
        _record_evidence(state, worktree, state.base_sha, errors, [])
        return False, errors
    if collected < state.baseline_test_count:
        errors.append(f"tests collectés en baisse ({collected} < {state.baseline_test_count})")
        _record_evidence(state, worktree, state.base_sha, errors, [])
        return False, errors

    try:
        proof_results = _normalize_proofs(
            run_proof_commands(task, worktree, Path(state.trust_root))
        )
    except RuntimeError as exc:
        errors.append(str(exc))
        _record_evidence(state, worktree, state.base_sha, errors, [])
        return False, errors
    for command, code, tail, _duration in proof_results:
        if code != 0:
            errors.append(f"preuve échouée : {command}\n{tail}")
    _record_evidence(state, worktree, state.base_sha, errors, proof_results)
    return not errors, errors


def collect_test_count(worktree: Path, trust_root: Path | None = None) -> int:
    root = trust_root or worktree
    python = resolve_tool(root, "python")
    proc = run_bounded(
        [*python, "-m", "pytest", "--collect-only", "-q"],
        worktree,
        env=ci_env(root),
    )
    if proc.returncode != 0:
        raise RuntimeError(str(proc.stderr).strip() or str(proc.stdout).strip())
    match = re.search(r"(\d+)\s+tests?\s+collected", str(proc.stdout))
    if not match:
        raise RuntimeError("impossible de lire le nombre de tests collectés")
    return int(match.group(1))


def load_active_task(autonomy_home: Path) -> tuple[str, TaskState] | None:
    pointer = active_pointer_path(autonomy_home)
    if not pointer.is_file():
        return None
    data = json.loads(pointer.read_text(encoding="utf-8"))
    task_id = str(data["task_id"])
    state = load_state(state_path(autonomy_home, task_id))
    return task_id, state


def ensure_consistent_active(autonomy_home: Path, state: TaskState) -> str | None:
    active = load_active_task(autonomy_home)
    if active is None:
        return "aucune tâche active"
    active_id, active_state = active
    if active_id != state.task_id:
        return "pointeur active.json incohérent"
    if active_state.fingerprint != state.fingerprint:
        return "empreinte incohérente avec active.json"
    return None


def cmd_approve(args: argparse.Namespace) -> int:
    task_path = Path(args.task_file).resolve()
    task = load_task(task_path)
    autonomy_home = default_autonomy_home()
    trust_root = Path(args.trust_root).resolve() if args.trust_root else REPO_ROOT
    approval = ApprovalRecord(
        task_id=task.id,
        fingerprint=task.fingerprint(task_path),
        base_sha=git_in(trust_root, "rev-parse", task.base_sha),
        task_path=str(task_path),
        trust_root=str(trust_root),
        approved_at=utc_now_iso(),
        trust_script_hashes=trust_script_hashes(trust_root),
    )
    dest = approval_path(autonomy_home, task.id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    approval_payload = json.dumps(approval.to_json(), indent=2, ensure_ascii=False) + "\n"
    dest.write_text(approval_payload, encoding="utf-8")
    print(f"approbation enregistrée : {dest}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    task_path = Path(args.task_file).resolve()
    task = load_task(task_path)
    autonomy_home = default_autonomy_home()
    approval_file = approval_path(autonomy_home, task.id)
    if not approval_file.is_file():
        print("approbation manquante : lancez approve avant start", file=sys.stderr)
        return 2
    approval = ApprovalRecord(**json.loads(approval_file.read_text(encoding="utf-8")))
    fingerprint = task.fingerprint(task_path)
    if approval.fingerprint != fingerprint:
        print("empreinte de la fiche différente de l'approbation", file=sys.stderr)
        return 2
    trust_root = Path(approval.trust_root)
    worktree = default_tasks_root() / task.id
    if worktree.exists():
        print(f"worktree déjà présent : {worktree}", file=sys.stderr)
        return 2
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git_in(trust_root, "worktree", "add", "--detach", str(worktree), approval.base_sha)
    baseline = collect_test_count(worktree, trust_root)
    state = TaskState(
        task_id=task.id,
        fingerprint=fingerprint,
        base_sha=approval.base_sha,
        trust_root=str(trust_root),
        worktree_path=str(worktree),
        task_path=str(task_path),
        status="active",
        started_at=utc_now_iso(),
        approved_at=approval.approved_at,
        trust_script_hashes=approval.trust_script_hashes,
        baseline_test_count=baseline,
        consumption_before=args.consumption_before,
    )
    save_state(state_path(autonomy_home, task.id), state)
    active_pointer_path(autonomy_home).write_text(
        json.dumps({"task_id": task.id}, indent=2) + "\n",
        encoding="utf-8",
    )
    append_journal(journal_path(autonomy_home, task.id), "start worktree créé")
    print(state.worktree_path)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    autonomy_home = default_autonomy_home()
    with locked_state(autonomy_home, args.task_id) as state:
        task = load_task(Path(state.task_path))
        if state.fingerprint != task.fingerprint(Path(state.task_path)):
            print("fiche modifiée depuis le démarrage", file=sys.stderr)
            return 2
        if state.status == "stopped":
            state.status = "active"
            state.stopped_reason = None
        elif state.status == "ready_for_review":
            state.status = "active"
            state.review_digest = None
            append_journal(
                journal_path(autonomy_home, args.task_id),
                "invalidation explicite par resume",
            )
        active_pointer_path(autonomy_home).write_text(
            json.dumps({"task_id": state.task_id}, indent=2) + "\n",
            encoding="utf-8",
        )
        append_journal(journal_path(autonomy_home, args.task_id), "resume")
    print("reprise sans remise à zéro des compteurs")
    return 0


def _control_violation(errors: list[str]) -> str | None:
    for err in errors:
        if any(marker in err for marker in CONTROL_MARKERS):
            return err
    return None


def _apply_verify_result(
    fresh: TaskState,
    *,
    ok: bool,
    errors: list[str],
    repeat: int,
    signature: str | None,
    evidence: list[dict[str, Any]],
    digest_after: str | None,
    preserve_stop: bool,
) -> None:
    fresh.evidence = evidence
    fresh.repeat_error_count = repeat
    fresh.last_error_signature = signature
    if preserve_stop:
        fresh.review_digest = None
        return
    violation = _control_violation(errors)
    if violation:
        fresh.status = "stopped"
        fresh.stopped_reason = violation
        fresh.review_digest = None
        return
    if repeat >= 2 and errors:
        fresh.status = "stopped"
        fresh.stopped_reason = "même erreur deux fois de suite"
        fresh.review_digest = None
        return
    if ok and digest_after is not None:
        fresh.status = "ready_for_review"
        fresh.review_digest = digest_after
        fresh.stopped_reason = None
        return
    fresh.status = "active"
    fresh.review_digest = None


def cmd_verify(args: argparse.Namespace) -> int:
    autonomy_home = default_autonomy_home()
    with locked_state(autonomy_home, args.task_id) as state:
        task = load_task(Path(state.task_path))
        if state.verify_count >= task.max_cycles:
            state.status = "stopped"
            state.stopped_reason = f"plafond de cycles verify atteint ({task.max_cycles})"
            print(state.stopped_reason, file=sys.stderr)
            return 2
        if state.status in {"stopped", "failed"}:
            print(f"tâche arrêtée : {state.stopped_reason}", file=sys.stderr)
            return 2
        inconsistency = ensure_consistent_active(autonomy_home, state)
        if inconsistency:
            state.status = "stopped"
            state.stopped_reason = inconsistency
            print(inconsistency, file=sys.stderr)
            return 2
        if state.status == "ready_for_review":
            current = compute_review_digest(Path(state.worktree_path), state.base_sha)
            if current != state.review_digest:
                state.status = "active"
                state.review_digest = None
                append_journal(
                    journal_path(autonomy_home, args.task_id),
                    "invalidation du digest de revue",
                )
        state.verify_count += 1
        previous_signature = state.last_error_signature
        previous_repeat = state.repeat_error_count
        digest_before = compute_review_digest(Path(state.worktree_path), state.base_sha)
        working = state

    ok, errors = verify_task(working, task, autonomy_home)
    digest_after = compute_review_digest(Path(working.worktree_path), working.base_sha)
    if digest_before != digest_after:
        ok = False
        errors = [*errors, "contenu modifié pendant les preuves"]
    signature = errors[0][:200] if errors else None
    if previous_signature == signature and errors and signature is not None:
        repeat = previous_repeat + 1
    else:
        repeat = 1 if errors else 0
    with locked_state(autonomy_home, args.task_id) as fresh:
        _apply_verify_result(
            fresh,
            ok=ok,
            errors=errors,
            repeat=repeat,
            signature=signature,
            evidence=list(working.evidence),
            digest_after=digest_after if digest_before == digest_after else None,
            preserve_stop=fresh.status in {"stopped", "failed"},
        )
        if fresh.status == "ready_for_review":
            append_journal(journal_path(autonomy_home, args.task_id), "verify vert")
        elif errors:
            append_journal(journal_path(autonomy_home, args.task_id), f"verify rouge : {errors[0]}")
        published = fresh.status == "ready_for_review"
    if published:
        print("verify : vert")
        return 0
    for err in errors:
        print(err, file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    autonomy_home = default_autonomy_home()
    state = load_state(state_path(autonomy_home, args.task_id))
    worktree = Path(state.worktree_path)
    print(json.dumps(state.to_json(), indent=2, ensure_ascii=False))
    if worktree.is_dir():
        print("--- git status ---")
        print(git_in(worktree, "status", "--short"))
        print("--- changes vs base ---")
        for status, relative in list_worktree_changes(worktree, state.base_sha):
            print(f"{status}\t{relative}")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    autonomy_home = default_autonomy_home()
    append_journal(journal_path(autonomy_home, args.task_id), f"note : {args.message}")
    return 0


def _report_limits() -> list[str]:
    return [
        "## Limites",
        "- Pas de sandbox du code exécuté par pytest.",
        "- Aucune garantie réseau ajoutée : seuls les jetons Telegram de test sont imposés.",
        "- Les hooks Cursor n'ont pas été essayés dans l'IDE.",
        "- La CI distante n'est pas exécutée par ce rapport.",
        "- failClosed ne couvre qu'un crash, un timeout ou une sortie invalide du hook.",
        "- failClosed ne compense pas une décision allow incorrecte.",
        "",
    ]


def cmd_report(args: argparse.Namespace) -> int:
    autonomy_home = default_autonomy_home()
    with locked_state(autonomy_home, args.task_id) as state:
        task = load_task(Path(state.task_path))
        worktree = Path(state.worktree_path)
        current_digest = None
        if worktree.is_dir():
            current_digest = compute_review_digest(worktree, state.base_sha)
        digest_changed = current_digest != state.review_digest
        if state.status == "ready_for_review" and digest_changed:
            state.status = "active"
            state.review_digest = None
            append_journal(
                journal_path(autonomy_home, args.task_id),
                "invalidation du digest de revue",
            )
        stale = not state.evidence or digest_changed
        lines = [
            f"# Rapport autonomie — {task.id}",
            "",
            f"- Statut : {state.status}",
            f"- Début : {state.started_at}",
            f"- Cycles verify : {state.verify_count} / {task.max_cycles}",
            f"- Appels d'outils : {state.tool_call_count} / {task.max_tool_calls}",
            f"- Digest de revue : {state.review_digest or 'absent'}",
            f"- Consommation avant : {state.consumption_before or 'non mesurée'}",
            (
                "- Consommation après : "
                f"{args.consumption_after or state.consumption_after or 'non mesurée'}"
            ),
            "",
            "## Objectif",
            task.objective,
            "",
        ]
        if state.stopped_reason:
            lines.extend(["## Arrêt", state.stopped_reason, ""])
        if stale:
            lines.extend(
                [
                    "## Preuves",
                    "preuve périmée : aucune preuve enregistrée ne correspond à l'arbre actuel.",
                    "",
                ]
            )
        else:
            lines.append("## Preuves enregistrées")
            for entry in state.evidence:
                lines.append(
                    f"- {entry.get('at')} HEAD `{entry.get('head_sha')}` "
                    f"digest `{entry.get('review_digest')}`"
                )
                for proof in entry.get("proofs", []):
                    lines.append(
                        f"  - `{proof.get('argv')}` → code {proof.get('returncode')} "
                        f"({proof.get('duration_s')} s)"
                    )
                    tail = str(proof.get("tail") or "").strip()
                    if tail:
                        lines.append(f"    {tail.splitlines()[-1][:300]}")
            lines.append("")
        if worktree.is_dir():
            lines.append("## Fichiers touchés")
            for status, relative in list_worktree_changes(worktree, state.base_sha):
                lines.append(f"- `{status}` {relative}")
            lines.extend(["", "```", git_in(worktree, "diff", "--stat", state.base_sha), "```", ""])
        lines.append("## Revue humaine attendue")
        for item in task.human_review:
            lines.append(f"- {item}")
        lines.append("")
        lines.extend(_report_limits())
        dest = report_path(autonomy_home, args.task_id)
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if args.consumption_after:
            state.consumption_after = args.consumption_after
    print(dest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Garde-fou d'autonomie BotRadar")
    sub = parser.add_subparsers(dest="command", required=True)

    approve = sub.add_parser("approve", help="Approbation humaine de la fiche")
    approve.add_argument("task_file", type=Path)
    approve.add_argument("--trust-root", type=Path, default=None)

    start = sub.add_parser("start", help="Créer le worktree et l'état")
    start.add_argument("task_file", type=Path)
    start.add_argument("--consumption-before", default=None)

    resume = sub.add_parser("resume", help="Reprendre sans remettre les compteurs à zéro")
    resume.add_argument("task_id")

    verify = sub.add_parser("verify", help="Vérifier périmètre et preuves")
    verify.add_argument("task_id")

    status = sub.add_parser("status", help="Afficher l'état et le diff")
    status.add_argument("task_id")

    note = sub.add_parser("note", help="Ajouter une ligne au journal")
    note.add_argument("task_id")
    note.add_argument("message")

    report = sub.add_parser("report", help="Écrire le rapport final")
    report.add_argument("task_id")
    report.add_argument("--consumption-after", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "approve": cmd_approve,
        "start": cmd_start,
        "resume": cmd_resume,
        "verify": cmd_verify,
        "status": cmd_status,
        "note": cmd_note,
        "report": cmd_report,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

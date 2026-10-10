#!/usr/bin/env python
"""Garde-fou d'autonomie : fiche, worktree, vérification et rapport."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]

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

SHELL_FORBIDDEN_RE = re.compile(
    r"(;|&&|\|\||\||`|\$\(|>\s*>|<\(|>\s*/dev/|^\s*sudo\b|^\s*env\b|^\s*sh\b|^\s*bash\b|"
    r"^\s*python\s+-c\b|^\s*python3\s+-c\b)",
    re.MULTILINE,
)

PROOF_ALLOWED_RE = re.compile(
    r"^(ruff check \.|ruff check [^;&|`$]+|mypy app|pytest [^;&|`$]+)$"
)

AGENT_BLOCKED_SUBCOMMANDS = frozenset({"approve", "start", "resume", "reset-counters"})

Status = Literal["active", "stopped", "ready_for_review", "failed"]


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


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TaskState:
        return cls(**data)


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


def load_task(path: Path) -> TaskSpec:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    required = ("id", "base_sha", "objective", "allowed_write", "proof")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"champs manquants dans la fiche : {', '.join(missing)}")
    if not raw["allowed_write"]:
        raise ValueError("allowed_write ne peut pas être vide")
    if not raw["proof"]:
        raise ValueError("proof ne peut pas être vide")
    for command in raw["proof"]:
        if not proof_command_allowed(str(command).strip()):
            raise ValueError(f"commande de preuve non autorisée : {command}")
    return TaskSpec(
        id=str(raw["id"]),
        base_sha=str(raw["base_sha"]),
        objective=str(raw["objective"]),
        allowed_write=[str(p) for p in raw["allowed_write"]],
        proof=[str(p).strip() for p in raw["proof"]],
        non_goals=[str(x) for x in raw.get("non_goals", [])],
        forbidden=[str(x) for x in raw.get("forbidden", [])],
        max_minutes=int(raw.get("max_minutes", DEFAULT_MAX_MINUTES)),
        max_cycles=int(raw.get("max_cycles", DEFAULT_MAX_CYCLES)),
        max_tool_calls=int(raw.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)),
        max_diff_lines=int(raw.get("max_diff_lines", DEFAULT_MAX_DIFF_LINES)),
        escalate_on=str(raw.get("escalate_on", "")),
        human_review=[str(x) for x in raw.get("human_review", [])],
        allow_subagents=bool(raw.get("allow_subagents", False)),
    )


def proof_command_allowed(command: str) -> bool:
    return bool(PROOF_ALLOWED_RE.match(command))


ALLOWED_SHELL_PREFIXES = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "rg ",
    "ls ",
    "pwd",
)


def shell_command_allowed(command: str, trust_root: Path) -> bool:
    stripped = command.strip()
    if not stripped or SHELL_FORBIDDEN_RE.search(stripped):
        return False
    lowered = stripped.lower()
    if any(lowered.startswith(prefix) for prefix in ALLOWED_SHELL_PREFIXES):
        return True
    if re.search(r"task_verdict\.py\s+(verify|status|note|report)\b", stripped):
        return True
    if re.search(r"task_guard\.py\b", stripped):
        return True
    venv = trust_root / ".venv" / "bin"
    for tool in ("pytest", "ruff", "mypy"):
        prefix = str(venv / tool)
        if stripped.startswith(prefix):
            remainder = stripped[len(prefix) :].strip()
            if tool == "pytest":
                return bool(remainder) and proof_command_allowed(f"pytest {remainder}")
            if tool == "ruff" and remainder.startswith("check"):
                return True
            if tool == "mypy" and remainder == "app":
                return True
    return False


def task_guard_increment_tool_call(autonomy_home: Path, task_id: str) -> None:
    path = state_path(autonomy_home, task_id)
    if not path.is_file():
        return
    state = load_state(path)
    state.tool_call_count += 1
    save_state(path, state)


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
    path.write_text(payload, encoding="utf-8")


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


def git_in(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} : {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def is_ancestor(candidate: str, descendant: str, repo: Path) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate, descendant],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )
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
    if target.is_symlink():
        link_target = target.resolve()
        try:
            link_target.relative_to(root_resolved)
        except ValueError:
            return None
    return target


def list_worktree_changes(worktree: Path, base_sha: str) -> list[tuple[str, str]]:
    lines = git_in(worktree, "diff", "--name-status", base_sha).splitlines()
    changes: list[tuple[str, str]] = []
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") or status.startswith("C"):
            if len(parts) >= 3:
                changes.append((status, parts[2]))
            continue
        changes.append((status, parts[-1]))
    untracked = git_in(worktree, "ls-files", "--others", "--exclude-standard").splitlines()
    for path in untracked:
        if path:
            changes.append(("?", path))
    return changes


def count_changed_lines(worktree: Path, base_sha: str) -> int:
    proc = subprocess.run(
        ["git", "diff", "--numstat", base_sha],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    total = 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            total += int(parts[0]) + int(parts[1])
    return total


def collect_test_count(worktree: Path) -> int:
    python = resolve_python(worktree)
    proc = subprocess.run(
        [str(python), "-m", "pytest", "--collect-only", "-q"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        env=ci_env(),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    match = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout)
    if not match:
        raise RuntimeError("impossible de lire le nombre de tests collectés")
    return int(match.group(1))


def resolve_python(repo: Path) -> Path:
    local = repo / ".venv" / "bin" / "python"
    if local.exists():
        return local
    trust = REPO_ROOT / ".venv" / "bin" / "python"
    if trust.exists():
        return trust
    found = shutil.which("python3")
    if not found:
        raise RuntimeError("interpréteur Python introuvable")
    return Path(found)


def resolve_tool(trust_root: Path, name: str) -> list[str]:
    local = trust_root / ".venv" / "bin" / name
    if local.exists():
        return [str(local)]
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"outil introuvable : {name}")
    return [found]


def ci_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(CI_TOKENS)
    return env


def read_tracked_file_at(worktree: Path, base_sha: str, relative: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{base_sha}:{relative}"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def detect_test_weakening(worktree: Path, base_sha: str, relative: str) -> str | None:
    if not relative.startswith("tests/") or not relative.endswith(".py"):
        return None
    before = read_tracked_file_at(worktree, base_sha, relative)
    after_path = worktree / relative
    if before is None and not after_path.exists():
        return None
    after = after_path.read_text(encoding="utf-8") if after_path.exists() else ""
    if before is None:
        return None
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
    proc = subprocess.run(
        ["git", "diff", base_sha],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    for pattern in SECRET_PATTERNS:
        if pattern.search(proc.stdout):
            return "motif de secret détecté dans le diff"
    return None


def check_scope(task: TaskSpec, worktree: Path, base_sha: str) -> list[str]:
    errors: list[str] = []
    for status, relative in list_worktree_changes(worktree, base_sha):
        if matches_any(relative, task.all_forbidden()):
            errors.append(f"hors périmètre (interdit) : {status} {relative}")
            continue
        if status in {"D"}:
            if relative.startswith("tests/"):
                errors.append(f"suppression interdite dans tests/ : {relative}")
            continue
        if not matches_any(relative, task.allowed_write):
            errors.append(f"hors périmètre (non autorisé) : {status} {relative}")
    return errors


def check_trust_scripts(state: TaskState) -> str | None:
    trust_root = Path(state.trust_root)
    current = trust_script_hashes(trust_root)
    if current != state.trust_script_hashes:
        return "les scripts de confiance ont été modifiés depuis l'approbation"
    return None


def check_time_budget(state: TaskState, task: TaskSpec) -> str | None:
    started = datetime.fromisoformat(state.started_at)
    elapsed = datetime.now(UTC) - started
    if elapsed.total_seconds() > task.max_minutes * 60:
        return f"plafond de durée dépassé ({task.max_minutes} min)"
    return None


ProofResult = tuple[str, int, str]


def run_proof_commands(
    task: TaskSpec,
    worktree: Path,
    trust_root: Path,
) -> list[ProofResult]:
    results: list[ProofResult] = []
    for command in task.proof:
        parts = command.split()
        executable = parts[0]
        if executable == "pytest":
            cmd = [str(resolve_python(trust_root)), "-m", "pytest", *parts[1:]]
        elif executable in {"ruff", "mypy"}:
            cmd = [*resolve_tool(trust_root, executable), *parts[1:]]
        else:
            raise RuntimeError(f"commande inconnue : {command}")
        proc = subprocess.run(
            cmd,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            env=ci_env(),
            check=False,
        )
        tail = (proc.stdout + proc.stderr).strip()[-2000:]
        results.append((command, proc.returncode, tail))
    return results


def verify_task(
    state: TaskState,
    task: TaskSpec,
    _autonomy_home: Path,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    worktree = Path(state.worktree_path)
    if not worktree.is_dir():
        return False, ["worktree introuvable"]
    if state.fingerprint != task.fingerprint(Path(state.task_path)):
        return False, ["empreinte de la fiche modifiée"]
    if state.base_sha != task.base_sha:
        return False, ["base_sha de la fiche modifiée"]
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
    if state.verify_count >= task.max_cycles:
        errors.append(f"plafond de cycles verify déjà atteint ({task.max_cycles})")

    errors.extend(check_scope(task, worktree, state.base_sha))

    changed_lines = count_changed_lines(worktree, state.base_sha)
    if changed_lines > task.max_diff_lines:
        errors.append(f"diff trop grand ({changed_lines} > {task.max_diff_lines} lignes)")

    secret_error = detect_secrets_in_diff(worktree, state.base_sha)
    if secret_error:
        errors.append(secret_error)

    for status, relative in list_worktree_changes(worktree, state.base_sha):
        if status in {"M", "R", "C"} or (status == "?" and relative.startswith("tests/")):
            weaken = detect_test_weakening(worktree, state.base_sha, relative)
            if weaken:
                errors.append(weaken)

    try:
        collected = collect_test_count(worktree)
    except RuntimeError as exc:
        errors.append(str(exc))
        collected = state.baseline_test_count
    if collected < state.baseline_test_count:
        errors.append(
            f"tests collectés en baisse ({collected} < {state.baseline_test_count})"
        )

    if errors:
        return False, errors

    proof_results = run_proof_commands(task, worktree, Path(state.trust_root))
    for command, code, tail in proof_results:
        if code != 0:
            errors.append(f"preuve échouée : {command}\n{tail}")
    return not errors, errors


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
    baseline = collect_test_count(worktree)
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
    state = load_state(state_path(autonomy_home, args.task_id))
    task = load_task(Path(state.task_path))
    if state.fingerprint != task.fingerprint(Path(state.task_path)):
        print("fiche modifiée depuis le démarrage", file=sys.stderr)
        return 2
    if state.status == "stopped":
        state.status = "active"
        state.stopped_reason = None
        save_state(state_path(autonomy_home, args.task_id), state)
        active_pointer_path(autonomy_home).write_text(
            json.dumps({"task_id": state.task_id}, indent=2) + "\n",
            encoding="utf-8",
        )
    append_journal(journal_path(autonomy_home, args.task_id), "resume")
    print("reprise sans remise à zéro des compteurs")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    autonomy_home = default_autonomy_home()
    state = load_state(state_path(autonomy_home, args.task_id))
    task = load_task(Path(state.task_path))
    if state.verify_count >= task.max_cycles:
        state.status = "stopped"
        state.stopped_reason = f"plafond de cycles verify atteint ({task.max_cycles})"
        save_state(state_path(autonomy_home, args.task_id), state)
        print(state.stopped_reason, file=sys.stderr)
        return 2
    if state.status == "stopped":
        print(f"tâche arrêtée : {state.stopped_reason}", file=sys.stderr)
        return 2
    inconsistency = ensure_consistent_active(autonomy_home, state)
    if inconsistency:
        state.status = "stopped"
        state.stopped_reason = inconsistency
        save_state(state_path(autonomy_home, args.task_id), state)
        print(inconsistency, file=sys.stderr)
        return 2
    ok, errors = verify_task(state, task, autonomy_home)
    state.verify_count += 1
    signature = errors[0][:200] if errors else "ok"
    if state.last_error_signature == signature and errors:
        state.repeat_error_count += 1
    else:
        state.repeat_error_count = 1 if errors else 0
        state.last_error_signature = signature if errors else None
    if state.repeat_error_count >= 2 and errors:
        state.status = "stopped"
        state.stopped_reason = "même erreur deux fois de suite"
    elif ok:
        state.status = "ready_for_review"
        append_journal(journal_path(autonomy_home, args.task_id), "verify vert")
    else:
        state.status = "active"
        append_journal(
            journal_path(autonomy_home, args.task_id),
            f"verify rouge : {errors[0]}",
        )
    save_state(state_path(autonomy_home, args.task_id), state)
    if ok:
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


def cmd_report(args: argparse.Namespace) -> int:
    autonomy_home = default_autonomy_home()
    state = load_state(state_path(autonomy_home, args.task_id))
    task = load_task(Path(state.task_path))
    worktree = Path(state.worktree_path)
    lines = [
        f"# Rapport autonomie — {task.id}",
        "",
        f"- Statut : {state.status}",
        f"- Début : {state.started_at}",
        f"- Cycles verify : {state.verify_count} / {task.max_cycles}",
        f"- Appels d'outils : {state.tool_call_count} / {task.max_tool_calls}",
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
    if worktree.is_dir():
        lines.append("## Fichiers touchés")
        for status, relative in list_worktree_changes(worktree, state.base_sha):
            lines.append(f"- `{status}` {relative}")
        lines.append("")
        lines.append("```")
        lines.append(git_in(worktree, "diff", "--stat", state.base_sha))
        lines.append("```")
    lines.append("## Revue humaine attendue")
    for item in task.human_review:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Validations non exécutées par ce rapport")
    lines.append("- CI distante")
    lines.append("- make test complet (sauf preuves de la fiche)")
    lines.append("- Essais réels des hooks Cursor (ssh/rsync/docker)")
    dest = report_path(autonomy_home, args.task_id)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.consumption_after:
        state.consumption_after = args.consumption_after
        save_state(state_path(autonomy_home, args.task_id), state)
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

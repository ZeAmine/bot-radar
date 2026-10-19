"""Politique de chemins, de diff et de classification des tests."""

from __future__ import annotations

import difflib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from app.services.alert_fix import core

_PROTECTED_NAMES = frozenset(
    {
        ".env",
        "AGENTS.md",
        "Dockerfile",
        "Makefile",
        "compose.yml",
        "compose.yaml",
        "pyproject.toml",
        "requirements.lock",
        "tests/conftest.py",
        "scripts/check_alert_chain.py",
        "scripts/verify_consolidation.py",
        "docs/operations/alert-fix-executor.md",
        "docs/operations/alert-chain-check.md",
        "app/services/alert_chain_state.py",
    }
)
_PROTECTED_PREFIXES = (
    ".github/",
    ".git/",
    "alembic/",
    "app/bot/",
    "app/providers/llm/",
    "app/services/alert_fix/",
)
_FORBIDDEN_TEST_MARKERS = (
    "pytest.skip",
    "pytest.xfail",
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "unittest.skip",
    "os._exit",
    "sys.exit",
    "subprocess",
    "socket",
    "httpx",
    "requests",
    "urllib",
    "aiohttp",
    "eval(",
    "exec(",
    "time.sleep",
)
_ERROR_MARKERS = (
    "ERROR",
    "errors during collection",
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",
    "IndentationError",
    "No module named",
    "failed to import test module",
)
_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_DIFF_FORBIDDEN = (
    "GIT binary patch",
    "Binary files",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "new file mode ",
    "similarity index ",
    "dissimilarity index ",
)
_IGNORED_NAMES = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


def normalize_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise core.PolicyError("chemin vide")
    if "\x00" in value or "\\" in value:
        raise core.PolicyError("chemin interdit")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise core.PolicyError(f"chemin non relatif : {value}")
    if any(character in value for character in "*?[]"):
        raise core.PolicyError(f"glob interdit : {value}")
    return path.as_posix()


def is_protected(path: str) -> bool:
    normalized = normalize_path(path)
    if normalized in _PROTECTED_NAMES:
        return True
    if normalized.startswith(".env."):
        return True
    if normalized.startswith("Dockerfile."):
        return True
    return any(normalized.startswith(prefix) for prefix in _PROTECTED_PREFIXES)


def is_test_path(path: str) -> bool:
    normalized = normalize_path(path)
    if not normalized.startswith(("tests/unit/", "tests/integration/")):
        return False
    return Path(normalized).name.startswith("test_") and normalized.endswith(".py")


def is_source_path(path: str) -> bool:
    normalized = normalize_path(path)
    return normalized.startswith(("app/", "docs/"))


def validate_allowed_files(
    ticket: core.Ticket, additions: Sequence[str]
) -> tuple[str, ...]:
    paths = list(ticket.allowed_files)
    for addition in additions:
        paths.append(normalize_path(addition))
    if len(set(paths)) != len(paths):
        raise core.PolicyError("allowed_files contient des doublons")
    if len(paths) > core.MAX_FILES:
        raise core.PolicyError("trop de fichiers autorisés")
    for path in paths:
        if is_protected(path):
            raise core.PolicyError(f"fichier protégé : {path}")
        if path == ticket.test_file:
            if not is_test_path(path):
                raise core.PolicyError("test_file n'est pas un test valide")
            continue
        if path.startswith("tests/"):
            raise core.PolicyError("un seul test de reproduction est autorisé")
        if not is_source_path(path):
            raise core.PolicyError(f"racine non autorisée : {path}")
    if ticket.test_file not in paths:
        raise core.PolicyError("test_file absent de allowed_files")
    return tuple(paths)


def validate_test_content(*, path: str, content: str, limits: core.Limits) -> None:
    normalized = normalize_path(path)
    if not is_test_path(normalized):
        raise core.PolicyError("chemin de test invalide")
    if "\x00" in content:
        raise core.PolicyError("contenu de test invalide")
    if len(content.encode("utf-8")) > limits.max_bytes:
        raise core.PolicyError("test de reproduction trop volumineux")
    if "def test_" not in content and "async def test_" not in content:
        raise core.PolicyError("le test de reproduction est absent")
    lowered = content.lower()
    for marker in _FORBIDDEN_TEST_MARKERS:
        if marker.lower() in lowered:
            raise core.PolicyError(f"marqueur interdit dans le test : {marker}")


def parse_diff(patch: str) -> core.DiffStats:
    if not patch.strip():
        raise core.PolicyError("diff vide")
    if "\x00" in patch:
        raise core.PolicyError("diff binaire interdit")
    encoded = patch.encode("utf-8")
    files: list[str] = []
    added = 0
    removed = 0
    hunks = 0
    current: str | None = None
    minus_path: str | None = None
    plus_path: str | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            if current is not None and (minus_path != current or plus_path != current):
                raise core.PolicyError("chemins d'en-tête incohérents")
            match = _DIFF_HEADER.fullmatch(line)
            if match is None:
                raise core.PolicyError("en-tête de diff invalide")
            left, right = match.groups()
            if left != right:
                raise core.PolicyError("renommage interdit")
            normalized = normalize_path(left)
            if " " in normalized:
                raise core.PolicyError("chemin avec espace interdit")
            files.append(normalized)
            current = normalized
            minus_path = None
            plus_path = None
            continue
        if any(line.startswith(marker) for marker in _DIFF_FORBIDDEN):
            raise core.PolicyError("forme de diff interdite")
        if line.startswith("--- "):
            if current is None or "/dev/null" in line:
                raise core.PolicyError("création ou suppression de fichier interdite")
            raw = line[4:].split("\t", 1)[0]
            if not raw.startswith("a/"):
                raise core.PolicyError("chemin de diff invalide")
            minus_path = normalize_path(raw[2:])
            continue
        if line.startswith("+++ "):
            if current is None or "/dev/null" in line:
                raise core.PolicyError("création ou suppression de fichier interdite")
            raw = line[4:].split("\t", 1)[0]
            if not raw.startswith("b/"):
                raise core.PolicyError("chemin de diff invalide")
            plus_path = normalize_path(raw[2:])
            if minus_path != current or plus_path != current:
                raise core.PolicyError("chemins d'en-tête incohérents")
            continue
        if line.startswith("@@"):
            hunks += 1
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    if current is not None and (minus_path != current or plus_path != current):
        raise core.PolicyError("chemins d'en-tête incohérents")
    if not files or hunks == 0:
        raise core.PolicyError("diff sans fichier ou sans hunk")
    if len(set(files)) != len(files):
        raise core.PolicyError("fichier répété dans le diff")
    if added + removed == 0:
        raise core.PolicyError("diff sans modification")
    return core.DiffStats(
        files=tuple(files),
        added=added,
        removed=removed,
        bytes=len(encoded),
        sha256=core.sha256_bytes(encoded),
    )


def validate_diff(
    *, patch: str, allowed_files: Sequence[str], test_file: str, limits: core.Limits
) -> core.DiffStats:
    stats = parse_diff(patch)
    allowed = set(allowed_files)
    for path in stats.files:
        if path == test_file:
            raise core.PolicyError("le correctif ne peut pas modifier le test")
        if path not in allowed:
            raise core.PolicyError(f"fichier hors allowlist : {path}")
        if is_protected(path):
            raise core.PolicyError(f"fichier protégé : {path}")
    if len(stats.files) > limits.max_files:
        raise core.PolicyError("trop de fichiers dans le diff")
    if stats.added + stats.removed > limits.max_lines:
        raise core.PolicyError("diff trop long")
    if stats.bytes > limits.max_bytes:
        raise core.PolicyError("diff trop volumineux")
    return stats


def _final_report(stdout: str, stderr: str) -> dict[str, Any] | None:
    payload: dict[str, Any] | None = None
    for line in (stdout + "\n" + stderr).splitlines():
        if not line.startswith("FINAL_SUITE_REPORT "):
            continue
        try:
            candidate = json.loads(line[len("FINAL_SUITE_REPORT ") :])
        except ValueError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
    return payload


def _has_error_marker(text: str) -> bool:
    return any(marker in text for marker in _ERROR_MARKERS)


def classify_test_output(
    *,
    outcome: core.CommandOutcome,
    test_path: str,
    phase: str,
) -> core.TestObservation:
    report = _final_report(outcome.stdout, outcome.stderr)
    collected: list[str] = []
    failed = 0
    passed = 0
    reports: dict[str, Any] = {}
    report_valid = False
    if report is not None:
        raw_nodes = report.get("nodes")
        raw_collected = report.get("collected")
        raw_reports = report.get("reports")
        if isinstance(raw_nodes, list):
            collected = [item for item in raw_nodes if isinstance(item, str)]
        elif isinstance(raw_collected, list):
            collected = [item for item in raw_collected if isinstance(item, str)]
        if isinstance(raw_reports, dict) and collected:
            reports = raw_reports
            raw_failed = report.get("failed")
            raw_passed = report.get("passed")
            failed = raw_failed if isinstance(raw_failed, int) else 0
            passed = raw_passed if isinstance(raw_passed, int) else 0
            report_valid = True
    error_text = outcome.stdout + "\n" + outcome.stderr
    errors = 1 if _has_error_marker(error_text) else 0
    nodeids = tuple(collected)
    matching = [
        nodeid
        for nodeid in collected
        if nodeid == test_path or nodeid.startswith(f"{test_path}::")
    ]
    call_failed = any(
        isinstance(reports.get(nodeid), dict)
        and reports.get(nodeid, {}).get("call") == "failed"
        for nodeid in matching
    )
    functional = (
        phase == "before"
        and report_valid
        and len(nodeids) == 1
        and len(matching) == 1
        and failed >= 1
        and call_failed
        and errors == 0
        and outcome.exit_code == 1
        and not outcome.timed_out
        and not outcome.workspace_modified
    )
    return core.TestObservation(
        collected=len(nodeids),
        failed=failed,
        passed=passed,
        errors=errors,
        exit_code=outcome.exit_code,
        functional_failure=functional,
        report_valid=report_valid,
        nodeids=nodeids,
        output_excerpt=core.excerpt(error_text),
    )


def successful_test(observation: core.TestObservation) -> bool:
    return (
        observation.report_valid
        and observation.collected == 1
        and observation.failed == 0
        and observation.passed >= 1
        and observation.errors == 0
        and observation.exit_code == 0
    )


def successful_control(outcome: core.CommandOutcome) -> bool:
    if outcome.timed_out or outcome.workspace_modified or outcome.exit_code != 0:
        return False
    report = _final_report(outcome.stdout, outcome.stderr)
    if report is None:
        return False
    failed = report.get("failed")
    nodes = report.get("nodes")
    if not isinstance(nodes, list):
        nodes = report.get("collected")
    return (
        failed == 0
        and isinstance(nodes, list)
        and bool(nodes)
        and report.get("pytest_exit_code") == 0
    )


def _workspace_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    if not root.exists():
        raise core.PolicyError("workspace absent")
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _IGNORED_NAMES and not name.startswith(".git")
        ]
        for filename in filenames:
            if filename in _IGNORED_NAMES or filename == ".env":
                continue
            path = Path(directory) / filename
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                continue
            files[relative] = path
    return files


def diff_workspace(
    *,
    base: Path,
    after: Path,
    allowed_files: Sequence[str],
    test_file: str,
    limits: core.Limits,
) -> core.DiffStats:
    base_files = _workspace_files(base)
    after_files = _workspace_files(after)
    removed_paths = sorted(set(base_files) - set(after_files))
    if removed_paths:
        raise core.PolicyError(f"suppression de fichier interdite : {removed_paths[0]}")
    changed: list[str] = []
    added = 0
    removed = 0
    diff_parts: list[str] = []
    allowed = set(allowed_files)
    for relative in sorted(after_files):
        before_path = base_files.get(relative)
        after_path = after_files[relative]
        before_bytes = before_path.read_bytes() if before_path is not None else b""
        after_bytes = after_path.read_bytes()
        if before_bytes == after_bytes:
            continue
        if relative not in allowed:
            raise core.PolicyError(f"fichier hors allowlist : {relative}")
        if is_protected(relative):
            raise core.PolicyError(f"fichier protégé : {relative}")
        if before_path is not None and stat.S_IMODE(before_path.stat().st_mode) != stat.S_IMODE(
            after_path.stat().st_mode
        ):
            raise core.PolicyError(f"changement de mode interdit : {relative}")
        changed.append(relative)
        try:
            before_text = before_bytes.decode("utf-8")
            after_text = after_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise core.PolicyError(f"fichier non textuel : {relative}") from exc
        before_lines = before_text.splitlines()
        after_lines = after_text.splitlines()
        diff_lines = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                lineterm="",
            )
        )
        diff_parts.extend(line + "\n" for line in diff_lines)
        added += sum(
            1
            for line in diff_lines
            if line.startswith("+") and not line.startswith("+++")
        )
        removed += sum(
            1
            for line in diff_lines
            if line.startswith("-") and not line.startswith("---")
        )
    if not changed:
        raise core.PolicyError("aucune modification produite")
    if test_file not in changed:
        raise core.PolicyError("le test de reproduction n'a pas été ajouté")
    if len(changed) > limits.max_files:
        raise core.PolicyError("trop de fichiers modifiés")
    if added + removed > limits.max_lines:
        raise core.PolicyError("diff trop long")
    diff_text = "".join(diff_parts)
    encoded = diff_text.encode("utf-8")
    if len(encoded) > limits.max_bytes:
        raise core.PolicyError("diff trop volumineux")
    return core.DiffStats(
        files=tuple(changed),
        added=added,
        removed=removed,
        bytes=len(encoded),
        sha256=core.sha256_bytes(encoded),
    )


def validate_fix_files(*, stats: core.DiffStats, fix_files: Iterable[str], test_file: str) -> None:
    actual = {path for path in stats.files if path != test_file}
    declared = {normalize_path(path) for path in fix_files}
    if actual != declared:
        raise core.PolicyError("les fichiers du correctif ne correspondent pas au diff")



def validate_context_files(paths: Sequence[str], hashes: Mapping[str, str]) -> None:
    if not 1 <= len(paths) <= 8 or len(set(paths)) != len(paths):
        raise core.PolicyError("liste de contexte invalide")
    if set(paths) != set(hashes):
        raise core.PolicyError("empreintes de contexte incomplètes")
    for path in paths:
        normalized = normalize_path(path)
        parts = PurePosixPath(normalized).parts
        if (normalized != path or not normalized.startswith(("app/", "docs/", "tests/unit/",
                                                             "tests/integration/"))
                or not normalized.endswith((".py", ".md"))
                or any(part.casefold() in {"private", "secrets", ".git", ".ssh"}
                       or part.casefold().startswith(".env") for part in parts)):
            raise core.PolicyError("chemin de contexte interdit")
        if not isinstance(hashes[path], str) or not re.fullmatch(r"[0-9a-f]{64}", hashes[path]):
            raise core.PolicyError("empreinte de contexte invalide")

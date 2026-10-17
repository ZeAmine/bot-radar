"""Runner local déterministe, sans conteneur ni réseau ajouté."""

from __future__ import annotations

import contextlib
import hashlib
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO, Protocol

from app.services.alert_fix import core

MAX_OUTPUT_BYTES = 1_048_576
DEFAULT_COMMAND_TIMEOUT = 600.0
_IGNORED_NAMES = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
_RESOURCE_LIMITS = (
    "RLIMIT_CPU",
    "RLIMIT_FSIZE",
    "RLIMIT_NOFILE",
    "RLIMIT_NPROC",
    "RLIMIT_CORE",
    "RLIMIT_AS",
)


class Runner(Protocol):
    def run_test(
        self, workspace: Path, test_file: str, *, phase: str, deadline: float
    ) -> core.CommandOutcome: ...

    def run_ruff(self, workspace: Path, *, deadline: float) -> core.CommandOutcome: ...

    def run_mypy(self, workspace: Path, *, deadline: float) -> core.CommandOutcome: ...

    def run_isolated_suite(
        self, workspace: Path, *, deadline: float
    ) -> core.CommandOutcome: ...


def _supported_limits() -> tuple[str, ...]:
    names: list[str] = []
    for name in _RESOURCE_LIMITS:
        resource_id = getattr(resource, name, None)
        if resource_id is None:
            continue
        try:
            soft, hard = resource.getrlimit(resource_id)
            resource.setrlimit(resource_id, (soft, hard))
        except (OSError, ValueError):
            continue
        names.append(name)
    return tuple(names)


def _limit_value(name: str, timeout: float) -> int:
    if name == "RLIMIT_CPU":
        return max(1, min(int(timeout), 600))
    if name == "RLIMIT_FSIZE":
        return 64 * 1024 * 1024
    if name == "RLIMIT_NOFILE":
        return 256
    if name == "RLIMIT_NPROC":
        return 1024
    if name == "RLIMIT_CORE":
        return 0
    if name == "RLIMIT_AS":
        return 2 * 1024 * 1024 * 1024
    return 0


def _preexec(timeout: float) -> None:
    for name in _RESOURCE_LIMITS:
        resource_id = getattr(resource, name, None)
        if resource_id is None:
            continue
        value = _limit_value(name, timeout)
        try:
            _, hard = resource.getrlimit(resource_id)
            if hard != resource.RLIM_INFINITY:
                value = min(value, hard)
            resource.setrlimit(resource_id, (value, hard))
        except (OSError, ValueError):
            continue


def _create_empty_env(execution: Path) -> None:
    temporary = execution / "env.fake"
    temporary.write_bytes(b"")
    os.replace(temporary, execution / ".env")


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _IGNORED_NAMES
        )
        for filename in sorted(filenames):
            if filename in _IGNORED_NAMES or filename == ".env":
                continue
            path = Path(directory) / filename
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(b"symlink:")
                digest.update(os.readlink(path).encode("utf-8"))
            else:
                digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _read_bounded(handle: BinaryIO) -> str:
    handle.flush()
    size = handle.tell()
    handle.seek(0)
    data = handle.read(MAX_OUTPUT_BYTES)
    if size > MAX_OUTPUT_BYTES:
        data += b"\n[... tronque ...]\n"
    return data.decode("utf-8", errors="replace")


def _safe_name(value: str) -> str:
    cleaned = "".join(
        character for character in value if character.isalnum() or character in "-_"
    )
    return cleaned or "command"


class LocalRunner:
    def __init__(
        self,
        run_root: Path,
        *,
        python: str | None = None,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        self.run_root = run_root
        self.python = python or sys.executable
        self.command_timeout = command_timeout
        self.limits_applied = _supported_limits()
        for directory in (
            self.run_root / "home",
            self.run_root / "tmp",
            self.run_root / "executions",
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def environment(self, execution: Path) -> dict[str, str]:
        return {
            "PATH": f"{Path(self.python).parent}:/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": str(execution),
            "HOME": str(self.run_root / "home"),
            "TMPDIR": str(self.run_root / "tmp"),
            "TMP": str(self.run_root / "tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "TZ": "UTC",
        }

    def _run(
        self,
        *,
        workspace: Path,
        command: Sequence[str],
        name: str,
        deadline: float,
    ) -> core.CommandOutcome:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise core.RunnerError("délai global dépassé")
        execution = (
            self.run_root / "executions" / f"{_safe_name(name)}-{os.urandom(4).hex()}"
        )
        shutil.copytree(
            workspace,
            execution,
            ignore=shutil.ignore_patterns(
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".env",
            ),
            symlinks=True,
        )
        _create_empty_env(execution)
        before = _tree_hash(execution)
        started = time.monotonic()
        command_timeout = min(self.command_timeout, remaining)
        argv = tuple(str(part) for part in command)
        modified = False
        timed_out = False
        try:
            with (
                tempfile.TemporaryFile(dir=self.run_root / "tmp") as stdout_file,
                tempfile.TemporaryFile(dir=self.run_root / "tmp") as stderr_file,
            ):
                try:
                    process = subprocess.Popen(
                        argv,
                        cwd=execution,
                        env=self.environment(execution),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        start_new_session=True,
                        preexec_fn=lambda: _preexec(command_timeout),
                    )
                except (OSError, ValueError) as exc:
                    raise core.RunnerError("commande locale impossible") from exc
                try:
                    process.wait(timeout=command_timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait()
                stdout = _read_bounded(stdout_file)
                stderr = _read_bounded(stderr_file)
            env_path = execution / ".env"
            env_size = env_path.stat().st_size if env_path.exists() else -1
            modified = before != _tree_hash(execution) or env_size != 0
        finally:
            if not modified:
                shutil.rmtree(execution, ignore_errors=True)
        return core.CommandOutcome(
            name=name,
            argv=argv,
            exit_code=process.returncode,
            duration_seconds=time.monotonic() - started,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            workspace_modified=modified,
            limits_applied=self.limits_applied,
            infra_files=(".env",),
        )

    def run_test(
        self, workspace: Path, test_file: str, *, phase: str, deadline: float
    ) -> core.CommandOutcome:
        return self._run(
            workspace=workspace,
            command=(
                self.python,
                "-B",
                "scripts/verify_consolidation.py",
                "unit",
                "--target",
                test_file,
            ),
            name=f"test-{phase}",
            deadline=deadline,
        )

    def run_ruff(self, workspace: Path, *, deadline: float) -> core.CommandOutcome:
        return self._run(
            workspace=workspace,
            command=(
                self.python,
                "-m",
                "ruff",
                "check",
                ".",
                "--cache-dir",
                str(self.run_root / "cache" / "ruff"),
            ),
            name="ruff",
            deadline=deadline,
        )

    def run_mypy(self, workspace: Path, *, deadline: float) -> core.CommandOutcome:
        return self._run(
            workspace=workspace,
            command=(
                self.python,
                "-m",
                "mypy",
                "--cache-dir",
                str(self.run_root / "cache" / "mypy"),
                "app",
                "scripts/matching_diagnostics.py",
            ),
            name="mypy",
            deadline=deadline,
        )

    def run_isolated_suite(
        self, workspace: Path, *, deadline: float
    ) -> core.CommandOutcome:
        return self._run(
            workspace=workspace,
            command=(
                self.python,
                "-B",
                "scripts/verify_consolidation.py",
                "unit",
                "--target",
                "tests",
            ),
            name="test-isolated",
            deadline=deadline,
        )

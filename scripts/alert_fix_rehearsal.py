#!/usr/bin/env python3
"""Rehearsal locale de la boucle prepare, sans réseau ni publication."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import alert_fix_fixtures as fixtures  # noqa: E402

_GIT_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(repo), *arguments),
        check=False,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"rehearsal échouée : {message}")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--keep", action="store_true")
    return parser.parse_args(argv)


def _run_prepare(
    *, clone: Path, ticket: Path, proposals: Path, state_dir: Path, ticket_hash: str
) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(state_dir / "home"),
        "TMPDIR": str(state_dir / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    (state_dir / "home").mkdir(parents=True, exist_ok=True)
    (state_dir / "tmp").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        (
            sys.executable,
            str(ROOT / "scripts" / "alert_fix.py"),
            "prepare",
            "--ticket",
            str(ticket),
            "--repo",
            str(clone),
            "--state-dir",
            str(state_dir),
            "--confirm-prepare",
            "--trusted-recorded",
            "--confirm-ticket-hash",
            ticket_hash,
            "--recorded-proposals",
            str(proposals),
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
        env=environment,
    )



def check_branches(repo: Path, expected: str) -> None:
    current = _git(repo, "branch", "--format=%(refname)")
    _require(current.returncode == 0, "branches illisibles")
    _require(current.stdout.strip() == expected, "branches modifiées par prepare")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    source = args.repo.resolve()
    status = _git(source, "status", "--porcelain", "--untracked-files=no")
    _require(status.returncode == 0, "dépôt source illisible")
    _require(not status.stdout.strip(), "dépôt source avec modifications suivies")
    clone_root = Path(tempfile.mkdtemp(prefix="alert-fix-rehearsal-clone-"))
    clone = clone_root / "repo"
    state_dir = args.state_dir or Path(tempfile.mkdtemp(prefix="alert-fix-rehearsal-state-"))
    try:
        clone_result = subprocess.run(
            ("git", "clone", "--no-hardlinks", "--quiet", str(source), str(clone)),
            check=False,
            capture_output=True,
            text=True,
            env=_GIT_ENV,
        )
        _require(clone_result.returncode == 0, "clone local impossible")
        bug_path = clone_root / "bug.diff"
        bug_path.write_text(fixtures.SYNTHETIC_BUG_PATCH, encoding="utf-8")
        apply_result = _git(clone, "apply", str(bug_path))
        _require(apply_result.returncode == 0, "patch de bug synthétique inapplicable")
        commit = _git(
            clone,
            "-c",
            "user.name=BotRadar Rehearsal",
            "-c",
            "user.email=rehearsal",
            "commit",
            "-q",
            "-am",
            "test: injecter le bug de rehearsal",
        )
        _require(commit.returncode == 0, "commit local du bug impossible")
        base_sha = _git(clone, "rev-parse", "HEAD").stdout.strip()
        ticket = fixtures.build_ticket(
            base_sha=base_sha,
            now=datetime.now(UTC),
            allowed_files=(fixtures.BUG_FILE, fixtures.TEST_PATH),
            test_file=fixtures.TEST_PATH,
            evidence_files=(fixtures.BUG_FILE,),
            evidence_hashes={fixtures.BUG_FILE: fixtures.core.sha256_bytes(
                (clone / fixtures.BUG_FILE).read_bytes())},
        )
        ticket_path = state_dir / "rehearsal-ticket.json"
        proposals_path = state_dir / "rehearsal-proposals.json"
        fixtures.write_ticket(ticket_path, ticket)
        fixtures.write_recorded(proposals_path)
        branches_before = _git(clone, "branch", "--format=%(refname)").stdout.strip()
        first = _run_prepare(
            clone=clone,
            ticket=ticket_path,
            proposals=proposals_path,
            state_dir=state_dir,
            ticket_hash=ticket.ticket_hash,
        )
        _require(first.returncode == 0, f"prepare a échoué : {first.stderr[-2000:]}")
        reports = sorted((state_dir / "runs").glob("*/run.json"))
        _require(len(reports) == 1, "un seul rapport attendu")
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        _require(report["status"] == "ready_for_review", "rapport non prêt")
        _require(report["publication"] == "none", "publication inattendue")
        _require(report["llm"]["mode"] == "recorded", "mode LLM inattendu")
        _require(report["llm"]["usage"] is None, "usage inattendu")
        _require(report["llm"]["cost_usd"] is None, "coût inattendu")
        _require(report["test"]["before"]["functional"] is True, "test avant non rouge")
        _require(report["test"]["after"]["failed"] == 0, "test après non vert")
        _require(
            all(control["exit_code"] == 0 for control in report["controls"]),
            "contrôle en échec",
        )
        _require(
            report["infra_files"]
            == [{"path": ".env", "bytes": 0, "created_by": "runner", "secret": False}],
            ".env vide non listé",
        )
        expected_files = [fixtures.BUG_FILE, fixtures.TEST_PATH]
        _require(report["diff"]["files"] == expected_files, "diff inattendu")
        _require(reports[0].with_name("REPORT.md").exists(), "REPORT.md absent")
        second = _run_prepare(
            clone=clone,
            ticket=ticket_path,
            proposals=proposals_path,
            state_dir=state_dir,
            ticket_hash=ticket.ticket_hash,
        )
        _require(second.returncode == 4, "second usage du ticket non refusé")
        _require(
            len(list((state_dir / "runs").glob("*/run.json"))) == 1,
            "un second run a été créé",
        )
        ledger = (state_dir / "used_tickets.jsonl").read_text(encoding="utf-8").splitlines()
        _require(len(ledger) == 1, "ledger inattendu")
        clean = _git(clone, "status", "--porcelain")
        _require(not clean.stdout.strip(), "clone modifié par prepare")
        check_branches(clone, branches_before)
        head = _git(clone, "rev-parse", "HEAD").stdout.strip()
        _require(head == base_sha, "commit créé par prepare")
        source_after = _git(source, "status", "--porcelain", "--untracked-files=no")
        _require(not source_after.stdout.strip(), "dépôt source modifié")
        print(f"REPORT {reports[0]}")
        print(f"STATE {state_dir}")
        if args.keep:
            print(f"CLONE {clone}")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(clone_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

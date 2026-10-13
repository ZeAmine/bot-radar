#!/usr/bin/env python
"""Rejoue la chaîne de qualité sur chaque commit de la branche.

Un découpage d'historique qui produit un commit rouge est indétectable sans ce
garde-fou : les tests passent sur `HEAD` alors qu'un état intermédiaire est cassé.
Le script crée un worktree jetable par commit, y rejoue Ruff et Pytest (mypy sur option), puis
sort en erreur au premier commit qui échoue.

Le travail principal n'est jamais touché : ni son index, ni ses fichiers, ni ses
répertoires. Seul un worktree temporaire est créé puis supprimé, dans un `finally`.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Jetons factices identiques à ceux de `.github/workflows/ci.yml` : sans `.env`,
# les tests d'intégration d'API et de webhook échouent faute de secret.
CI_TOKENS = {
    "TELEGRAM_BOT_TOKEN": "123456789:AAG_ci_test_dummy_token_abc123",
    "TELEGRAM_WEBHOOK_SECRET": "ci_webhook_secret_test",
    "INGEST_API_TOKEN": "ci_ingest_token_test",
    "INTERNAL_API_TOKEN": "ci_internal_token_test",
}
DEFAULT_STEPS = ("ruff", "pytest")
# `mypy` est proposé mais pas par défaut : c'est un objectif de bout de branche. Un
# refactor de typage ne peut pas produire des commits intermédiaires verts, sinon il
# faudrait tout écraser. Ruff et Pytest, eux, sont le contrat de chaque commit.
OPTIONAL_STEPS = ("mypy",)
ALL_STEPS = DEFAULT_STEPS + OPTIONAL_STEPS
# Arguments passés à chaque outil : `ruff` seul n'afficherait que son aide.
STEP_ARGS = {"ruff": ["check", "."], "mypy": ["app"], "pytest": ["-q"]}
OUTPUT_TAIL = 30


@dataclass
class StepResult:
    """Verdict d'une commande de qualité sur un commit donné."""

    name: str
    passed: bool
    summary: str
    tail: str = ""


class HistoryError(RuntimeError):
    """Erreur d'exécution du garde-fou (distincte d'un échec de qualité)."""


def git(*args: str) -> str:
    """Exécute une commande git et renvoie sa sortie standard."""
    proc = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise HistoryError(f"git {' '.join(args)} a échoué : {proc.stderr.strip()}")
    return proc.stdout.strip()


def is_ancestor(candidate: str, descendant: str = "HEAD") -> bool:
    """Indique si `candidate` est un ancêtre de `descendant` (sans lever)."""
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate, descendant],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def resolve_interpreter() -> Path:
    """Retourne l'interpréteur Python du projet, ou celui de l'environnement."""
    local = REPO_ROOT / ".venv" / "bin" / "python"
    if local.exists():
        return local
    return Path(shutil.which("python3") or sys.executable)


def resolve_command(name: str) -> list[str]:
    """Construit la commande d'un outil de qualité, le `.venv` local en priorité."""
    local = REPO_ROOT / ".venv" / "bin" / name
    if local.exists():
        return [str(local)]
    if name == "pytest":
        return [str(resolve_interpreter()), "-m", "pytest"]
    found = shutil.which(name)
    if not found:
        raise HistoryError(
            f"Outil introuvable : {name} (installez le projet avec `pip install -e .[dev]`)"
        )
    return [found]


def resolve_base(explicit: str | None) -> str:
    """Détermine le commit de départ : argument, variable d'env, tag `v*`, puis HEAD~5."""
    if explicit:
        return git("rev-parse", explicit)
    from_env = os.environ.get("CHECK_HISTORY_BASE")
    if from_env:
        return git("rev-parse", from_env)
    tags = git("tag", "--list", "v*", "--sort=-creatordate").splitlines()
    head = git("rev-parse", "HEAD")
    for tag in tags:
        # Un tag posé sur HEAD est le commit qu'on est en train de vérifier : c'est
        # le précédent qu'il faut prendre comme base.
        if git("rev-parse", tag) != head and is_ancestor(tag):
            return git("rev-parse", tag)
    return git("rev-parse", "HEAD~5")


def commit_range(base: str, max_commits: int) -> list[str]:
    """Liste les commits de `base` à HEAD, du plus ancien au plus récent."""
    revs = git("rev-list", "--reverse", "--first-parent", f"{base}..HEAD").splitlines()
    return revs[-max_commits:] if max_commits else revs


def summarize(name: str, output: str, returncode: int) -> str:
    """Réduit la sortie d'une étape à une phrase lisible dans un tableau."""
    if name == "ruff":
        if "All checks passed" in output:
            return "OK"
        errors = [line for line in output.splitlines() if "error" in line.lower()]
        return errors[0][:60] if errors else f"échec ({returncode})"
    if name == "mypy":
        found = re.search(r"Found (\d+) errors? in (\d+) files?", output)
        if found:
            return f"{found.group(1)} erreurs"
        return "0 erreur" if "Success: no issues" in output else f"échec ({returncode})"
    passed = re.search(r"(\d+) passed", output)
    failed = re.search(r"(\d+) failed", output)
    if failed:
        return f"{passed.group(1) if passed else '?'} passés, {failed.group(1)} échecs"
    if passed:
        return f"{passed.group(1)} tests passés"
    return f"échec ({returncode})"


def run_step(name: str, worktree: Path) -> StepResult:
    """Exécute une étape de qualité dans le worktree et résume son verdict."""
    command = [*resolve_command(name), *STEP_ARGS[name]]
    proc = subprocess.run(
        command,
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **CI_TOKENS},
    )
    output = (proc.stdout + proc.stderr).strip()
    return StepResult(
        name=name,
        passed=proc.returncode == 0,
        summary=summarize(name, output, proc.returncode),
        tail="\n".join(output.splitlines()[-OUTPUT_TAIL:]),
    )


def check_commit(sha: str, worktree_root: Path, steps: tuple[str, ...]) -> list[StepResult]:
    """Rejoue les étapes de qualité sur un commit, dans un worktree jetable."""
    worktree = worktree_root / sha[:7]
    git("worktree", "add", "--detach", "--quiet", str(worktree), sha)
    try:
        local_env = REPO_ROOT / ".env"
        if local_env.is_file():
            shutil.copy2(local_env, worktree / ".env")
        return [run_step(step, worktree) for step in steps]
    finally:
        git("worktree", "remove", "--force", str(worktree))


def parse_args() -> argparse.Namespace:
    """Analyse les arguments de ligne de commande."""
    parser = argparse.ArgumentParser(
        description="Rejoue Ruff et Pytest sur chaque commit (mypy sur option).",
    )
    parser.add_argument(
        "--base",
        help="Commit ou tag de départ (défaut : $CHECK_HISTORY_BASE, "
        "sinon le dernier tag v* ancêtre, sinon HEAD~5)",
    )
    parser.add_argument(
        "--max-commits",
        type=int,
        default=0,
        help="Ne vérifier que les N derniers commits (0 = tous)",
    )
    parser.add_argument(
        "--steps",
        default=",".join(DEFAULT_STEPS),
        help=f"Étapes à rejouer, séparées par des virgules (parmi {', '.join(ALL_STEPS)})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    steps = tuple(step.strip() for step in args.steps.split(",") if step.strip())
    unknown = set(steps) - set(ALL_STEPS)
    if unknown:
        raise HistoryError(f"Étapes inconnues : {', '.join(sorted(unknown))}")

    base = resolve_base(args.base)
    commits = commit_range(base, args.max_commits)
    if not commits:
        print(f"Aucun commit à vérifier entre {base[:7]} et HEAD.")
        return 0

    print(f"Historique vert : {len(commits)} commit(s) depuis {base[:7]}")
    print(f"Interpréteur : {resolve_interpreter()}\n")
    header = f"{'commit':8} {'sujet':50}" + "".join(f"{step:>22}" for step in steps)
    print(header)
    print("-" * len(header))

    worktree_root = Path(tempfile.mkdtemp(prefix="check_history_"))
    failed_sha: str | None = None
    try:
        for sha in commits:
            subject = git("log", "-1", "--format=%s", sha)[:48]
            results = check_commit(sha, worktree_root, steps)
            cells = "".join(f"{result.summary:>22}" for result in results)
            print(f"{sha[:7]} {subject:50}{cells}")
            broken = [result for result in results if not result.passed]
            if broken:
                failed_sha = sha
                for result in broken:
                    print(f"\n--- {result.name} échoue sur {sha[:7]} ---\n")
                    print(result.tail)
                break
    finally:
        shutil.rmtree(worktree_root, ignore_errors=True)
        git("worktree", "prune")

    if failed_sha:
        subject = git("log", "-1", "--format=%s", failed_sha)
        print(f"\nHISTORIQUE ROUGE : {failed_sha[:7]} — {subject}")
        return 1
    print(f"\nHISTORIQUE VERT : {len(commits)} commit(s) vérifiés.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HistoryError as error:
        print(f"ERREUR : {error}", file=sys.stderr)
        raise SystemExit(2) from error

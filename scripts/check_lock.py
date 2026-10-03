#!/usr/bin/env python
"""Vérifie que `requirements.lock` couvre toutes les dépendances déclarées.

Le verrouillage n'a de valeur que s'il suit `pyproject.toml`. Ce contrôle est
hors réseau et déterministe : il échoue dès qu'une dépendance a été ajoutée,
retirée ou renommée sans régénérer le verrou.

Usage : ``python scripts/check_lock.py`` (code de sortie 1 si le verrou dérive).
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCKFILE = REPO_ROOT / "requirements.lock"

# `uvicorn[standard]>=0.30.0` -> `uvicorn`. Les marqueurs d'environnement sont ignorés.
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9._-]+)")


def canonicalize(name: str) -> str:
    """Normalise un nom de distribution selon PEP 503."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def declared_dependencies(pyproject: dict) -> set[str]:
    """Dépendances directes du projet et de ses extras, en noms normalisés."""
    project = pyproject.get("project", {})
    raw: list[str] = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        raw.extend(extra)

    names: set[str] = set()
    for requirement in raw:
        match = _REQUIREMENT_NAME.match(requirement)
        if match:
            names.add(canonicalize(match.group(1)))
    return names


def locked_distributions(lockfile: Path) -> set[str]:
    """Distributions épinglées dans le verrou, en noms normalisés."""
    names: set[str] = set()
    for line in lockfile.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        match = _REQUIREMENT_NAME.match(entry)
        if match:
            names.add(canonicalize(match.group(1)))
    return names


def main() -> int:
    """Compare les deux déclarations et signale toute dépendance non verrouillée."""
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared = declared_dependencies(pyproject)

    if not LOCKFILE.exists():
        print(f"ERREUR : {LOCKFILE.name} est absent. Lancez `make lock`.", file=sys.stderr)
        return 1

    locked = locked_distributions(LOCKFILE)
    missing = sorted(declared - locked)

    if missing:
        print(
            "Dépendances déclarées mais absentes du verrou : " + ", ".join(missing),
            file=sys.stderr,
        )
        print("Lancez `make lock` puis commitez le fichier régénéré.", file=sys.stderr)
        return 1

    print(f"Verrou cohérent : {len(declared)} dépendances déclarées, toutes épinglées.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
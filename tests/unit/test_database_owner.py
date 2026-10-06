"""`tests/database.py` est le seul à créer un moteur SQLAlchemy ou un schéma de test.

Dix copies du même enchaînement — moteur, `drop_all`/`create_all`, fabrique — avaient
fini par diverger en silence (voir la docstring de `tests/database.py`). Le verrou
porte cette propriété à l'exécution : dès qu'un fichier de test, harnais compris,
recrée **son propre** moteur ou **son propre** schéma au lieu de passer par le module
de base partagé, ce fichier échoue et nomme le coupable.

Le contrôle est syntaxique (AST), pas textuel : les seules chaînes nommant ces
symboles dans ce fichier sont des constantes, qui ne sont ni des appels ni des
attributs — le garde ne peut donc pas se déclencher lui-même.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]
OWNER = TESTS_ROOT / "database.py"

FORBIDDEN_CALLS = {"create_async_engine", "create_engine"}
FORBIDDEN_SCHEMA_ATTRS = {"create_all", "drop_all"}


def _violations() -> list[str]:
    """Chaque recréation de moteur ou de schéma hors du propriétaire, `fichier:ligne`."""
    found: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path == OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(TESTS_ROOT.parent).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_CALLS:
                    found.append(f"{rel}:{node.lineno} — {node.func.id}()")
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_SCHEMA_ATTRS:
                found.append(f"{rel}:{node.lineno} — {ast.unparse(node)}")
    return found


def test_no_test_file_recreates_its_own_engine_or_schema() -> None:
    violations = _violations()
    assert violations == [], (
        "Moteur ou schéma recréé hors de tests/database.py :\n" + "\n".join(violations)
    )


def test_database_module_is_the_single_owner() -> None:
    """Le propriétaire existe et crée exactement un moteur : ni plus, ni ailleurs."""
    assert OWNER.is_file(), (
        "tests/database.py manquant : plus aucun propriétaire du moteur de test."
    )
    tree = ast.parse(OWNER.read_text(encoding="utf-8"), filename=str(OWNER))
    engines = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_async_engine"
    ]
    assert len(engines) == 1, (
        f"tests/database.py doit créer exactement un moteur, {len(engines)} trouvé(s)."
    )

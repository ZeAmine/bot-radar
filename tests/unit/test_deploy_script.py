"""Tests du script de déploiement : l'ordre des étapes et l'arrêt au premier échec.

Un déploiement est une suite d'étapes dont dépend l'état final du bot. Ce qui n'est
pas vérifiable localement — le rsync, le build, la migration — est donc garanti par
construction : le script ne peut ni réordonner les étapes, ni en sauter une, et il ne
poursuit jamais après un échec.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from deploy import (  # noqa: E402
    HEALTH_ATTEMPTS,
    RSYNC_EXCLUDES,
    Context,
    rsync_argv,
    run_steps,
    step_migrate,
    step_rebuild,
)


def _ctx(**overrides) -> Context:
    defaults = {
        "host": "ubuntu@example",
        "key": "/tmp/id_test",
        "remote_dir": "~/bot-radar",
        "repo_root": Path("/tmp/depot"),
    }
    defaults.update(overrides)
    return Context(**defaults)  # type: ignore[arg-type]


def _recorder(called: list[str], label: str, code: int = 0):
    """Étape factice : note son passage dans l'ordre et rend le code demandé."""

    def _step(_ctx: Context) -> int:
        called.append(label)
        return code

    return _step


class _FakeRemote:
    """Enregistre les scripts shell au lieu de les exécuter."""

    def __init__(self, code: int = 0) -> None:
        self.scripts: list[str] = []
        self.code = code

    def run(self, script: str) -> tuple[int, str, str]:
        self.scripts.append(script)
        return self.code, "sortie", ""


def test_steps_run_in_order_and_reach_the_end():
    """Sans échec, chaque étape est exécutée une fois, dans l'ordre déclaré."""
    called: list[str] = []

    steps = tuple((label, _recorder(called, label)) for label in "abc")
    assert run_steps(steps, _ctx()) == 0
    assert called == ["a", "b", "c"]


def test_first_failure_stops_everything_after_it():
    """Un échec arrête la chaîne : aucune étape suivante n'est tentée.

    Le code de retour rendu est celui de l'étape fautive : un rsync interrompu et
    un pré-vol en échec n'appellent pas la même correction.
    """
    called: list[str] = []

    steps = (
        ("pré-vol", _recorder(called, "pré-vol")),
        ("synchro", _recorder(called, "synchro", 23)),
        ("migration", _recorder(called, "migration")),
        ("vérification", _recorder(called, "vérification")),
    )
    assert run_steps(steps, _ctx()) == 23
    assert called == ["pré-vol", "synchro"]


def test_dry_run_executes_nothing_but_keeps_the_order():
    """La simulation annonce chaque étape sans en exécuter aucune."""
    called: list[str] = []
    steps = tuple((label, _recorder(called, label)) for label in "abc")

    assert run_steps(steps, _ctx(dry_run=True)) == 0
    assert called == []


def test_rsync_never_sends_the_local_env():
    """Un `.env` local écraserait la production : le filtre l'exclut toujours."""
    argv = rsync_argv(_ctx())

    assert "--exclude=.env" in argv
    # Le filtre documenté dans AGENTS.md reste la référence, pas une copie divergente.
    assert set(RSYNC_EXCLUDES) == {
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
        ".env",
        "vendor",
    }
    assert argv[0] == "rsync"
    assert argv[-1] == "ubuntu@example:~/bot-radar/"


def test_migration_waits_for_a_healthy_container():
    """La migration ne part que sur un conteneur sain : schéma et code du même build."""
    remote = _FakeRemote()
    assert step_migrate(_ctx(remote=remote)) == 0

    script = remote.scripts[0]
    assert f'-lt {HEALTH_ATTEMPTS}' in script
    assert "healthy" in script
    # L'ordre compte : on interroge la santé avant de toucher au schéma.
    assert script.index("healthy") < script.index("alembic upgrade head")
    assert "alembic current" in script


def test_migration_reports_a_container_that_never_becomes_healthy():
    """Un conteneur qui ne devient pas sain fait échouer l'étape, pas la migration."""
    remote = _FakeRemote(code=1)
    assert step_migrate(_ctx(remote=remote)) == 1


def test_rebuild_runs_on_the_remote_directory():
    """Le rebuild se fait dans la cible du VPS, pas dans le dépôt local."""
    remote = _FakeRemote()
    assert step_rebuild(_ctx(remote=remote)) == 0
    assert "cd ~/bot-radar" in remote.scripts[0]
    assert "docker compose up -d --build app" in remote.scripts[0]


@pytest.mark.parametrize("code", [0, 1, 255])
def test_remote_failure_is_propagated(code):
    """Une commande distante en échec arrête l'étape, quel que soit son code."""
    assert step_rebuild(_ctx(remote=_FakeRemote(code=code))) == code
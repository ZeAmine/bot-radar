"""La séquence de déploiement, éprouvée par son comportement, sans serveur.

Le 31 octobre 2026, la mise en production a dû être faite à la main alors que
GitHub portait déjà la refonte : le serveur exécutait la version de la veille.
Deux défauts mesurés ce jour-là sont ici tenus par des tests — la copie sans
suppression laissait traîner des fichiers retirés du dépôt, et rien ne rendait
visible l'écart entre le commit poussé et celui déployé.

Rien n'est simulé à la place du script : `deploy/deploy.sh` est exécuté pour de
vrai, et seuls ses deux transports sont remplacés par des faux placés sur le
`PATH`. Les assertions portent donc sur ce que la chaîne a réellement demandé au
serveur : quelle commande distante, dans quel ordre, avec quels arguments, et ce
qu'elle a écrit. Retirer une garde du script fait échouer ces tests.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "deploy.sh"

PREV_IMAGE = "sha256:" + "a" * 64
BUILT_IMAGE = "sha256:" + "b" * 64
POSTGRES_BEFORE = "radar_postgres|id-pg|2026-09-25T17:38:32Z|true"
REDIS_BEFORE = "radar_redis|id-redis|2026-09-25T17:38:32Z|true"
DEPS_SAME = [f"{POSTGRES_BEFORE}\n{REDIS_BEFORE}"]

FAKE_SSH = r'''#!/usr/bin/env python3
"""Faux `ssh` : répond selon un scénario et journalise chaque commande reçue."""
import json
import os
import re
import sys
from pathlib import Path

state = Path(os.environ["FAKE_DEPLOY_STATE"])
scenario = json.loads((state / "scenario.json").read_text())
command = sys.argv[-1]
with (state / "ssh.log").open("a") as log:
    log.write(command.replace("\n", " ") + "\n")


def position(name):
    path = state / ("counter-" + name)
    seen = int(path.read_text()) if path.exists() else 0
    path.write_text(str(seen + 1))
    return seen


def sequence(name, default):
    values = scenario.get(name, default)
    if not isinstance(values, list):
        return values
    return values[min(position(name), len(values) - 1)]


def answer(text):
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.exit(0)


deployed_path = state / "deployed_commit"

if command.strip() == "true":
    sys.exit(int(scenario.get("reachable", 0)))

if '%s|%s|%s|%s' in command and "docker inspect" in command:
    answer(sequence("dependents", []))

if ".deployed_commit" in command and ">" in command:
    written = re.search(r"printf '%s\\n' (\S+) >", command)
    value = written.group(1) if written else ""
    (state / "written_commit").write_text(value)
    deployed_path.write_text(value + "\n")
    sys.exit(0)

if "cat " in command and ".deployed_commit" in command:
    # Après l'écriture, le serveur relit ce qu'il vient d'écrire : c'est la preuve.
    if deployed_path.exists() and deployed_path.read_text().strip():
        answer(deployed_path.read_text())
    declared = scenario.get("deployed", "UNREADABLE")
    if declared == "UNREADABLE":
        sys.exit(1)
    answer(declared)

if "docker image inspect" in command:
    answer(scenario["built_image"] if "rollback" not in command else scenario["previous_image"])

if "docker tag" in command and "compose up" in command:
    (state / "rollback_switch").write_text("yes")
    sys.exit(int(scenario.get("rollback_exit", 0)))

if "docker tag" in command:
    sys.exit(int(scenario.get("tag_exit", 0)))

if ".State.Health.Status" in command:
    answer(sequence("health", ["healthy"]))

if ".State.Running" in command:
    answer(sequence("running", ["true"]))

if "docker inspect -f '{{.Image}}'" in command:
    answer(sequence("images", [scenario["previous_image"], scenario["built_image"]]))

if "docker compose build" in command:
    sys.exit(int(scenario.get("build_exit", 0)))

if "docker compose up" in command:
    sys.exit(int(scenario.get("up_exit", 0)))

if "notify_deploy" in command:
    sys.exit(int(scenario.get("notify_exit", 0)))

(state / "unhandled.log").open("a").write(command.replace("\n", " ") + "\n")
sys.exit(int(scenario.get("unhandled_exit", 0)))
'''

FAKE_RSYNC = r'''#!/usr/bin/env python3
"""Faux `rsync` : journalise ses arguments, ne copie rien."""
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["FAKE_DEPLOY_STATE"])
with (state / "rsync.log").open("a") as log:
    log.write("\t".join(sys.argv[1:]) + "\n")
scenario = json.loads((state / "scenario.json").read_text())
sys.exit(int(scenario.get("rsync_exit", 0)))
'''


def _fake_bin(tmp_path: Path) -> Path:
    """Les deux transports remplacés : rien d'autre n'est simulé."""
    directory = tmp_path / "bin"
    directory.mkdir()
    for name, body in (("ssh", FAKE_SSH), ("rsync", FAKE_RSYNC)):
        path = directory / name
        path.write_text(body)
        path.chmod(0o755)
    return directory


GIT_IDENTITY = ["-c", "user.name=test", "-c", "user.email=test@example.com"]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def worktree(tmp_path: Path) -> dict[str, object]:
    """Une copie locale du dépôt, deux commits, et le SHA de chacun."""
    repo = tmp_path / "depot"
    (repo / "app").mkdir(parents=True)
    (repo / "alembic" / "versions").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "app" / "main.py").write_text("print('v1')\n")
    versions = repo / "alembic" / "versions"
    (versions / "0014_alert_market_freshness.py").write_text("revision = '0014'\n")
    (repo / "scripts" / "notify_deploy.py").write_text("# present\n")
    (repo / "alembic.ini").write_text("[alembic]\n")
    (repo / "pyproject.toml").write_text("[project]\n")
    (repo / "requirements.lock").write_text("# pins\n")
    _git(repo, "init", "-q", "-b", "main")
    deployed = _commit(repo, "etat deploye")

    (repo / "app" / "main.py").write_text("print('v2')\n")
    target = _commit(repo, "changement a deployer")
    return {"repo": repo, "deployed": deployed, "target": target, "tmp": tmp_path}


def _state(worktree: dict[str, object], **scenario: object) -> Path:
    state = Path(str(worktree["tmp"])) / "state"
    state.mkdir(exist_ok=True)
    for name in ("ssh.log", "rsync.log", "unhandled.log", "written_commit"):
        (state / name).write_text("")
    # Le serveur ne déclare un commit déployé que si `deployed` le fournit : le
    # fichier n'existe qu'après une écriture de la chaîne.
    for stale in ("rollback_switch", "deployed_commit"):
        (state / stale).unlink(missing_ok=True)
    for stale in state.glob("counter-*"):
        stale.unlink()
    defaults: dict[str, object] = {
        "deployed": worktree["deployed"],
        "previous_image": PREV_IMAGE,
        "built_image": BUILT_IMAGE,
        "dependents": DEPS_SAME,
    }
    defaults.update(scenario)
    (state / "scenario.json").write_text(json.dumps(defaults))
    return state


def _run(
    worktree: dict[str, object],
    state: Path,
    *args: str,
    target: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Exécute le script pour de vrai, avec les faux transports en tête de PATH."""
    tmp = Path(str(worktree["tmp"]))
    key = tmp / "deploy_key"
    key.write_text("cle-factice\n")
    key.chmod(0o600)
    fake_bin = tmp / "bin" if (tmp / "bin").exists() else _fake_bin(tmp)
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp),
        "FAKE_DEPLOY_STATE": str(state),
        "DEPLOY_HOST": "vps.test",
        "DEPLOY_USER": "ubuntu",
        "DEPLOY_SSH_KEY": str(key),
        "DEPLOY_REMOTE_DIR": "/home/ubuntu/bot-radar",
        "DEPLOY_REPO": str(worktree["repo"]),
        "DEPLOY_SHA": target or str(worktree["target"]),
        "DEPLOY_HEALTH_TIMEOUT_SECONDS": "2",
        "DEPLOY_HEALTH_INTERVAL_SECONDS": "1",
        "DEPLOY_EVIDENCE": str(tmp / "evidence.env"),
    }
    if env is not None:
        for name, value in env.items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
        cwd=str(tmp),
    )


def _calls(state: Path, name: str = "ssh") -> list[str]:
    content = (state / f"{name}.log").read_text()
    return [line for line in content.splitlines() if line.strip()]


def _mutations(state: Path) -> list[str]:
    """Les commandes qui changent quelque chose sur le serveur."""
    markers = ("docker tag", "docker compose build", "docker compose up", "notify")
    writes = [call for call in _calls(state) if ".deployed_commit" in call and ">" in call]
    return [call for call in _calls(state) if any(marker in call for marker in markers)] + writes


def _written(state: Path) -> str:
    return (state / "written_commit").read_text().strip()


def test_nominal_deploy_copies_with_delete_builds_and_recreates_only_app(worktree) -> None:
    """Le parcours qui a marché le 31 octobre : copie avec suppression, image, app seule."""
    state = _state(worktree, health=["healthy"])
    result = _run(worktree, state)

    assert result.returncode == 0, result.stdout + result.stderr
    rsync_calls = _calls(state, "rsync")
    # Trois arbres copiés avec suppression : un fichier retiré du dépôt disparaît.
    assert sum("--delete" in call for call in rsync_calls) == 3
    # La configuration du serveur n'est jamais dans la copie.
    assert not any(".env" in call or "compose.yml" in call for call in rsync_calls)
    assert any("app/" in call for call in rsync_calls)
    assert any("alembic.ini" in call for call in rsync_calls)
    # La construction n'arrête rien et ne tire aucune image du réseau.
    assert any("docker compose build app" in call for call in _calls(state))
    recreations = [call for call in _calls(state) if "docker compose up" in call]
    assert len(recreations) == 1
    assert "--no-deps" in recreations[0]
    assert "--force-recreate" in recreations[0]
    assert recreations[0].rstrip().endswith("app")
    # Le SHA déployé devient lisible sur le serveur.
    assert _written(state) == worktree["target"]


def test_already_deployed_sha_does_not_rebuild_or_restart(worktree) -> None:
    """Idempotence : le SHA visé tourne déjà, rien n'est reconstruit ni redémarré."""
    state = _state(worktree, deployed=worktree["target"])
    result = _run(worktree, state)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _mutations(state) == []
    assert _calls(state, "rsync") == []


def test_unreadable_deployed_commit_refuses_before_any_change(worktree) -> None:
    """L'état de départ illisible n'est pas celui attendu : refus, rien de modifié."""
    state = _state(worktree, deployed="UNREADABLE")
    result = _run(worktree, state)

    assert result.returncode == 2, result.stdout + result.stderr
    assert _mutations(state) == []
    assert _calls(state, "rsync") == []
    assert ".deployed_commit" in result.stdout


def test_unreachable_server_changes_nothing(worktree) -> None:
    """Serveur injoignable : arrêt net, aucune action, aucun retour arrière."""
    state = _state(worktree, reachable=255)
    result = _run(worktree, state)

    assert result.returncode == 5, result.stdout + result.stderr
    assert _mutations(state) == []
    assert _calls(state, "rsync") == []


def test_rollback_when_the_app_does_not_start(worktree) -> None:
    """La seule condition autorisée : l'application ne démarre pas."""
    state = _state(worktree, health=["starting"], images=[PREV_IMAGE, PREV_IMAGE])
    result = _run(worktree, state)

    assert result.returncode == 3, result.stdout + result.stderr
    assert (state / "rollback_switch").exists()
    # Le retour arrière remet le SHA d'avant : le serveur ne ment pas sur son état.
    assert _written(state) == worktree["deployed"]


def test_no_rollback_when_the_app_is_healthy(worktree) -> None:
    """Un démarrage sain n'entraîne jamais de retour arrière."""
    state = _state(worktree, health=["starting", "healthy"])
    result = _run(worktree, state)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (state / "rollback_switch").exists()
    assert not any("rollback" in call and "compose up" in call for call in _calls(state))


def test_stuck_health_check_is_not_a_green_light(worktree) -> None:
    """Un contrôle qui reste bloqué n'est pas un feu vert : pas de succès."""
    state = _state(worktree, health=["starting"], images=[PREV_IMAGE, PREV_IMAGE])
    result = _run(worktree, state, env={"DEPLOY_HEALTH_TIMEOUT_SECONDS": "2"})

    assert result.returncode != 0
    assert "déploiement terminé" not in result.stdout


def test_changed_dependent_service_fails_closed(worktree) -> None:
    """Un service dépendant modifié fait échouer le passage, sans réparation."""
    changed = [
        DEPS_SAME[0],
        "radar_postgres|id-pg-bis|2026-10-31T00:00:00Z|true\n" + REDIS_BEFORE,
    ]
    state = _state(worktree, dependents=changed)
    result = _run(worktree, state)

    assert result.returncode == 4, result.stdout + result.stderr
    assert "radar_postgres" in result.stdout
    assert not (state / "rollback_switch").exists()
    assert not any("compose up" in call and "rollback" in call for call in _calls(state))


def test_unreadable_dependents_stop_before_any_copy(worktree) -> None:
    """Ne pas pouvoir regarder ne vaut pas un feu vert : refus avant toute mutation."""
    absent = ["radar_postgres|absente|absente|absente\nradar_redis|absente|absente|absente"]  # noqa: E501
    state = _state(worktree, dependents=absent)
    result = _run(worktree, state)

    assert result.returncode == 4, result.stdout + result.stderr
    assert _calls(state, "rsync") == []
    assert not any("compose build" in call for call in _calls(state))


def test_new_alembic_revision_is_refused_before_any_change(worktree) -> None:
    """La chaîne ne touche jamais la base : elle refuse un schéma qu'elle n'applique pas."""
    repo = Path(str(worktree["repo"]))
    versions = repo / "alembic" / "versions"
    (versions / "0015_chasse_weekly_prices.py").write_text("revision = '0015'\n")
    target = _commit(repo, "une revision de schema")
    state = _state(worktree, deployed=worktree["deployed"])
    result = _run(worktree, state, target=target)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "0015_chasse_weekly_prices" in result.stdout
    assert _mutations(state) == []
    assert _calls(state, "rsync") == []


def test_schema_change_allowed_only_by_explicit_acknowledgement(worktree) -> None:
    """Une migration déjà appliquée ailleurs s'acquitte par le SHA, pas par défaut."""
    repo = Path(str(worktree["repo"]))
    versions = repo / "alembic" / "versions"
    (versions / "0015_chasse_weekly_prices.py").write_text("revision = '0015'\n")
    target = _commit(repo, "une revision de schema")
    state = _state(worktree, health=["healthy"])
    result = _run(worktree, state, target=target, env={"DEPLOY_SCHEMA_ACK_TARGET": target})

    assert result.returncode == 0, result.stdout + result.stderr
    assert _written(state) == target


def test_missing_secret_is_named_and_nothing_is_attempted(worktree) -> None:
    """Un secret manquant est nommé précisément, avant tout contact avec le serveur."""
    state = _state(worktree)
    result = _run(worktree, state, env={"DEPLOY_HOST": None})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "DEPLOY_HOST" in result.stdout
    assert _calls(state) == []
    assert _calls(state, "rsync") == []


def test_check_mode_reports_the_gap_without_writing(worktree) -> None:
    """--check ne compare que le SHA visé et l'état du serveur."""
    behind = _state(worktree)
    result = _run(worktree, behind, "--check")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "EN RETARD" in result.stdout
    assert _mutations(behind) == []

    current = _state(worktree, deployed=worktree["target"])
    result = _run(worktree, current, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "à jour" in result.stdout
    assert _mutations(current) == []


def test_dry_run_writes_nothing(worktree) -> None:
    """--dry-run énonce ce qu'il ferait et n'écrit rien, ni localement ni à distance."""
    state = _state(worktree)
    result = _run(worktree, state, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _calls(state, "rsync") == []
    assert _mutations(state) == []
    assert _written(state) == ""


def test_the_chain_runs_no_migration_and_no_legacy_mode(worktree) -> None:
    """Ni migration de base, ni mode pilote : la chaîne reproduit ce qui a marché."""
    state = _state(worktree, health=["healthy"])
    result = _run(worktree, state)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = " ".join(_calls(state))
    assert "alembic" not in calls
    assert "pilot" not in calls.lower()
    assert (state / "unhandled.log").read_text() == ""


def test_evidence_names_the_deployed_sha_and_the_outcome(worktree) -> None:
    """La preuve du passage est écrite pour être lue sans se connecter."""
    state = _state(worktree, health=["healthy"])
    result = _run(worktree, state)

    assert result.returncode == 0, result.stdout + result.stderr
    evidence = Path(str(worktree["tmp"])) / "evidence.env"
    content = evidence.read_text()
    assert f"deployed_after_sha={worktree['target']}" in content
    assert "outcome=deployed" in content
    assert f"rollback_tag=bot-radar-app:rollback-{str(worktree['target'])[:7]}" in content

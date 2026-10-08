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
    clean_commit_message,
    format_deploy_notification,
    rsync_argv,
    run_steps,
    step_migrate,
    step_notify,
    step_rebuild,
    step_start,
)
from notify_deploy import send_telegram_notification  # noqa: E402


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
    """Le démarrage après migration attend toujours la santé de l'image cible."""
    remote = _FakeRemote()
    assert step_start(_ctx(remote=remote)) == 0

    script = remote.scripts[0]
    assert f'-lt {HEALTH_ATTEMPTS}' in script
    assert "healthy" in script
    assert script.index("docker compose up") < script.index("healthy")


def test_docker_exec_in_a_script_never_eats_the_rest_of_it():
    """Le one-shot ferme stdin pour préserver la suite du script SSH.

    Le script arrive sur l'entrée de `bash -s`. Sans la redirection, tout ce qui suit
    le premier appel Docker n'est jamais exécuté : la migration passait, sa
    ligne de confirmation disparaissait, et l'étape rendait un succès muet.
    """
    remote = _FakeRemote()
    assert step_migrate(_ctx(remote=remote)) == 0

    execs = [line for line in remote.scripts[0].splitlines() if "docker compose run" in line]
    assert len(execs) == 2
    for line in execs:
        assert "< /dev/null" in line, line


def test_migration_reports_a_container_that_never_becomes_healthy():
    """Un conteneur qui ne devient pas sain fait échouer l'étape, pas la migration."""
    remote = _FakeRemote(code=1)
    assert step_start(_ctx(remote=remote)) == 1


def test_rebuild_runs_on_the_remote_directory():
    """Le rebuild se fait dans la cible du VPS, pas dans le dépôt local."""
    remote = _FakeRemote()
    assert step_rebuild(_ctx(remote=remote)) == 0
    assert "cd ~/bot-radar" in remote.scripts[0]
    assert "docker compose build app" in remote.scripts[0]
    assert "docker compose up" not in remote.scripts[0]


def test_migration_uses_the_target_image_without_starting_the_app():
    remote = _FakeRemote()
    assert step_migrate(_ctx(remote=remote)) == 0
    script = remote.scripts[0]
    assert "docker compose stop app" in script
    assert "--entrypoint alembic app upgrade head" in script
    assert "--entrypoint alembic app current" in script
    assert "--no-deps" in script
    assert "--pull never" in script
    assert "docker compose exec" not in script
    assert "docker compose up" not in script
    assert script.index("docker compose stop app") < script.index("alembic app upgrade head")


@pytest.mark.parametrize("failure", ["", "build", "upgrade", "current"])
def test_procedure_shell_uses_built_image_and_stops_on_failure(tmp_path, failure):
    """Scripts shell réels avec Docker doublé, sans SSH ni conteneur réel."""
    import os
    import subprocess

    import deploy

    log = tmp_path / "commands"
    image = tmp_path / "image"
    image.write_text("old")
    docker = tmp_path / "docker"
    docker.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['COMMAND_LOG'], 'a') as f:
    f.write(' '.join(args) + '\\n')
image = pathlib.Path(os.environ['IMAGE_STATE'])
if args[:2] == ['compose', 'build']:
    if os.environ['FAILURE'] == 'build': sys.exit(31)
    image.write_text('target')
if args[:2] == ['compose', 'run']:
    assert image.read_text() == 'target', 'migration dans une ancienne image'
    assert '--entrypoint' in args and args[args.index('--entrypoint') + 1] == 'alembic'
    assert '--no-deps' in args and '--pull' in args and 'never' in args
    if 'upgrade' in args and os.environ['FAILURE'] == 'upgrade': sys.exit(32)
    if 'current' in args and os.environ['FAILURE'] == 'current': sys.exit(33)
    print('0010_alert_preparation (head)')
if args[:3] == ['compose', 'ps', '-q']: print('synthetic-cid')
if args[0] == 'inspect': print('healthy')
''')
    docker.chmod(0o755)
    env = dict(
        os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
        COMMAND_LOG=str(log), IMAGE_STATE=str(image), FAILURE=failure,
    )

    class ShellRemote:
        def run(self, script):
            result = subprocess.run(
                ["bash", "-s"], input=script, text=True, capture_output=True, env=env
            )
            return result.returncode, result.stdout, result.stderr

    trailing = []
    steps = tuple(
        (label, _recorder(trailing, label) if action in (
            deploy.step_preflight, deploy.step_sync, deploy.step_verify, deploy.step_notify
        ) else action)
        for label, action in deploy.STEPS
    )
    code = run_steps(steps, _ctx(remote=ShellRemote(), remote_dir=str(tmp_path)))
    commands = log.read_text().splitlines()
    assert commands[0] == "compose build app"
    if failure == "build":
        assert code == 31
        assert len(commands) == 1
    else:
        assert commands[1] == "compose stop app"
        assert commands[2].startswith("compose run ")
        assert commands[2].endswith("alembic app upgrade head")
        if failure:
            assert code == {"upgrade": 32, "current": 33}[failure]
            assert not any(c.startswith("compose up ") for c in commands)
        else:
            assert code == 0
            assert commands[3].endswith("alembic app current")
            assert commands[4] == "compose up -d --no-build --pull never app"
            assert trailing[-2:] == ["Vérification", "Notification"]
    if failure:
        assert trailing == ["Pré-vol", "Synchronisation"]


@pytest.mark.parametrize("code", [0, 1, 255])
def test_remote_failure_is_propagated(code):
    """Une commande distante en échec arrête l'étape, quel que soit son code."""
    assert step_rebuild(_ctx(remote=_FakeRemote(code=code))) == code


def test_clean_commit_message_strips_github_prefixes():
    """Vérifie le retrait des préfixes conventionnels et la capitalisation naturelle."""
    assert (
        clean_commit_message("feat(bot): interruption binaire stricte sur /add")
        == "Interruption binaire stricte sur /add"
    )
    assert (
        clean_commit_message("fix(ia): recherche des variantes stabilisée")
        == "Recherche des variantes stabilisée"
    )
    assert (
        clean_commit_message("refactor: clarification du pipeline")
        == "Clarification du pipeline"
    )
    # Message déjà naturel sans préfixe
    assert (
        clean_commit_message("Le backoff du provider rendu injectable")
        == "Le backoff du provider rendu injectable"
    )


def test_format_deploy_notification_structure():
    """La notification contient l'en-tête de mise en ligne et les puces nettoyées."""
    res = format_deploy_notification(
        [
            "feat(bot): détection de doublons",
            "fix(ia): correction du timeout",
        ]
    )
    assert "🚀 <b>Mise en ligne effectuée</b>" in res
    assert "• Détection de doublons" in res
    assert "• Correction du timeout" in res


def test_format_deploy_notification_fallback_empty():
    """Un ensemble vide de commits génère une puce de repli générique."""
    res = format_deploy_notification([])
    assert "🚀 <b>Mise en ligne effectuée</b>" in res
    assert "• Mise à jour des composants et améliorations mineures." in res


def test_step_notify_dry_run_executes_nothing():
    """En dry-run, aucune commande n'est envoyée au VPS."""
    remote = _FakeRemote()
    ctx = _ctx(dry_run=True, remote=remote)
    assert step_notify(ctx) == 0
    assert remote.scripts == []


def test_step_notify_executes_remote_and_saves_commit(monkeypatch):
    """Vérifie l'exécution distante de notify_deploy.py et l'enregistrement du commit."""
    remote = _FakeRemote()
    ctx = _ctx(remote=remote)

    monkeypatch.setattr(
        "deploy.get_deployed_commits",
        lambda _c: ("abcdef123456", ["feat(bot): nouvelle fonctionnalité"]),
    )

    assert step_notify(ctx) == 0
    assert len(remote.scripts) == 1
    script = remote.scripts[0]
    assert "docker compose exec -T app python scripts/notify_deploy.py --base64" in script
    assert "echo 'abcdef123456' > ~/bot-radar/.deployed_commit" in script


def test_step_notify_survives_remote_failure(monkeypatch):
    """Une erreur de la commande distante ne doit pas faire échouer le déploiement."""
    remote = _FakeRemote(code=1)
    ctx = _ctx(remote=remote)

    monkeypatch.setattr(
        "deploy.get_deployed_commits",
        lambda _c: ("abcdef123456", ["feat(bot): test"]),
    )

    # step_notify retourne 0 pour ne pas invalider un déploiement déjà sain
    assert step_notify(ctx) == 0


def test_send_telegram_notification_dry_run(monkeypatch):
    """En mode dry-run, send_telegram_notification affiche le message sans HTTP."""
    monkeypatch.setattr("app.core.config.settings.TELEGRAM_BOT_TOKEN", "123:dummy")
    monkeypatch.setattr("app.core.config.settings.TELEGRAM_ALLOWED_USER_IDS", [123456789])

    assert send_telegram_notification("Test message", dry_run=True) is True


def test_send_telegram_notification_missing_credentials(monkeypatch):
    """Sans token ou utilisateurs, la fonction retourne False proprement."""
    monkeypatch.setattr("app.core.config.settings.TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr("app.core.config.settings.TELEGRAM_ALLOWED_USER_IDS", [])

    assert send_telegram_notification("Test message") is False


def test_send_telegram_notification_http_post(monkeypatch):
    """Vérifie l'envoi HTTP effectif avec httpx mocké et token SecretStr."""
    from pydantic import SecretStr

    monkeypatch.setattr(
        "app.core.config.settings.TELEGRAM_BOT_TOKEN", SecretStr("123:dummy")
    )
    monkeypatch.setattr("app.core.config.settings.TELEGRAM_ALLOWED_USER_IDS", [123456789])

    posted_urls: list[str] = []
    posted_payloads: list[dict] = []

    class _MockResponse:
        status_code = 200
        text = "ok"

    class _MockClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json=None, **_kwargs):
            posted_urls.append(url)
            posted_payloads.append(json or {})
            return _MockResponse()

    monkeypatch.setattr("httpx.Client", lambda **_kwargs: _MockClient())

    assert send_telegram_notification("<b>Message</b>") is True
    assert len(posted_urls) == 1
    assert "https://api.telegram.org/bot123:dummy/sendMessage" in posted_urls[0]
    assert posted_payloads[0]["chat_id"] == 123456789
    assert posted_payloads[0]["text"] == "<b>Message</b>"
    assert posted_payloads[0]["parse_mode"] == "HTML"

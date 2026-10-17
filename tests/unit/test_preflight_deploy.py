"""Le pré-vol de déploiement, testé sans jamais toucher le réseau.

Le script interroge le VPS par SSH. Ce qui peut casser sans réseau — la construction
des scripts distants, la liste des fichiers comparés, la lecture du graphe Alembic —
est donc vérifié ici. Le contrôle qui a le plus coûté cher est le quoting : le
guillemet fermant d'un programme awk disparaissait parce qu'il servait de délimiteur
à la chaîne Python qui le construisait, et le script concluait « secrets illisibles »
sans jamais dire pourquoi.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import preflight_deploy as pf  # noqa: E402


def test_secrets_script_closes_its_awk_quoting():
    """Le guillemet fermant doit survivre : sinon bash voit un apostrophe orphelin."""
    script = pf.secrets_script("~/bot-radar")

    assert script.count("'") % 2 == 0, "apostrophes non appariées"
    assert "' .env" in script, "le programme awk n'est pas refermé avant .env"
    assert script.startswith("cd ~/bot-radar || exit 1\n")


def test_secrets_script_never_asks_for_a_value():
    """Le script distant rapporte OK ou VIDE, il ne peut pas faire fuiter un secret."""
    script = pf.secrets_script("~/bot-radar")

    assert "VIDE" in script
    assert "OK" in script
    missing = [name for name in pf.REQUIRED_SECRETS if name not in script]
    assert missing == [], f"secrets absents du contrôle : {missing}"
    # Aucun `echo $valeur` : seul le nom de la clé et un verdict sortent.
    assert "print $2" not in script


def test_synced_files_excludes_what_rsync_excludes():
    """Un fichier hors filtre ne doit jamais être comparé : le distant peut diverger."""
    files = pf.synced_files()

    # `app/main.py` est suivi par git : le test ne dépend pas d'un fichier récent.
    assert "app/main.py" in files
    assert ".env" not in files
    assert not any(path.startswith("vendor/") for path in files)
    assert not any(path.startswith(".venv/") for path in files)


def test_manifest_skips_unreadable_files():
    """Un fichier illisible ne fait pas échouer la comparaison entière."""
    content = pf.manifest(["README.md", "fichier/qui/nexiste/pas.md"])

    assert content.count("\n") == 1
    assert content.endswith("README.md\n")


def test_local_alembic_head_matches_the_repository():
    """La tête est déduite des fichiers, sans base : elle doit être la vraie."""
    head = pf.local_alembic_head()

    assert head == "0014_alert_market_freshness"
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    assert ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini"))).get_heads() == [head]

    # `sys.executable`, pas `.venv/bin/python` : `check-history` n'a pas de venv.
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        assert head in proc.stdout


def test_alembic_head_is_none_when_the_directory_is_absent(monkeypatch, tmp_path):
    """Sans dossier de versions, le contrôle saute au lieu d'inventer une tête."""
    monkeypatch.chdir(tmp_path)

    assert pf.local_alembic_head() is None


@pytest.mark.parametrize(
    "name",
    ["worker", "celery_worker", "rq", "beat", "app_scheduler"],
)
def test_forbidden_container_names_are_detected(name):
    """L'invariant n°2 ne tient que si un second ordonnanceur est détecté."""
    offenders = [
        found
        for found in [name]
        if any(word in found.lower() for word in pf.FORBIDDEN_CONTAINERS)
    ]

    assert offenders == [name]


def test_report_separates_failures_from_warnings():
    """Un avertissement ne bloque pas le déploiement, un échec si."""
    report = pf.Report()
    report.ok("conteneur", "healthy")
    report.warn("code déployé", "3 fichiers à synchroniser")

    assert report.failures == []
    assert len(report.warnings) == 1

    report.fail("APP_ENV", "development")
    assert len(report.failures) == 1


class _FakeRemote:
    """Un VPS simulé : seule la lecture de l'état Alembic est stubée."""

    def __init__(self, current: str | None, heads: list[str], code: int = 0) -> None:
        self._state = (current, heads, code)
        self.scripts: list[str] = []

    def run(self, script: str):
        self.scripts.append(script)
        current, heads, code = self._state
        if code != 0:
            return code, "", "conteneur injoignable"
        out = (
            "__CURRENT__\n"
            + (f"{current} (head)\n" if current else "\n")
            + "__HEADS__\n"
            + "".join(f"{head} (head)\n" for head in heads)
        )
        return 0, out, ""


def test_migration_state_asks_the_container_not_the_working_directory():
    """Le graphe pertinent est celui du code qui tourne, pas celui du dossier."""
    script = pf.migration_state_script("~/bot-radar")

    assert "docker compose exec -T app alembic current" in script
    assert "docker compose exec -T app alembic heads" in script
    # Le dossier de travail ne ferait autorité que pour du code pas encore déployé.
    assert "alembic heads" not in script.replace(
        "docker compose exec -T app alembic heads", ""
    )
    # Sans cette redirection, `docker compose exec` avale la seconde lecture.
    execs = [line for line in script.splitlines() if "docker compose exec" in line]
    assert len(execs) == 2
    for line in execs:
        assert "< /dev/null" in line, line


def test_parse_migration_state_separates_current_from_heads():
    """Deux marqueurs, deux lectures : les confondre donnerait un faux verdict."""
    out = (
        "__CURRENT__\n"
        "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.\n"
        "0005_correct_historical_low (head)\n"
        "__HEADS__\n"
        "0006_prix (head)\n"
    )

    current, heads = pf.parse_migration_state(out)

    assert current == "0005_correct_historical_low"
    assert heads == ["0006_prix"]


def test_pending_migration_does_not_block_the_preflight():
    """Une migration à jouer est l'attendu d'un déploiement, pas une dérive.

    Régression : le pré-vol comparait la base à la tête locale, donc aucune
    migration ne pouvait jamais être déployée — le rebuild précède l'`upgrade head`.
    """
    remote = _FakeRemote(
        current="0004_alert_books_book_id_index",
        heads=["0004_alert_books_book_id_index"],
    )
    report = pf.Report()

    pf.check_remote_migration(report, remote, "~/bot-radar", "0005_correct_historical_low", False)

    assert report.failures == []


def test_schema_ahead_of_deployed_code_is_fatal():
    """Une base en avance sur le code qui la sert est la dérive qui casse l'app."""
    remote = _FakeRemote(current="0006_prix", heads=["0005_correct_historical_low"])
    report = pf.Report()

    pf.check_remote_migration(report, remote, "~/bot-radar", "0006_prix", False)

    assert len(report.failures) == 1
    assert "divergent" in report.failures[0]


def test_several_heads_in_deployed_code_is_fatal():
    """Deux têtes en production, c'est un graphe ambigu : rien ne dit quoi jouer."""
    remote = _FakeRemote(current="0005_correct_historical_low", heads=["0005_a", "0005_b"])
    report = pf.Report()

    pf.check_remote_migration(report, remote, "~/bot-radar", "0005_a", False)

    assert len(report.failures) == 1
    assert "têtes" in report.failures[0]


def test_after_sync_the_base_must_carry_the_local_head():
    """Après le rebuild, l'écart est redevenu fatal : la migration a dû être jouée."""
    remote = _FakeRemote(
        current="0004_alert_books_book_id_index",
        heads=["0005_correct_historical_low"],
    )
    report = pf.Report()

    pf.check_remote_migration(
        report, remote, "~/bot-radar", "0005_correct_historical_low", True
    )

    assert len(report.failures) == 1
    assert "attendu 0005_correct_historical_low" in report.failures[0]


def test_unreadable_migration_state_is_never_a_pass():
    """Un conteneur muet ne doit pas passer pour un accord."""
    remote = _FakeRemote(current=None, heads=[], code=1)
    report = pf.Report()

    pf.check_remote_migration(report, remote, "~/bot-radar", "0005_correct_historical_low", False)

    assert len(report.failures) == 1


@pytest.mark.parametrize("runtime", ["target", "stale", "unavailable"])
def test_post_sync_checks_running_code_even_when_server_files_match(
    monkeypatch, tmp_path, runtime
):
    """Un rsync correct ne suffit pas si l'ancien conteneur sert encore son code."""
    import os

    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("target")
    (tmp_path / "compose.yml").write_text("synthetic compose")
    image = tmp_path / "runtime"
    (image / "app").mkdir(parents=True)
    if runtime != "unavailable":
        (image / "app/main.py").write_text("target" if runtime == "target" else "stale")
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, pathlib, sys\n"
        "assert sys.argv[1:] == ['exec', '-i', '-w', '/app', 'radar_app', "
        "'sha256sum', '-c', '--quiet', '-']\n"
        f"root = pathlib.Path({str(image)!r})\n"
        "failed = False\n"
        "for line in sys.stdin:\n"
        "    expected, name = line.rstrip('\\n').split('  ', 1)\n"
        "    assert name == 'app/main.py', 'un fichier hors image est vérifié'\n"
        "    path = root / name\n"
        "    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:\n"
        "        failed = True\n"
        "        print(name + ': FAILED')\n"
        "sys.exit(1 if failed else 0)\n"
    )
    docker.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pf, "synced_files", lambda: ["app/main.py", "compose.yml"])

    class RemoteWithMatchingDisk:
        def __init__(self):
            self.scripts = []

        def run(self, script):
            self.scripts.append(script)
            if "docker exec" not in script:
                return 0, "", ""  # Les fichiers rsync correspondent déjà au manifeste.
            result = subprocess.run(
                ["/bin/bash"],
                input=script,
                text=True,
                capture_output=True,
                env={"PATH": str(tmp_path) + os.pathsep + os.defpath},
            )
            return result.returncode, result.stdout, result.stderr

    remote = RemoteWithMatchingDisk()
    report = pf.Report()
    pf.check_remote_code(report, remote, str(tmp_path), post_sync=True)

    assert len(remote.scripts) == 2
    assert bool(report.failures) is (runtime != "target")
    assert report.warnings == []


def test_before_sync_keeps_running_image_differences_expected(monkeypatch, tmp_path):
    """Avant construction, la vérification de l'image cible n'est pas encore exigée."""
    (tmp_path / "main.py").write_text("target")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pf, "synced_files", lambda: ["main.py"])

    class RemoteWithOldDisk:
        def run(self, script):
            assert "docker exec" not in script
            return 1, "main.py: FAILED\n", ""

    report = pf.Report()
    pf.check_remote_code(report, RemoteWithOldDisk(), str(tmp_path), post_sync=False)
    assert report.failures == []
    assert len(report.warnings) == 1


def test_preflight_rejects_invalid_target_compose_without_echoing_its_output(
    monkeypatch, capsys
):
    """La cible non configurable bloque le pré-vol avant toute étape destructive."""
    scripts = []

    class InvalidTarget:
        def __init__(self, *_):
            pass

        def reachable(self):
            return True

        def run(self, script):
            scripts.append(script)
            return 1, "synthetic-sensitive-output", "synthetic-sensitive-error"

    monkeypatch.setattr(sys, "argv", ["preflight_deploy.py"])
    monkeypatch.setattr(pf, "Remote", InvalidTarget)
    monkeypatch.setattr(pf, "check_local", lambda _: "synthetic-head")
    monkeypatch.setattr(pf, "check_remote_config", lambda *_: None)
    monkeypatch.setattr(pf, "check_remote_code", lambda *_: None)
    monkeypatch.setattr(pf, "check_remote_migration", lambda *_: None)

    assert pf.main() == 1
    assert len(scripts) == 1
    assert "config --no-env-resolution --quiet" in scripts[0]
    assert "POSTGRES_PASSWORD" in scripts[0]  # Modèle cible envoyé, pas celui du serveur.
    assert "synthetic-sensitive" not in capsys.readouterr().out

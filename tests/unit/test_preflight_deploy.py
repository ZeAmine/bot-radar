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

    # `app/main.py` est suivi par git depuis le début du dépôt : contrairement à un
    # fichier nouvellement créé, le test ne dépend pas de l'état du commit.
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

    assert head == "0005_correct_historical_low_from_observations"

    # `sys.executable`, pas `.venv/bin/python` : ce test tourne aussi dans le
    # worktree jetable de `check-history`, qui n'a pas de venv.
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

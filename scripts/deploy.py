#!/usr/bin/env python3
"""Déploie : pré-vol, arrêt confirmé, sync, build, migration, démarrage, vérification.

La procédure était une liste de commandes à recopier dans le bon ordre, et c'est
exactement le genre de liste qu'on exécute à moitié. Trois accidents y sont invisibles
depuis le dépôt :

- un `rsync` oublié — le push est vert, le VPS sert encore le code précédent ;
- une migration dans le conteneur précédent — elle vise l'ancien graphe ;
- un démarrage cible avant migration — la reprise lit des colonnes encore absentes.

Un script n'a pas d'oubli : chaque étape attend l'échec de la précédente, et la
première qui échoue arrête tout sans lancer la suite. L'ordre est donc une donnée,
pas une mémoire, et `--dry-run` permet de le relire avant de l'exécuter.

Usage :
    python scripts/deploy.py                    # déploiement complet
    python scripts/deploy.py --dry-run          # affiche les étapes, n'exécute rien
    python scripts/deploy.py --host user@hote --key ~/.ssh/id_rsa
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from preflight_deploy import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_KEY,
    DEFAULT_REMOTE_DIR,
    Remote,
)

GREEN, RED, YELLOW, BOLD, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m",
)

# Le même filtre que celui documenté dans AGENTS.md. `.env` en fait partie : un
# `.env` local présent sur cette machine écraserait la production, qui a été
# reconfigurée à la main lors du passage en production.
RSYNC_EXCLUDES = (
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    ".env",
    "vendor",
)

# Attente de la santé du conteneur cible après migration, en tentatives de deux secondes.
HEALTH_ATTEMPTS = 30


@dataclass
class Context:
    """Tout ce dont une étape a besoin pour travailler, sans état global."""

    host: str
    key: str
    remote_dir: str
    repo_root: Path
    dry_run: bool = False
    remote: Remote | None = None

    def remote_or_default(self) -> Remote:
        if self.remote is None:
            self.remote = Remote(self.host, self.key)
        return self.remote


def rsync_argv(ctx: Context) -> list[str]:
    """Commande de synchronisation : le dépôt local vers la cible du VPS."""
    return [
        "rsync",
        "-avz",
        *[f"--exclude={name}" for name in RSYNC_EXCLUDES],
        "-e",
        f"ssh -i {ctx.key} -o StrictHostKeyChecking=no",
        "./",
        f"{ctx.host}:{ctx.remote_dir}/",
    ]


def repo_root() -> Path:
    """Racine du dépôt, déduite de l'emplacement du script."""
    return Path(__file__).resolve().parent.parent


def _run_local(argv: list[str]) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=str(repo_root()), capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# --------------------------------------------------------------------------- étapes


def step_preflight(ctx: Context) -> int:
    """Contrôle l'état du VPS et l'écart de code avant de toucher quoi que ce soit."""
    print(f"{DIM}interroge le VPS, vérifie APP_ENV, les secrets et la migration en tête{RESET}")
    argv = [
        sys.executable,
        str(ctx.repo_root / "scripts" / "preflight_deploy.py"),
        "--host",
        ctx.host,
        "--key",
        ctx.key,
    ]
    return subprocess.run(argv).returncode


def step_sync(ctx: Context) -> int:
    """Envoie le code. Un `.env` local est signalé, jamais transmis."""
    local_env = (ctx.repo_root / ".env").is_file()
    if local_env:
        print(
            f"{YELLOW}ATTENTION{RESET} un {DIM}.env{RESET} local existe : "
            f"il est {DIM}exclu{RESET} du rsync et ne sera pas transmis."
        )

    code, output = _run_local(rsync_argv(ctx))
    if output:
        print(f"{DIM}{output}{RESET}")
    return code


def step_stop(ctx: Context) -> int:
    """Arrête et confirme l'arrêt de l'app avant la synchronisation de la cible."""
    script = f"""set -eu
cd {ctx.remote_dir}
cid=$(docker compose ps -a -q app)
[ -n "$cid" ] || {{ echo "conteneur app introuvable : arrêt non confirmé"; exit 1; }}
docker compose stop app
running=$(docker inspect --format '{{{{.State.Running}}}}' "$cid")
[ "$running" = false ] || {{ echo "app encore active : synchronisation interdite"; exit 1; }}
echo "arrêt app confirmé avant synchronisation"
"""
    code, out, err = ctx.remote_or_default().run(script)
    print(f"{DIM}{out.strip()}{RESET}")
    if err.strip():
        print(f"{DIM}{err.strip()}{RESET}")
    return code


def step_rebuild(ctx: Context) -> int:
    """Construit l'image cible sans démarrer l'app ; son graphe Alembic y est inclus."""
    script = (
        "set -eu\n"
        f"cd {ctx.remote_dir}\n"
        "docker compose build app\n"
    )
    code, out, err = ctx.remote_or_default().run(script)
    print(f"{DIM}{out.strip()}{RESET}")
    if err.strip():
        print(f"{DIM}{err.strip()}{RESET}")
    return code


def step_migrate(ctx: Context) -> int:
    """Migre depuis l'image construite, pendant que l'app reste arrêtée.

    Compose run utilise l'image du service, pas celle du conteneur ancien. Sans
    build ni pull ici, le graphe est celui de la cible. L'entrypoint Alembic ne
    démarre ni app ni scheduler ; les dépendances restent en service. Les appels
    ferment stdin pour ne pas consommer la suite du script SSH.
    """
    script = f"""set -eu
cd {ctx.remote_dir}
docker compose run --rm --no-deps --pull never -T --entrypoint alembic app upgrade head < /dev/null
docker compose run --rm --no-deps --pull never -T --entrypoint alembic app current < /dev/null
"""
    code, out, err = ctx.remote_or_default().run(script)
    print(f"{DIM}{out.strip()}{RESET}")
    if err.strip():
        print(f"{DIM}{err.strip()}{RESET}")
    return code


def step_start(ctx: Context) -> int:
    """Démarre seulement après migration réussie et attend la santé réelle."""
    script = f"""set -eu
cd {ctx.remote_dir}
docker compose up -d --no-build --pull never app
cid=$(docker compose ps -q app)
[ -n "$cid" ] || {{ echo "conteneur app introuvable"; exit 1; }}
state=unknown
i=0
while [ "$i" -lt {HEALTH_ATTEMPTS} ]; do
  state=$(docker inspect --format '{{{{.State.Health.Status}}}}' "$cid" \\
    2>/dev/null || echo starting)
  [ "$state" = "healthy" ] && break
  i=$((i + 1))
  sleep 2
done
if [ "$state" != "healthy" ]; then
  echo "conteneur app non sain après {HEALTH_ATTEMPTS} tentatives : $state"
  exit 1
fi
echo "conteneur cible sain après migration"
"""
    code, out, err = ctx.remote_or_default().run(script)
    print(f"{DIM}{out.strip()}{RESET}")
    if err.strip():
        print(f"{DIM}{err.strip()}{RESET}")
    return code


def step_verify(ctx: Context) -> int:
    """Rejoue le pré-vol en mode strict : là, un écart de code est fatal."""
    print(f"{DIM}revérifie le VPS ; tout écart de code est désormais fatal{RESET}")
    argv = [
        sys.executable,
        str(ctx.repo_root / "scripts" / "preflight_deploy.py"),
        "--post-sync",
        "--host",
        ctx.host,
        "--key",
        ctx.key,
    ]
    return subprocess.run(argv).returncode


def clean_commit_message(raw_msg: str) -> str:
    """Nettoie un message de commit pour en faire une phrase naturelle sans préfixe GitHub.

    Exemples :
        "feat(bot): interruption binaire stricte sur /add dès détection d'un doublon"
        -> "Interruption binaire stricte sur /add dès détection d'un doublon"
        "fix(scripts): le corps du PUT prend la forme que l'API accepte"
        -> "Le corps du PUT prend la forme que l'API accepte"
    """
    cleaned = raw_msg.strip()
    cleaned = re.sub(
        r"^(?:feat|fix|refactor|docs|perf|test|chore|style|ci|build)(?:\([^)]+\))?:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.strip()
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def format_deploy_notification(commit_messages: list[str]) -> str:
    """Formate la notification Telegram dans le style naturel des alertes du bot."""
    cleaned = [clean_commit_message(msg) for msg in commit_messages if msg.strip()]
    if not cleaned:
        cleaned = ["Mise à jour des composants et améliorations mineures."]

    bullets = "\n".join(f"• {msg}" for msg in cleaned)
    return f"🚀 <b>Mise en ligne effectuée</b>\n\n{bullets}"


def get_deployed_commits(ctx: Context) -> tuple[str, list[str]]:
    """Récupère le commit HEAD courant et la liste des commits déployés depuis le précédent."""
    code, current_head = _run_local(["git", "rev-parse", "HEAD"])
    if code != 0 or not current_head:
        return "", []

    prev_commit = ""
    remote = (
        None
        if ctx.dry_run
        else (ctx.remote if ctx.remote is not None else ctx.remote_or_default())
    )
    if remote is not None:
        code_prev, out_prev, _ = remote.run(
            f"cat {ctx.remote_dir}/.deployed_commit 2>/dev/null || true"
        )
        if code_prev == 0 and out_prev.strip():
            prev_commit = out_prev.strip()

    commits: list[str] = []
    if prev_commit:
        code_anc, _ = _run_local(["git", "cat-file", "-e", prev_commit])
        if code_anc == 0:
            code_log, out_log = _run_local(
                ["git", "log", f"{prev_commit}..HEAD", "--pretty=format:%s"]
            )
            if code_log == 0 and out_log.strip():
                commits = [line.strip() for line in out_log.splitlines() if line.strip()]

    if not commits:
        code_log, out_log = _run_local(["git", "log", "-1", "--pretty=format:%s"])
        if code_log == 0 and out_log.strip():
            commits = [out_log.strip()]

    return current_head, commits


def step_notify(ctx: Context) -> int:
    """Envoie la notification de mise en ligne sur Telegram et met à jour le commit de référence."""
    print(f"{DIM}envoie la notification de mise en ligne sur Telegram{RESET}")
    current_head, commits = get_deployed_commits(ctx)
    message = format_deploy_notification(commits)

    if ctx.dry_run:
        print(f"{DIM}  Message Telegram (simulation) :\n{message}{RESET}")
        return 0

    b64_msg = base64.b64encode(message.encode("utf-8")).decode("ascii")
    script = (
        f"set -eu\n"
        f"cd {ctx.remote_dir}\n"
        f"docker compose exec -T app python scripts/notify_deploy.py --base64 '{b64_msg}'"
        " < /dev/null || true\n"
    )
    if current_head:
        script += f"echo '{current_head}' > {ctx.remote_dir}/.deployed_commit\n"

    code, out, err = ctx.remote_or_default().run(script)
    if out.strip():
        print(f"{DIM}{out.strip()}{RESET}")
    if err.strip():
        print(f"{YELLOW}Avertissement notification : {err.strip()}{RESET}")

    return 0


STEPS: tuple[tuple[str, Callable[[Context], int]], ...] = (
    ("Pré-vol", step_preflight),
    ("Arrêt confirmé de l'app", step_stop),
    ("Synchronisation", step_sync),
    ("Reconstruction de l'image", step_rebuild),
    ("Migration", step_migrate),
    ("Démarrage et santé", step_start),
    ("Vérification", step_verify),
    ("Notification", step_notify),
)


def run_steps(steps: tuple[tuple[str, Callable[[Context], int]], ...], ctx: Context) -> int:
    """Exécute les étapes dans l'ordre et s'arrête à la première qui échoue.

    Le code de retour de l'étape fautive est rendu tel quel : un `rsync` interrompu
    et un pré-vol en échec n'appellent pas la même correction, et l'appelant doit
    pouvoir les distinguer.
    """
    for index, (label, action) in enumerate(steps, start=1):
        print(f"\n{BOLD}[{index}/{len(steps)}] {label}{RESET}")
        if ctx.dry_run:
            print(f"{DIM}  (simulation — non exécuté){RESET}")
            continue
        code = action(ctx)
        if code != 0:
            print(
                f"\n{RED}Déploiement arrêté à l'étape {index} ({label}), "
                f"code {code}. Les étapes suivantes n'ont pas été tentées.{RESET}"
            )
            return code
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("DEPLOY_HOST", DEFAULT_HOST))
    parser.add_argument("--key", default=os.environ.get("DEPLOY_KEY", DEFAULT_KEY))
    parser.add_argument(
        "--remote-dir", default=os.environ.get("DEPLOY_REMOTE_DIR", DEFAULT_REMOTE_DIR)
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="affiche l'ordre des étapes sans rien exécuter",
    )
    args = parser.parse_args()

    ctx = Context(
        host=args.host,
        key=args.key,
        remote_dir=args.remote_dir,
        repo_root=repo_root(),
        dry_run=args.dry_run,
    )

    print(f"{BOLD}Déploiement de BotRadar{RESET}  {DIM}→ {ctx.host}:{ctx.remote_dir}{RESET}")
    if ctx.dry_run:
        print(f"{YELLOW}Simulation{RESET} {DIM}: aucune commande ne sera exécutée.{RESET}")

    code = run_steps(STEPS, ctx)
    if code != 0:
        return code
    if ctx.dry_run:
        print(f"\n{GREEN}Ordre des étapes vérifié, rien n'a été exécuté.{RESET}")
        return 0
    print(f"\n{GREEN}Déploiement terminé et vérifié.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Déploie le bot sur le VPS : pré-vol, synchronisation, rebuild, migration, vérification.

La procédure était une liste de commandes à recopier dans le bon ordre, et c'est
exactement le genre de liste qu'on exécute à moitié. Deux accidents y sont invisibles
depuis le dépôt :

- un `rsync` oublié — le push est vert, le VPS sert encore le code précédent ;
- une migration passée avant que le conteneur ne soit sain — elle vise alors
  l'image d'avant, et le schéma se retrouve en retard d'une version sur le code.

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
import os
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

# Attente de la santé du conteneur avant migration, en tentatives d'une seconde.
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


def step_rebuild(ctx: Context) -> int:
    """Reconstruit l'image de l'app : obligatoire, `alembic/` n'est pas monté en volume."""
    script = (
        "set -eu\n"
        f"cd {ctx.remote_dir}\n"
        "docker compose up -d --build app\n"
    )
    code, out, err = ctx.remote_or_default().run(script)
    print(f"{DIM}{out.strip()}{RESET}")
    if err.strip():
        print(f"{DIM}{err.strip()}{RESET}")
    return code


def step_migrate(ctx: Context) -> int:
    """Applique la migration, mais seulement une fois le conteneur sain.

    Migrer contre un conteneur qui redémarre encore viserait l'image précédente :
    le schéma resterait en retard d'une version sur le code, sans que rien ne le
    signale. Le schéma doit suivre le code : c'est une condition, pas une étape.
    """
    script = f"""set -eu
cd {ctx.remote_dir}
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
echo "conteneur sain, migration..."
docker compose exec -T app alembic upgrade head
docker compose exec -T app alembic current
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


STEPS: tuple[tuple[str, Callable[[Context], int]], ...] = (
    ("Pré-vol", step_preflight),
    ("Synchronisation", step_sync),
    ("Reconstruction de l'image", step_rebuild),
    ("Migration", step_migrate),
    ("Vérification", step_verify),
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
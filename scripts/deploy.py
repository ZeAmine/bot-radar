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

Un quatrième accident s'y est ajouté, invisible depuis le dépôt lui-même : le
déploiement du 16 octobre 2026 a **recréé `radar_postgres`** alors qu'il ne
visait que `app`. Compose réconcilie les dépendances d'un service avant de le
démarrer, et l'empreinte de configuration de `postgres` avait changé — la
commande de son `healthcheck`. Le fait est établi : `docker compose config
--hash postgres` sur la définition déployée rend l'empreinte exacte portée par
le conteneur en service. Le démarrage porte donc `--no-deps`, et l'invariance
des dépendances est **contrôlée** avant et après, jamais supposée : `--dry-run`
ne pouvait pas la montrer, et une simulation n'est pas une garantie.

Usage :
    python scripts/deploy.py                    # déploiement complet
    python scripts/deploy.py --dry-run          # affiche les étapes, n'exécute rien
    python scripts/deploy.py --host user@hote --key ~/.ssh/id_rsa
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from deploy_dependencies import (  # noqa: E402
    DEPENDENT_SERVICES,
    DependencyError,
    InvarianceVerdict,
    assert_operational,
    capture_script,
    compare,
    decode_capture,
)
from preflight_deploy import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_KEY,
    DEFAULT_REMOTE_DIR,
    Remote,
)

GREEN, RED, YELLOW, BOLD, DIM, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[1m",
    "\033[2m",
    "\033[0m",
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

# Le démarrage applicatif ne doit jamais réconcilier les dépendances. Compose
# recrée un service dont l'empreinte de configuration a changé, **y compris une
# dépendance qu'on ne nomme pas** : c'est ce qui a recréé `radar_postgres` lors du
# déploiement du 16 octobre 2026, après un changement de la commande de son
# `healthcheck`. `--no-deps` borne le démarrage au seul service nommé.
# `--no-build` et `--pull never` garantissent par ailleurs que l'image servie est
# celle qui vient d'être construite et vérifiée, et non une image tirée du réseau.
APP_ONLY_UP = "docker compose up -d --no-build --pull never --no-deps app"

# Préfixe du tag de retour créé à chaque déploiement. Le build réécrit `latest`
# en place : c'est la copie faite avant lui qui rend l'image d'avant atteignable.
# Un horodatage rend le tag unique, donc deux déploiements ne se recouvrent pas.
ROLLBACK_TAG_PREFIX = "bot-radar-app:rollback-"


@dataclass
class Context:
    """Tout ce dont une étape a besoin pour travailler, sans état global."""

    host: str
    key: str
    remote_dir: str
    repo_root: Path
    dry_run: bool = False
    remote: Remote | None = None
    # Identité des dépendances relevée avant l'arrêt de l'app, relue après son
    # démarrage. Transiter par le contexte plutôt que par un fichier temporaire
    # garde la comparaison dans le même processus, donc dans le même déploiement.
    dependencies_before: dict[str, object] | None = None
    # Tag de retour créé avant le build, revérifié après. Conservé pour que le
    # bilan du déploiement nomme la référence de retour réellement disponible
    # plutôt qu'un nom supposé.
    rollback_image: str = ""
    pilot: bool = False
    pilot_manifest: Path | None = None
    pilot_digest: str = ""

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
    if ctx.pilot:
        try:
            if ctx.pilot_manifest is None:
                return 1
            manifest = json.loads(ctx.pilot_manifest.read_text())
            from app.utils.pilot_manifest import validate_manifest

            validate_manifest(manifest, ctx.pilot_digest)
        except (OSError, ValueError, TypeError, KeyError):
            print("Préconditions privées du pilote absentes ou invalides")
            return 1
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


def step_pilot_activate(ctx: Context) -> int:
    if not ctx.pilot:
        return 0
    if ctx.pilot_manifest is None:
        return 1
    state = "/var/lib/botradar/chasse-pilot/state.json"
    data = base64.b64encode(ctx.pilot_manifest.read_bytes()).decode("ascii")
    script = (
        f"set -eu\ncd {ctx.remote_dir}\n"
        "docker compose run --rm -T --no-deps --pull never --entrypoint python app -c "
        + shlex.quote(
            "import base64,json; from pathlib import Path; "
            "from app.core.config import settings; from app.core import pilot_mode; "
            f"assert settings.CHASSE_PILOT_STATE_PATH == {state!r}; "
            f"pilot_mode.enable(Path({state!r}),json.loads(base64.b64decode({data!r})),"
            f"{ctx.pilot_digest!r}); print('Pilote persistant vérifié')"
        ) + "\n"
    )
    code, _, _ = ctx.remote_or_default().run(script)
    return int(code)


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
    return int(code)


def step_stop(ctx: Context) -> int:
    """Relève l'identité des dépendances, puis arrête et confirme l'arrêt de l'app.

    La capture précède l'arrêt : après, les valeurs ne décriraient plus l'état
    d'avant le déploiement, et la comparaison perdrait son sens. Les dépendances
    sont aussi **vérifiées opérationnelles ici**, avant toute interruption :
    arrêter l'app alors qu'une dépendance manque ne ferait que rendre le service
    indisponible sans rien permettre de réparer.
    """
    capture = capture_script(ctx.remote_dir)

    # 1. Capture avant toute action, et contrôle que les dépendances sont là.
    code, out, err = ctx.remote_or_default().run(capture)
    if code != 0:
        print(f"{RED}Capture des dépendances impossible : {err.strip()}{RESET}")
        return code or 1
    try:
        identites = decode_capture(out)
    except DependencyError as exc:
        print(f"{RED}{exc}{RESET}")
        return 1
    ctx.dependencies_before = dict(identites)

    problemes = assert_operational(identites, DEPENDENT_SERVICES)
    if problemes:
        for probleme in problemes:
            print(f"{RED}dépendance non opérationnelle : {probleme}{RESET}")
        print(f"{RED}App non arrêtée : rien ne serait servi pendant l'interruption.{RESET}")
        return 1
    for service in DEPENDENT_SERVICES:
        identite = identites.get(service)
        if identite is not None:
            print(
                f"{DIM}{service} : {identite.container_id[:12]} "
                f"démarré {identite.started_at}{RESET}"
            )

    # 2. Conservation de l'image de retour, avant que le build n'écrase le tag
    #    applicatif. `docker compose build app` réécrit `bot-radar-app:latest` en
    #    place : sans cette copie, l'image réellement en service devient
    #    inaccessible et le retour arrière n'a plus de référence.
    script = f"""set -eu
cd {ctx.remote_dir}
cid=$(docker compose ps -a -q app)
[ -n "$cid" ] || {{ echo "conteneur app introuvable : arrêt non confirmé"; exit 1; }}
image_id=$(docker inspect --format '{{{{.Image}}}}' "$cid")
[ -n "$image_id" ] || {{ echo "identifiant d'image introuvable"; exit 1; }}
tag="{ROLLBACK_TAG_PREFIX}$(date +%Y%m%d-%H%M%S)"
docker tag "$image_id" "$tag"
# Le tag doit pointer sur l'image capturée : un tag créé ne vaut que vérifié,
# sinon le retour arrière restaurerait autre chose que ce qui tournait.
tagged=$(docker inspect --format '{{{{.Id}}}}' "$tag")
[ "$tagged" = "$image_id" ] || {{ echo "tag de retour incohérent : $tag"; exit 1; }}
echo "image de retour conservée : $tag ($image_id)"
# Le tag seul, sur sa propre ligne : il ne contient ni espace ni parenthèse, et
# l'appelant le relit sans avoir à découper un message d'affichage.
echo "RADAR_ROLLBACK_TAG=$tag"
echo "$tag" > .rollback-image
docker compose stop app
running=$(docker inspect --format '{{{{.State.Running}}}}' "$cid")
[ "$running" = false ] || {{ echo "app encore active : synchronisation interdite"; exit 1; }}
echo "arrêt app confirmé avant synchronisation"
"""
    code, out, err = ctx.remote_or_default().run(script)
    print(f"{DIM}{out.strip()}{RESET}")
    if err.strip():
        print(f"{DIM}{err.strip()}{RESET}")
    for ligne in out.splitlines():
        if ligne.startswith("RADAR_ROLLBACK_TAG="):
            ctx.rollback_image = ligne.split("=", 1)[1].strip()
    return int(code)


def step_rebuild(ctx: Context) -> int:
    """Construit l'image cible sans démarrer l'app ; son graphe Alembic y est inclus.

    Le tag de retour créé à l'étape précédente est revérifié après le build :
    c'est le moment où `latest` a été réécrit, donc celui où l'image d'avant
    doit encore être atteignable. Un tag disparu ne se découvrirait sinon qu'au
    moment du retour arrière, c'est-à-dire trop tard.
    """
    conservation = ""
    if ctx.rollback_image:
        conservation = f"""
tag={shlex.quote(ctx.rollback_image)}
docker inspect --format '{{{{.Id}}}}' "$tag" > /dev/null 2>&1 \\
  || {{ echo "tag de retour disparu après le build : $tag"; exit 1; }}
echo "tag de retour toujours présent après le build : $tag"
"""
    script = f"set -eu\ncd {ctx.remote_dir}\ndocker compose build app\n{conservation}"
    code, out, err = ctx.remote_or_default().run(script)
    print(f"{DIM}{out.strip()}{RESET}")
    if err.strip():
        print(f"{DIM}{err.strip()}{RESET}")
    return int(code)


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
    return int(code)


def step_start(ctx: Context) -> int:
    """Démarre seulement après migration réussie et attend la santé réelle.

    Le démarrage porte `--no-deps` : Compose ne réconcilie donc pas les
    dépendances. L'invariance est **ensuite vérifiée**, pas supposée — c'est la
    différence entre une parade et une garantie contrôlée. Le contrôle a lieu
    même si la santé échoue, tant que l'inspection reste possible : un
    déploiement qui a touché PostgreSQL doit le dire, quelle que soit l'issue.
    """
    script = f"""set -eu
cd {ctx.remote_dir}
{APP_ONLY_UP}
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

    # Le contrôle d'invariance tourne même quand la santé a échoué : un
    # déploiement qui a touché PostgreSQL doit le dire, quelle que soit l'issue.
    verdict = _verify_dependencies_unchanged(ctx)
    if verdict.ecarts:
        for ecart in verdict.ecarts:
            print(f"{RED}écart de dépendance : {ecart}{RESET}")
        print(
            f"{RED}Le déploiement n'est pas déclaré réussi : un service dépendant a "
            f"changé. Aucune réparation automatique n'est tentée.{RESET}"
        )
        return 1
    if not verdict.verifiee:
        # Ne pas pouvoir inspecter n'autorise pas à conclure. Le déploiement
        # s'arrête sans affirmer que les dépendances ont changé — on ne sait
        # pas — ni les déclarer intactes : ce serait une garantie sans preuve.
        print(
            f"{RED}Invariance des dépendances NON VÉRIFIÉE : leur état n'a pas pu "
            f"être relu après le démarrage.{RESET}"
        )
        print(
            f"{RED}Ni modifiées ni intactes ne sont établis. Déploiement non "
            f"entièrement validé ; aucune réparation ni retour arrière automatique.{RESET}"
        )
        return 1
    print(f"{GREEN}dépendances inchangées ({', '.join(DEPENDENT_SERVICES)}){RESET}")
    return int(code)


def _verify_dependencies_unchanged(ctx: Context) -> InvarianceVerdict:
    """Relit l'identité des dépendances et rend un verdict explicite.

    Une inspection ratée rend un verdict **non vérifié**, jamais un succès : ne
    pas avoir pu regarder ne prouve pas que rien n'a bougé. L'appelant décide
    quoi en faire, mais il ne peut pas confondre les deux situations.
    """
    if ctx.dependencies_before is None:
        return InvarianceVerdict.non_verifiee(
            "identité des dépendances inconnue avant le déploiement"
        )
    capture = capture_script(ctx.remote_dir)
    code, out, err = ctx.remote_or_default().run(capture)
    if code != 0:
        return InvarianceVerdict.non_verifiee(
            f"inspection en échec (code {code}) : {err.strip() or 'sans détail'}"
        )
    try:
        apres = decode_capture(out)
    except DependencyError as exc:
        return InvarianceVerdict.non_verifiee(f"relevé illisible : {exc}")
    return InvarianceVerdict.concluante(compare(ctx.dependencies_before, apres, DEPENDENT_SERVICES))


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
        None if ctx.dry_run else (ctx.remote if ctx.remote is not None else ctx.remote_or_default())
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
    if not ctx.pilot:
        print(f"{DIM}envoie la notification de mise en ligne sur Telegram{RESET}")
    current_head, commits = get_deployed_commits(ctx)
    if ctx.pilot:
        if not re.fullmatch("[a-f0-9]{40}", current_head):
            return 1
        code, _, _ = ctx.remote_or_default().run(
            f"set -eu\ncd {ctx.remote_dir}\necho {shlex.quote(current_head)} > .deployed_commit\n"
        )
        return int(code)
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
    ("Activation du pilote", step_pilot_activate),
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
            return int(code)
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
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--pilot-manifest", type=Path)
    parser.add_argument("--pilot-digest", default="")
    args = parser.parse_args()
    if args.pilot and not all((args.pilot_manifest, args.pilot_digest)):
        parser.error("Le pilote exige un manifeste et son empreinte")

    ctx = Context(
        host=args.host,
        key=args.key,
        remote_dir=args.remote_dir,
        repo_root=repo_root(),
        dry_run=args.dry_run, pilot=args.pilot, pilot_manifest=args.pilot_manifest,
        pilot_digest=args.pilot_digest,
    )

    print(f"{BOLD}Déploiement de BotRadar{RESET}  {DIM}→ {ctx.host}:{ctx.remote_dir}{RESET}")
    if ctx.dry_run:
        print(f"{YELLOW}Simulation{RESET} {DIM}: aucune commande ne sera exécutée.{RESET}")

    code = run_steps(STEPS, ctx)
    if code != 0:
        return int(code)
    if ctx.dry_run:
        print(f"\n{GREEN}Ordre des étapes vérifié, rien n'a été exécuté.{RESET}")
        return 0
    print(f"\n{GREEN}Déploiement terminé et vérifié.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Vérifie que le VPS est dans l'état attendu avant (ou après) une synchronisation.

Un push n'a jamais déployé quoi que ce soit : `~/bot-radar` n'est pas un dépôt git,
c'est une cible `rsync`. Deux dérives ont déjà survécu à des pushes verts —

- le code poussé n'a jamais été envoyé, le bot a continué de servir l'ancien ;
- `APP_ENV` est resté à `development`, donc la garde-fou des secrets ne s'exécutait
  jamais et `/docs` servait la surface d'ingestion en clair ;
- le schéma et le code qui le sert ont divergé, et l'application démarre sur une
  base qui ne lui correspond plus.

Aucune des deux n'est visible depuis le dépôt local. Ce script les rend visibles en
interrogeant le serveur, et échoue bruyamment plutôt que de laisser passer.

Usage :
    python scripts/preflight_deploy.py            # avant synchronisation
    python scripts/preflight_deploy.py --post-sync  # après, écarts de code fatals

Les secrets ne sont jamais affichés : seul leur présence est rapportée.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOST = os.environ.get("VPS_USER_HOST", "")
DEFAULT_KEY = os.path.expanduser("~/.ssh/ssh-key-2026-09-10.key")
DEFAULT_REMOTE_DIR = "~/bot-radar"

# Le filtre rsync documenté dans AGENTS.md. Un fichier exclu ici n'est jamais
# comparé : le distant peut légitimement diverger sans que ce soit une alerte.
RSYNC_EXCLUDES = frozenset(
    {".env", ".venv", ".git", "vendor", "node_modules", "alembic/__pycache__"}
)
RSYNC_EXCLUDE_PREFIXES = ("vendor/", ".venv/", ".git/", "node_modules/")

# Les quatre secrets que `validate_production_secrets` refuse de laisser vides.
REQUIRED_SECRETS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "INGEST_API_TOKEN",
    "INTERNAL_API_TOKEN",
)

# Un second scheduler ou une file d'attente casserait l'invariant n°2.
FORBIDDEN_CONTAINERS = ("worker", "celery", "rq", "beat", "scheduler")

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass
class Report:
    """Résultat du contrôle : une liste de contrôles, un seul verdict."""

    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  {GREEN}OK{RESET}    {label}" + (f" {DIM}{detail}{RESET}" if detail else ""))

    def fail(self, label: str, detail: str) -> None:
        self.failures.append(f"{label} — {detail}")
        print(f"  {RED}ECHEC{RESET} {label} {RED}{detail}{RESET}")

    def warn(self, label: str, detail: str) -> None:
        self.warnings.append(f"{label} — {detail}")
        print(f"  {YELLOW}ATTENTION{RESET} {label} {detail}")

    def skip(self, label: str, why: str) -> None:
        print(f"  {DIM}--    {label} ({why}){RESET}")


class Remote:
    """Exécute des scripts shell sur le VPS.

    Le script part sur l'entrée standard de `bash -s`, jamais sur la ligne de
    commande : ssh réécrit les guillemets qu'il reçoit, ce qui cassait toute
    commande contenant un apostrophe — un programme `awk`, une chaîne vide.
    Sur stdin, le quoting est écrit une fois et il est le même partout.
    """

    def __init__(self, host: str, key: str) -> None:
        self.host = host
        self.key = key

    def run(self, script: str) -> tuple[int, str, str]:
        argv = [
            "ssh",
            "-i",
            self.key,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=15",
            self.host,
            "bash -s",
        ]
        try:
            proc = subprocess.run(
                argv, input=script, capture_output=True, text=True, timeout=120
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 255, "", str(exc)
        return proc.returncode, proc.stdout, proc.stderr

    def reachable(self) -> bool:
        code, _, _ = self.run("true")
        return code == 0


def local_run(*argv: str) -> tuple[int, str]:
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip()


def synced_files() -> list[str]:
    """Fichiers suivis par git que le rsync pousse effectivement."""
    code, out = local_run("git", "ls-files", "-z")
    if code != 0:
        return []
    files = [path for path in out.split("\0") if path]
    return [
        path
        for path in files
        if path not in RSYNC_EXCLUDES
        and not path.startswith(RSYNC_EXCLUDE_PREFIXES)
    ]


def manifest(files: list[str]) -> str:
    """Ligne `sha256  chemin` par fichier, au format que `sha256sum -c` attend."""
    lines = []
    for path in files:
        try:
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            continue
        lines.append(f"{digest}  {path}\n")
    return "".join(lines)


def local_alembic_head() -> str | None:
    """Révision la plus haute du graphe Alembic, lue sans base de données.

    `alembic heads` demanderait une configuration que ce script ne doit pas
    supposer. Les révisions et leurs `down_revision` sont dans les fichiers ; la
    tête est celle que personne ne référence comme parent.
    """
    revisions: dict[str, str | None] = {}
    directory = "alembic/versions"
    if not os.path.isdir(directory):
        return None
    pattern_revision = re.compile(r"^revision[^=\n]*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
    pattern_down = re.compile(r"^down_revision[^=\n]*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            body = handle.read()
        found_revision = pattern_revision.search(body)
        if not found_revision:
            continue
        found_down = pattern_down.search(body)
        revisions[found_revision.group(1)] = found_down.group(1) if found_down else None

    if not revisions:
        return None
    referenced = {down for down in revisions.values() if down}
    heads = [rev for rev in revisions if rev not in referenced]
    return heads[0] if len(heads) == 1 else None


def check_local(report: Report) -> str | None:
    """État du dépôt : on ne déploie que du code commité et publié."""
    print("\nDépôt local")

    code, out = local_run("git", "status", "--porcelain")
    if out:
        report.fail("arbre propre", f"{len(out.splitlines())} fichier(s) modifié(s)")
    else:
        report.ok("arbre propre")

    code, head = local_run("git", "rev-parse", "--short", "HEAD")
    code2, remote_head = local_run(
        "git", "ls-remote", "origin", "refs/heads/main"
    )
    if code or code2 or not remote_head:
        report.warn("HEAD publié", "impossible de joindre origin/main")
    elif not remote_head.startswith(head):
        report.fail(
            "HEAD publié",
            f"local {head} ≠ origin/main {remote_head[:len(head)]} : le code déployé "
            "ne serait pas celui de GitHub",
        )
    else:
        report.ok("HEAD publié", head)

    head_revision = local_alembic_head()
    if head_revision:
        report.ok("révision Alembic locale", head_revision)
    return head_revision


def secrets_script(remote_dir: str) -> str:
    """Script distant qui rapporte la présence — et non la valeur — des secrets.

    Isolé pour être testable : le quoting imbriqué (apostrophe fermante d'un
    programme awk, guillemets Python) a déjà coûté une itération entière.
    """
    keys = "|".join(REQUIRED_SECRETS)
    return (
        f"cd {remote_dir} || exit 1\n"
        f"awk -F= '/^({keys})=/ "
        # Le littéral est en guillemets doubles : son contenu se termine par
        # l'apostrophe qui ferme le programme awk, pas par le délimiteur Python.
        "{ v=$2; gsub(/[\"[:space:]]/,\"\",v); print $1 (v==\"\" ? \" VIDE\" : \" OK\") }'"
        " .env\n"
    )


def check_remote_config(report: Report, remote: Remote, remote_dir: str) -> None:
    """Configuration et topologie du serveur."""
    print("\nVPS")

    code, out, err = remote.run(
        f"cd {remote_dir} && grep -E '^[[:space:]]*APP_ENV=' .env"
    )
    if code != 0:
        report.fail("APP_ENV", f".env illisible ({err.strip()[:60] or 'code != 0'})")
        return
    value = out.strip().split("=", 1)[-1].strip().strip("\"'")
    if value != "production":
        report.fail(
            "APP_ENV",
            f"{value or 'absent'} au lieu de production : garde-fou des secrets inerte "
            "et /docs exposé",
        )
    else:
        report.ok("APP_ENV", "production")

    # Présence seule : la valeur d'un secret ne doit jamais sortir d'ici.
    code, out, _ = remote.run(secrets_script(remote_dir))
    if code != 0:
        report.warn("secrets de production", "lecture impossible")
    else:
        empty = [line.split()[0] for line in out.splitlines() if line.endswith("VIDE")]
        if empty:
            report.fail("secrets de production", f"vides : {', '.join(empty)}")
        else:
            report.ok("secrets de production", f"{len(REQUIRED_SECRETS)} renseignés")

    code, out, _ = remote.run("docker ps --format '{{.Names}}'")
    offenders = [
        name
        for name in out.split()
        if any(word in name.lower() for word in FORBIDDEN_CONTAINERS)
    ]
    if offenders:
        report.fail("un seul scheduler", f"conteneur interdit : {', '.join(offenders)}")
    else:
        report.ok("un seul scheduler")

    code, out, _ = remote.run(
        "docker inspect radar_app --format '{{.State.Health.Status}}' 2>/dev/null"
    )
    status = out.strip()
    if status == "healthy":
        report.ok("conteneur radar_app", "healthy")
    else:
        report.fail("conteneur radar_app", f"état {status or 'inconnu'}")


def check_target_compose(report: Report, remote: Remote, remote_dir: str) -> None:
    """Valide la configuration cible avant arrêt, sans afficher ses valeurs."""
    target = (Path(__file__).resolve().parents[1] / "compose.yml").read_text()
    script = (
        f"cd {remote_dir} || exit 1\n"
        "docker compose --env-file .env -f - config --no-env-resolution --quiet "
        "<<'RADAR_TARGET_COMPOSE'\n"
        f"{target}\nRADAR_TARGET_COMPOSE\n"
    )
    code, _, _ = remote.run(script)
    if code:
        report.fail(
            "configuration Compose cible",
            "validation refusée ; vérifier la syntaxe, la version Compose et les "
            "paramètres PostgreSQL non vides avant tout arrêt",
        )
    else:
        report.ok("configuration Compose cible", "valide sans démarrage de service")


def check_remote_code(
    report: Report, remote: Remote, remote_dir: str, post_sync: bool
) -> None:
    """Le code servi est-il celui du dépôt ? C'est la dérive la plus coûteuse."""
    files = synced_files()
    if not files:
        report.warn("code déployé", "aucun fichier suivi par git")
        return

    check = manifest(files)
    if not check:
        report.warn("code déployé", "manifeste vide")
        return

    # Le manifeste voyage dans le script, délimité par un heredoc : une seule passe
    # réseau, et aucun quoting à invoquer.
    script = (
        f"cd {remote_dir} || exit 1\n"
        f"sha256sum -c --quiet - <<'MANIFEST' 2>&1\n{check}MANIFEST\n"
    )
    code, out, err = remote.run(script)
    label = "code déployé"
    detail_lines = [line for line in out.splitlines() if line.strip()]
    if code == 0:
        report.ok(label, f"{len(files)} fichiers identiques")
    elif post_sync:
        report.fail(label, f"{len(detail_lines)} fichier(s) divergent(s) après synchro")
        for line in detail_lines[:10]:
            print(f"        {DIM}{line}{RESET}")
    else:
        report.warn(label, f"{len(detail_lines)} fichier(s) à synchroniser")
    if err.strip() and code == 0:
        print(f"        {DIM}{err.strip()[:100]}{RESET}")

    if post_sync:
        # Le conteneur sert le code de son image, pas les fichiers synchronisés.
        image_files = [
            name for name in files
            if name.startswith(("app/", "alembic/", "scripts/")) or name == "alembic.ini"
        ]
        image_check = manifest(image_files)
        if not image_check:
            report.fail("code servi par radar_app", "manifeste de l'image vide")
            return
        script = (
            f"cd {remote_dir} || exit 1\n"
            "docker exec -i -w /app radar_app sha256sum -c --quiet - "
            "<<'IMAGE_MANIFEST' 2>&1\n"
            f"{image_check}IMAGE_MANIFEST\n"
        )
        code, _, _ = remote.run(script)
        if code:
            report.fail(
                "code servi par radar_app",
                "conteneur illisible ou fichiers différents de la cible ; reprise à vérifier",
            )
        else:
            report.ok("code servi par radar_app", "empreintes des fichiers cibles identiques")


# Alembic écrit « révision (head) » ; ses logs partagent `2>&1` et commencent aussi
# par un mot alphanumérique : lire le premier mot prendrait « INFO » pour une révision.
_REVISION_LINE = re.compile(r"^([A-Za-z0-9_]+)\s+\((?:head|branch[^)]*)\)")
_HEAD_LINE = re.compile(r"^([A-Za-z0-9_]+)\s+\(head\)", re.MULTILINE)


def migration_state_script(remote_dir: str) -> str:
    """Script distant : ce que la base contient, et ce que le code déployé attend.

    Les deux lectures viennent du conteneur : c'est son image qui décide du graphe
    Alembic qu'il sait exécuter. Lire le dossier de travail donnerait le graphe du
    code synchronisé, pas celui qui tourne — et les deux diffèrent précisément
    quand une migration vient d'être ajoutée.

    `< /dev/null` n'est pas cosmétique : le script arrive sur l'entrée standard de
    `bash -s`, et `docker compose exec` la consomme. Sans cette redirection, la
    seconde lecture n'est jamais exécutée — le script se termine sur un succès
    menteur, avec une seule tête lue au lieu de deux.
    """
    return (
        f"cd {remote_dir} || exit 1\n"
        "echo __CURRENT__\n"
        "docker compose exec -T app alembic current 2>&1 < /dev/null | tail -n 1\n"
        "echo __HEADS__\n"
        "docker compose exec -T app alembic heads 2>&1 < /dev/null\n"
    )


def parse_migration_state(out: str) -> tuple[str | None, list[str]]:
    """Lit la révision courante et les têtes du graphe dans la sortie du script."""
    current_block, _, heads_block = out.partition("__HEADS__")
    current_block = current_block.partition("__CURRENT__")[2]

    current: str | None = None
    for line in current_block.splitlines():
        match = _REVISION_LINE.match(line.strip())
        if match:
            current = match.group(1)
            break
    if current is None:
        # Un `alembic current` sans étiquette reste lisible : une révision isolée.
        for line in current_block.splitlines():
            stripped = line.strip()
            if re.fullmatch(r"[A-Za-z0-9_]+", stripped):
                current = stripped
                break

    heads = _HEAD_LINE.findall(heads_block)
    return current, heads


def remote_migration_state(
    remote: Remote, remote_dir: str
) -> tuple[str | None, list[str], str]:
    """Révision de la base et têtes du code déployé ; le dernier champ est l'erreur."""
    code, out, err = remote.run(migration_state_script(remote_dir))
    if code != 0:
        return None, [], (err or out).strip()[:80] or "échec de la lecture"
    current, heads = parse_migration_state(out)
    return current, heads, ""


def check_remote_migration(
    report: Report,
    remote: Remote,
    remote_dir: str,
    expected_head: str | None,
    post_sync: bool,
) -> None:
    """Le schéma et le code qui le sert ne doivent jamais diverger.

    Avant synchro, la référence est la tête du graphe **du conteneur qui tourne**.
    Exiger que la base porte déjà la révision locale rendait tout déploiement d'une
    migration impossible : le rebuild précède l'`upgrade head`, donc la base est
    nécessairement en retard au moment du pré-vol. Ce qui reste fatal, c'est la
    dérive réelle — un schéma en avance ou en retard sur le code qui le sert.

    Après synchro, la migration a été jouée : la base doit porter la tête locale.
    """
    print("\nBase de données")
    current, heads, error = remote_migration_state(remote, remote_dir)
    if error:
        report.fail("migration en tête", error)
        return

    if post_sync:
        if expected_head is None:
            report.skip("migration en tête", "révision locale illisible")
            return
        if current == expected_head:
            report.ok("migration en tête", current)
        else:
            actual = current or "inconnue"
            report.fail("migration en tête", f"attendu {expected_head}, base à {actual}")
        return

    if len(heads) != 1:
        report.fail(
            "migration en tête",
            (
                f"plusieurs têtes dans le code déployé : {', '.join(heads)}"
                if heads
                else "tête Alembic du code déployé illisible"
            ),
        )
        return

    if current != heads[0]:
        actual = current or "inconnue"
        report.fail(
            "migration en tête",
            f"schéma et code déployé divergent : base à {actual}, code à {heads[0]}",
        )
        return

    if expected_head and expected_head != current:
        report.ok("migration en tête", f"{current} — {expected_head} à jouer après synchro")
    else:
        report.ok("migration en tête", current)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("DEPLOY_HOST", DEFAULT_HOST))
    parser.add_argument("--key", default=os.environ.get("DEPLOY_KEY", DEFAULT_KEY))
    parser.add_argument(
        "--remote-dir", default=os.environ.get("DEPLOY_DIR", DEFAULT_REMOTE_DIR)
    )
    parser.add_argument(
        "--post-sync",
        action="store_true",
        help="après synchronisation : un écart de code devient fatal",
    )
    args = parser.parse_args()

    post = "après synchro" if args.post_sync else "avant synchro"
    print(f"{DIM}Pré-vol {post} — {args.host}:{args.remote_dir}{RESET}")

    report = Report()
    expected_head = check_local(report)

    remote = Remote(args.host, args.key)
    if not remote.reachable():
        report.fail("accès au VPS", f"{args.host} injoignable")
    else:
        check_target_compose(report, remote, args.remote_dir)
        check_remote_config(report, remote, args.remote_dir)
        check_remote_code(report, remote, args.remote_dir, args.post_sync)
        check_remote_migration(
            report, remote, args.remote_dir, expected_head, args.post_sync
        )

    print()
    if report.failures:
        print(f"{RED}{len(report.failures)} contrôle(s) en échec.{RESET}")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1

    if report.warnings:
        print(f"{YELLOW}Déploiement possible, avec {len(report.warnings)} réserve(s).{RESET}")
        for warning in report.warnings:
            print(f"  - {warning}")
        return 0

    print(f"{GREEN}VPS conforme.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

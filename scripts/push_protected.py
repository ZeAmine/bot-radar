#!/usr/bin/env python3
"""Pousse sur une branche protégée : lève la protection, pousse, la restaure.

`main` exige le status check `check`, et la CI ne peut pas tourner sur un commit qui
n'existe pas encore sur GitHub. Le push direct est donc refusé, et la procédure
documentée dans AGENTS.md consisted in six commandes `gh api` à recopier — dont
deux (`DELETE` puis `PUT`) qui laissent la branche nue si l'une d'elles échoue.

Ce script fait la même chose en un geste, avec une propriété que la procédure
manuelle n'offre pas : la restauration est dans un `finally`. Un push rejeté, une
exception ou un Ctrl-C rendent la protection exactement dans l'état où le script
l'a trouvée. Ce qu'aucun `finally` ne couvre est un `SIGKILL` ; la fenêtre est
donc courte, et l'état restauré est relu après coup pour le prouver.

Usage :
    python scripts/push_protected.py                    # pousse sur main
    python scripts/push_protected.py --dry-run          # relit le plan, n'exécute rien
    python scripts/push_protected.py --repo o/r --branch trunk

Codes de retour :
    0  poussé, protection restaurée à l'identique
    1  refus avant toute action (arbre sale, gh absent, état non restaurable)
    2  le push a échoué, la protection a été restaurée
    3  la restauration n'a pas pu être vérifiée : à reprendre à la main
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from preflight_deploy import DIM, GREEN, RED, RESET, YELLOW  # noqa: E402

DEFAULT_REPO = "ZeAmine/bot-radar"
DEFAULT_BRANCH = "main"

# Deux points de l'API, mesurés sur ce dépôt et documentés dans AGENTS.md :
#   - `allow_fork_syncing` est annoncé `true` par la doc, réellement appliqué `false` ;
#     le PUT répond 200 sans dire que la valeur n'a pas pris, seul un GET ultérieur
#     le révèle ;
#   - `block_creations` et `required_signatures` figurent dans le GET mais ne se
#     règlent pas par ce PUT.
# Un état qui porte l'un de ces deux champs n'est donc pas restaurable par ce
# script : il est refusé avant toute suppression, pas restauré à moitié.
UNSETTABLE_FIELDS = ("block_creations", "required_signatures")

# Réglages que le PUT accepte sous forme d'objet `{"enabled": bool}`.
BOOLEAN_SETTINGS = (
    "required_linear_history",
    "allow_force_pushes",
    "allow_deletions",
    "required_conversation_resolution",
    "lock_branch",
    "allow_fork_syncing",
)

OK, REFUSED, PUSH_FAILED, UNVERIFIED = 0, 1, 2, 3


def run(argv: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    """Exécute une commande et rend ses trois issues.

    La sortie d'erreur compte autant que la standard : `git push` écrit son compte
    rendu sur stderr, et `gh auth status` se plaint sur cette sortie.
    """
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True)
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _enabled(node: Any) -> bool:
    return isinstance(node, dict) and node.get("enabled") is True


def unrestorable(state: dict[str, Any]) -> list[str]:
    """Réglages capturés que ce script ne sait pas remettre en place."""
    return [name for name in UNSETTABLE_FIELDS if _enabled(state.get(name))]


def restore_payload(state: dict[str, Any]) -> dict[str, Any]:
    """Le corps du `PUT` qui remet l'état capturé, sans les URL du `GET`.

    Le GET répond des URL de sous-ressources (`url`, `contexts_url`) : les renvoyer
    dans le PUT le fait échouer. On ne garde que ce que l'endpoint accepte.

    `enforce_admins` absent du GET vaut « actif » : c'est la règle du dépôt
    (le propriétaire n'échappe pas à la protection), pas un défaut à corriger en
    silence si GitHub cessait de renvoyer le champ.
    """
    payload: dict[str, Any] = {
        "required_status_checks": None,
        "required_pull_request_reviews": None,
        "restrictions": None,
        "enforce_admins": {
            "enabled": True
            if state.get("enforce_admins") is None
            else _enabled(state["enforce_admins"])
        },
    }

    status = state.get("required_status_checks")
    if isinstance(status, dict):
        checks: dict[str, Any] = {
            "strict": bool(status.get("strict", False)),
            "contexts": list(status.get("contexts") or []),
        }
        if status.get("checks"):
            checks["checks"] = list(status["checks"])
        payload["required_status_checks"] = checks

    for name in BOOLEAN_SETTINGS:
        payload[name] = {"enabled": _enabled(state.get(name))}
    return payload


def diff_states(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Ce que la restauration n'a pas remis à l'identique, en clair.

    La comparaison se fait sur les deux corps de PUT, pas sur les deux GET : les
    URL de sous-ressources diffèrent toujours et ne diraient rien d'un écart réel.
    """
    wanted = restore_payload(before)
    got = restore_payload(after)
    return [
        f"{key} : attendu {value}, obtenu {got.get(key)}"
        for key, value in wanted.items()
        if got.get(key) != value
    ]


@dataclass
class GhProtection:
    """L'API de protection de branche, telle que `gh` l'expose."""

    repo: str = DEFAULT_REPO
    branch: str = DEFAULT_BRANCH

    @property
    def path(self) -> str:
        return f"repos/{self.repo}/branches/{self.branch}/protection"

    def state(self) -> tuple[int, dict[str, Any] | None]:
        """(0, état) si la branche est protégée, (1, None) si elle ne l'est pas."""
        code, out, _ = run(["gh", "api", self.path])
        if code != 0:
            return 1, None
        try:
            parsed: dict[str, Any] = json.loads(out)
        except json.JSONDecodeError:
            return 1, None
        return 0, parsed

    def remove(self) -> int:
        return run(["gh", "api", "-X", "DELETE", self.path])[0]

    def restore(self, payload: dict[str, Any]) -> int:
        return run(
            ["gh", "api", "-X", "PUT", self.path, "--input", "-"],
            stdin=json.dumps(payload),
        )[0]


def push_window(
    client: Any,
    push: Callable[[], int],
    dry_run: bool = False,
) -> int:
    """Lève la protection, pousse, et restaure quoi qu'il arrive.

    La restauration est un `finally` : c'est toute la raison d'être du script. Le
    push est la seule étape qui puisse échouer légitimement (commit rejeté, conflit),
    et il n'empêche pas la remise en place.
    """
    code, state = client.state()
    if code != 0 or state is None:
        print(f"{YELLOW}Aucune protection{RESET} {DIM}sur {client.branch} : push direct.{RESET}")
        if dry_run:
            return OK
        return push()

    blocked = unrestorable(state)
    if blocked:
        print(
            f"{RED}Refus{RESET} l'état capturé porte {', '.join(blocked)} : "
            f"ce script ne sait pas les remettre. Protection intacte."
        )
        return REFUSED

    if dry_run:
        print(f"{DIM}Simulation : DELETE protection, push, PUT de l'état capturé.{RESET}")
        print(json.dumps(restore_payload(state), indent=2))
        return OK

    started = time.monotonic()
    if client.remove() != 0:
        print(f"{RED}Échec{RESET} la protection n'a pas pu être levée : rien n'a été poussé.")
        return REFUSED

    print(f"{YELLOW}Protection levée{RESET} {DIM}pousser maintenant.{RESET}")
    # Le verdict de la restauration vit dans une variable, pas dans un `return` :
    # un `return` depuis le `finally` avalerait l'exception du push (un Ctrl-C), et
    # le push interrompu est précisément le cas où il faut lire la ligne
    # « protection restaurée » avant de quitter.
    verdict = OK
    try:
        push_code = push()
    finally:
        verdict = restore_and_verify(client, state, started)

    if verdict != OK:
        return verdict
    if push_code != 0:
        print(f"{YELLOW}Push refusé{RESET} {DIM}code {push_code} ; la branche est protégée.{RESET}")
        return PUSH_FAILED
    return OK


def restore_and_verify(client: Any, state: dict[str, Any], started: float) -> int:
    """Remet la protection en place, relit l'état, et rend le verdict.

    Le `PUT` répond 200 même quand une valeur n'a pas pris (`allow_fork_syncing`) :
    la seule preuve est le `GET` qui suit, comparé à l'état capturé.
    """
    elapsed = time.monotonic() - started
    if client.restore(restore_payload(state)) != 0:
        print(
            f"{RED}ALERTE{RESET} la protection n'a pas été remise ({elapsed:.1f}s de fenêtre) "
            f"— rejoue le PUT affiché par --dry-run."
        )
        return UNVERIFIED
    print(f"{GREEN}Protection restaurée{RESET} {DIM}en {elapsed:.1f}s.{RESET}")

    final_code, final_state = client.state()
    if final_code != 0 or final_state is None:
        print(f"{RED}ALERTE{RESET} l'état restauré est illisible : à vérifier à la main.")
        return UNVERIFIED

    divergences = diff_states(state, final_state)
    for divergence in divergences:
        print(f"{RED}ÉCART{RESET} {divergence}")
    return UNVERIFIED if divergences else OK


def check_local(report_gh: bool = True) -> tuple[int, str]:
    """L'arbre est propre, la branche est la bonne, `gh` répond : trois préconditions.

    Pousser un arbre sale laisserait le code déployé différent de GitHub, et le
    pré-vol de `make deploy` le refuse ensuite : mieux vaut l'apprendre ici.
    """
    code, out, _ = run(["git", "status", "--porcelain"])
    if code != 0:
        return REFUSED, "dépôt local illisible"
    if out.strip():
        return REFUSED, f"{len(out.splitlines())} fichier(s) non commité(s)"

    if report_gh:
        code, _, _ = run(["gh", "auth", "status"])
        if code != 0:
            return REFUSED, "`gh` absent ou non authentifié"
    return OK, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("PUSH_REPO", DEFAULT_REPO))
    parser.add_argument("--branch", default=os.environ.get("PUSH_BRANCH", DEFAULT_BRANCH))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="relit le plan et le corps du PUT sans rien exécuter",
    )
    args = parser.parse_args()

    print(f"{DIM}Push protégé{RESET} {args.repo} → {args.branch}")
    code, reason = check_local()
    if code != OK:
        print(f"{RED}Refus{RESET} {reason} — rien n'a été touché.")
        return code

    client = GhProtection(repo=args.repo, branch=args.branch)
    return push_window(
        client,
        lambda: run(["git", "push", "origin", args.branch])[0],
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())

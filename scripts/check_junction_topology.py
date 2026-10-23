#!/usr/bin/env python3
"""Vérifie que la jonction conteneur → tunnel correspond encore au terrain.

Trois valeurs doivent concorder, et aucune ne découle des deux autres :

1. le sous-réseau et la gateway du réseau Docker applicatif ;
2. la règle de pare-feu qui autorise le sous-réseau vers la gateway, port 1080 ;
3. le `permitlisten` de la clé du compte de tunnel, qui fixe où `sshd` accepte
   la redirection inverse.

Une dérive de la gateway rend 2 et 3 caducs : le tunnel se ferme, la collecte
échoue, et rien dans l'application ne désigne la cause. Ce contrôle la montre
avant la collecte.

**Privilèges requis** : le contrôle a besoin de lire `iptables`, qui exige
`CAP_NET_ADMIN` — soit en `root`, soit via `sudo`. `sshd -T` pour un compte
donné exige aussi les droits de lecture de la configuration. Le compte de tunnel
se passe par `--tunnel-user` ou par `TUNNEL_USER` : aucun nom n'est figé. Sans ces droits le
script ne conclut pas : il rend « non vérifié », jamais « conforme ».

Chaque commande est en lecture seule : ce script ne modifie ni le réseau, ni le
pare-feu, ni `authorized_keys`. La clé de tunnel est lue sans afficher son
contenu ni son empreinte.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from collections.abc import Callable

PORT = "1080"
NETWORK = "bot-radar_default"

# Codes de sortie : quatre issues, jamais confondues.
EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_UNKNOWN = 2
EXIT_PRIVILEGE = 3
EXIT_USAGE = 4

Runner = Callable[[str], tuple[int, str, str]]

# `iptables -C` rend 1 quand la règle est absente, et un autre code quand
# l'invocation elle-même échoue — droits insuffisants, binaire absent, syntaxe.
IPTABLES_RULE_ABSENT = 1

PERMITLISTEN_IN_KEY = re.compile(r'permitlisten="([^"]+)"')


class ProbeError(Exception):
    """Une valeur n'a pas pu être relevée : ni conforme, ni en écart."""


class PrivilegeError(ProbeError):
    """Le relevé a échoué faute de droits : distinct d'une erreur de terrain."""


def default_runner(script: str) -> tuple[int, str, str]:
    """Exécute un script local ; remplacé par un double dans les tests."""
    import subprocess

    resultat = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    return resultat.returncode, resultat.stdout, resultat.stderr


def _looks_like_privilege_error(stderr: str) -> bool:
    """Reconnaît les messages d'échec de droits des outils système."""
    indice = stderr.lower()
    return any(
        motif in indice
        for motif in (
            "permission denied",
            "operation not permitted",
            "must be root",
            "you need to be root",
            "insufficient privileges",
            "not permitted",
        )
    )


def read_network(run: Runner) -> tuple[str, str]:
    """Rend `(sous-réseau, gateway)` du réseau applicatif."""
    script = (
        f"docker network inspect {shlex.quote(NETWORK)} "
        "-f '{{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}}'"
    )
    code, out, err = run(script)
    if code != 0:
        raise ProbeError(f"réseau illisible (code {code}) : {err.strip()}")
    parties = out.split()
    if len(parties) != 2:
        raise ProbeError(f"réseau incomplet : {out.strip()!r}")
    return parties[0], parties[1]


def read_permitlisten(run: Runner, user: str) -> str:
    """Rend l'adresse écoutée par la clé du compte de tunnel."""
    script = (
        f"sshd -T -C user={shlex.quote(user)},host=localhost,addr=127.0.0.1 "
        "| grep -E '^permitlisten' | head -1"
    )
    code, out, err = run(script)
    if code != 0:
        if _looks_like_privilege_error(err):
            raise PrivilegeError(f"sshd -T refusé (droits) : {err.strip()}")
        raise ProbeError(f"permitlisten illisible (code {code}) : {err.strip()}")
    ligne = out.strip()
    if not ligne.startswith("permitlisten "):
        raise ProbeError(f"permitlisten absent du contexte : {ligne!r}")
    adresse, _, port = ligne.split(None, 1)[1].strip().rpartition(":")
    if port != PORT:
        raise ProbeError(f"permitlisten ne vise pas le port {PORT} : {port!r}")
    return adresse


def _address_only(value: str) -> str:
    """Retire le port d'une valeur `adresse:port`, pour comparer des adresses.

    La clé porte `permitlisten="<adresse>:1080"` alors que la gateway est une
    adresse seule. Comparer les deux formes brutes ne pourrait jamais concorder.
    """
    adresse, _, port = value.rpartition(":")
    return adresse if port.isdigit() else value


def read_key_permitlisten(
    run: Runner, user: str
) -> tuple[str | None, int]:
    """Rend `(option, nombre de lignes porteuses)` de `authorized_keys`.

    Lecture seule. Seule l'option est extraite : ni la clé publique, ni son
    empreinte, ni le commentaire ne sont rendus, donc rien de sensible ne peut
    fuir par la sortie ou par un journal.

    Trois états sont distingués explicitement, parce qu'ils n'ont pas le même
    sens : aucune ligne ne porte l'option (`(None, 0)`), une seule la porte
    (`(option, 1)`), plusieurs la portent (`(option, n>1)`) — ce dernier cas est
    ambigu et ne doit pas être tranché en silence.
    """
    home = run(f"getent passwd {shlex.quote(user)} | cut -d: -f6")
    code, out, err = home
    if code != 0:
        if _looks_like_privilege_error(err):
            raise PrivilegeError(f"getent refusé (droits) : {err.strip()}")
        raise ProbeError(f"compte introuvable (code {code}) : {err.strip()}")
    repertoire = out.strip()
    if not repertoire:
        raise ProbeError("répertoire du compte illisible")

    chemin = f"{repertoire}/.ssh/authorized_keys"
    code, out, err = run(f"cat {shlex.quote(chemin)}")
    if code != 0:
        if _looks_like_privilege_error(err):
            raise PrivilegeError(f"authorized_keys illisible (droits) : {err.strip()}")
        if "no such file" in err.lower():
            raise ProbeError(f"authorized_keys absent : {chemin}")
        raise ProbeError(f"authorized_keys illisible (code {code}) : {err.strip()}")
    if not out.strip():
        raise ProbeError(f"authorized_keys vide : {chemin}")

    options = [
        m.group(1)
        for ligne in out.splitlines()
        if not ligne.lstrip().startswith("#")
        for m in [PERMITLISTEN_IN_KEY.search(ligne)]
        if m
    ]
    if not options:
        return None, 0
    return options[0], len(options)


def firewall_rule_present(run: Runner, subnet: str, gateway: str) -> bool:
    """Dit si la règle exacte existe dans `INPUT`.

    Une règle absente (code 1) et un relevé impossible (autre code) ne veulent
    pas dire la même chose : la première est un **écart**, le second un
    **relevé non concluant**. Les confondre ferait passer une absence de droits
    pour une règle manquante, et pousserait à ajouter une règle déjà présente.
    """
    script = (
        f"iptables -C INPUT -s {shlex.quote(subnet)} -d {shlex.quote(gateway)} "
        f"-p tcp --dport {PORT} -j ACCEPT"
    )
    code, _, err = run(script)
    if code == 0:
        return True
    if code == IPTABLES_RULE_ABSENT:
        return False
    if _looks_like_privilege_error(err):
        raise PrivilegeError(f"iptables refusé (droits) : {err.strip()}")
    raise ProbeError(f"iptables non concluant (code {code}) : {err.strip()}")


def check(
    gateway: str,
    permitlisten: str,
    rule_present: bool,
    key_permitlisten: str | None = None,
    key_lines: int = 1,
) -> list[str]:
    """Rend les écarts entre la gateway et les valeurs qui la visent."""
    ecarts: list[str] = []
    if permitlisten != gateway:
        ecarts.append(
            "permitlisten de la configuration sshd ne vise pas la gateway : "
            f"{permitlisten} ≠ {gateway}"
        )
    if not rule_present:
        ecarts.append(
            f"règle de pare-feu absente : {PORT}/tcp du sous-réseau vers la gateway"
        )
    if key_lines > 1:
        ecarts.append(
            f"{key_lines} clés du compte portent permitlisten : laquelle sshd "
            "applique n'est pas déterminable, à trancher à la main"
        )
    elif key_permitlisten is None:
        ecarts.append(
            "aucune clé du compte de tunnel ne porte permitlisten : "
            "la restriction n'est pas posée"
        )
    elif _address_only(key_permitlisten) != gateway:
        ecarts.append(
            "permitlisten de la clé du compte de tunnel ne vise pas la gateway : "
            f"{key_permitlisten} ≠ {gateway}"
        )
    return ecarts


def verify(
    run: Runner, tunnel_user: str
) -> tuple[bool | None, list[str], bool]:
    """Rend `(conforme, écarts, droits_insuffisants)`.

    `conforme` vaut None quand une valeur manque : ni conforme, ni en écart.
    """
    try:
        subnet, gateway = read_network(run)
        permitlisten = read_permitlisten(run, tunnel_user)
        key_permitlisten, key_lines = read_key_permitlisten(run, tunnel_user)
    except PrivilegeError as exc:
        return None, [str(exc)], True
    except ProbeError as exc:
        return None, [str(exc)], False

    try:
        present = firewall_rule_present(run, subnet, gateway)
    except PrivilegeError as exc:
        return None, [str(exc)], True
    except ProbeError as exc:
        return None, [str(exc)], False

    ecarts = check(gateway, permitlisten, present, key_permitlisten, key_lines)
    return not ecarts, ecarts, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Contrôle de la jonction conteneur → tunnel (lecture seule)."
    )
    parser.add_argument(
        "--tunnel-user",
        default=os.environ.get("TUNNEL_USER"),
        help="compte dont la clé porte permitlisten (ou TUNNEL_USER).",
    )
    parser.add_argument("--json", action="store_true", help="rend le verdict en JSON.")
    args = parser.parse_args(argv)

    if not args.tunnel_user or not args.tunnel_user.strip():
        print(
            "compte de tunnel requis : --tunnel-user ou TUNNEL_USER.\n"
            "Le contrôle porte sur les clés de ce compte ; sans lui il ne peut "
            "pas conclure, et ne rend donc jamais « conforme ».",
            file=sys.stderr,
        )
        return EXIT_USAGE
    conforme, ecarts, droits = verify(default_runner, args.tunnel_user)
    if args.json:
        print(
            json.dumps(
                {
                    "conforme": conforme,
                    "ecarts": ecarts,
                    "droits_insuffisants": droits,
                },
                ensure_ascii=False,
            )
        )
    else:
        if droits:
            print("jonction NON VÉRIFIÉE : droits insuffisants.")
            print("  relancer avec les droits de lecture de iptables et de sshd.")
        elif conforme is None:
            print("jonction NON VÉRIFIÉE :")
        elif conforme:
            print("jonction conforme.")
        else:
            print("jonction EN ÉCART :")
        for ecart in ecarts:
            print(f"  - {ecart}")

    if droits:
        return EXIT_PRIVILEGE
    if conforme is None:
        return EXIT_UNKNOWN
    return EXIT_OK if conforme else EXIT_MISMATCH


if __name__ == "__main__":
    raise SystemExit(main())

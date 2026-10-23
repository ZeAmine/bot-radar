#!/usr/bin/env python3
"""Vérifie que la jonction conteneur → tunnel correspond encore au terrain.

Trois valeurs doivent concorder, et aucune ne découle des deux autres :

1. le sous-réseau et la gateway du réseau Docker applicatif ;
2. la règle de pare-feu qui autorise le sous-réseau vers la gateway, port 1080 ;
3. le `permitlisten` de la clé du compte de tunnel, qui fixe où `sshd` accepte
   la redirection inverse.

Une recréation du réseau peut changer 1 ; 2 et 3 visent alors une adresse qui
n'existe plus, le tunnel se ferme, et le collecteur échoue sans que rien dans
l'application ne désigne la cause. Ce contrôle montre l'écart avant la collecte.

Chaque commande est en lecture seule : ce script ne modifie ni le réseau, ni le
pare-feu, ni `authorized_keys`.
"""

from __future__ import annotations

import argparse
import json
import shlex
from collections.abc import Callable

PORT = "1080"
NETWORK = "bot-radar_default"

# Ordre de sortie du script : 0 conforme, 1 écart constaté, 2 relevé impossible.
EXIT_OK, EXIT_MISMATCH, EXIT_UNKNOWN = 0, 1, 2

Runner = Callable[[str], tuple[int, str, str]]


class ProbeError(Exception):
    """Une valeur n'a pas pu être relevée : ni conforme, ni en écart."""


def default_runner(script: str) -> tuple[int, str, str]:
    """Exécute un script local ; remplacé par un double dans les tests."""
    import subprocess

    resultat = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    return resultat.returncode, resultat.stdout, resultat.stderr


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


def read_permitlisten(run: Runner) -> str:
    """Rend l'adresse écoutée par la clé du compte de tunnel."""
    script = (
        "sshd -T -C user=radartunnel,host=localhost,addr=127.0.0.1 "
        "| grep -E '^permitlisten' | head -1"
    )
    code, out, err = run(script)
    if code != 0:
        raise ProbeError(f"permitlisten illisible (code {code}) : {err.strip()}")
    ligne = out.strip()
    if not ligne.startswith("permitlisten "):
        raise ProbeError(f"permitlisten absent du contexte : {ligne!r}")
    adresse, _, port = ligne.split(None, 1)[1].strip().rpartition(":")
    if port != PORT:
        raise ProbeError(f"permitlisten ne vise pas le port {PORT} : {port!r}")
    return adresse


def firewall_rule_present(run: Runner, subnet: str, gateway: str) -> bool:
    """Dit si la règle exacte existe dans `INPUT`."""
    script = (
        f"iptables -C INPUT -s {shlex.quote(subnet)} -d {shlex.quote(gateway)} "
        f"-p tcp --dport {PORT} -j ACCEPT"
    )
    code, _, _ = run(script)
    return code == 0


def check(gateway: str, permitlisten: str, rule_present: bool) -> list[str]:
    """Rend les écarts entre la gateway et les deux valeurs qui la visent."""
    ecarts: list[str] = []
    if permitlisten != gateway:
        ecarts.append(
            "permitlisten de la clé de tunnel ne vise pas la gateway : "
            f"{permitlisten} ≠ {gateway}"
        )
    if not rule_present:
        ecarts.append(
            f"règle de pare-feu absente : {PORT}/tcp du sous-réseau vers la gateway"
        )
    return ecarts


def verify(run: Runner) -> tuple[bool | None, list[str]]:
    """Rend `(conforme, écarts)` ; `None` quand une valeur manque."""
    try:
        _, gateway = read_network(run)
        permitlisten = read_permitlisten(run)
    except ProbeError as exc:
        return None, [str(exc)]
    present = firewall_rule_present(run, read_network(run)[0], gateway)
    ecarts = check(gateway, permitlisten, present)
    return not ecarts, ecarts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contrôle de la jonction conteneur → tunnel.")
    parser.add_argument("--json", action="store_true", help="rend le verdict en JSON.")
    args = parser.parse_args(argv)

    conforme, ecarts = verify(default_runner)
    if args.json:
        print(json.dumps({"conforme": conforme, "ecarts": ecarts}, ensure_ascii=False))
    else:
        if conforme is None:
            print("jonction NON VÉRIFIÉE :")
        elif conforme:
            print("jonction conforme.")
        else:
            print("jonction EN ÉCART :")
        for ecart in ecarts:
            print(f"  - {ecart}")

    if conforme is None:
        return EXIT_UNKNOWN
    return EXIT_OK if conforme else EXIT_MISMATCH


if __name__ == "__main__":
    raise SystemExit(main())

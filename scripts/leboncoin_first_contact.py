#!/usr/bin/env python3
"""Une seule requête vers la page de recherche Leboncoin, puis compte rendu.

Sert à constater, sous mandat et une seule fois, si le chemin résidentiel
aboutit à une réponse exploitable. Ce script **n'est pas** le collecteur : il ne
boucle pas, ne réessaie pas, n'écrit rien, n'envoie rien.

Ce qu'il publie : code HTTP, présence du marqueur de challenge, parsing réussi
ou non, nombre d'annonces, durée. Jamais le contenu de la page, ni un titre, ni
un prix, ni une URL d'annonce, ni une donnée de vendeur.

Il réutilise `fetch_search_page` et `parse_search_page` du collecteur : aucune
logique HTTP ni de parsing n'est dupliquée ici.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

PROXY_ENV = "LEBONCOIN_FIRST_CONTACT_PROXY_URL"
EXPECTED_SCHEME = "socks5h"
CHALLENGE_MARKER = "captcha-delivery"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2


class ProxyRefusalError(ValueError):
    """L'URL de proxy est absente ou n'utilise pas le schéma attendu."""


@dataclass
class Rapport:
    """Ce qui est publié : des faits sur l'échange, jamais son contenu."""

    code_http: int | None = None
    challenge: bool = False
    parsing: str = "non_tente"
    annonces: int | None = None
    duree_s: float | None = None
    categorie: str = "inconnu"
    detail: str = ""
    lignes: list[str] = field(default_factory=list)


def valider_proxy(brut: str | None) -> str:
    """Rend l'URL de proxy, ou refuse si elle n'est pas en `socks5h://`.

    `socks5://` ferait résoudre le nom par le client, donc depuis le conteneur :
    la requête ne sortirait pas par le relais de la même façon, et le test ne
    prouverait plus ce qu'il prétend prouver.
    """
    if not brut or not brut.strip():
        raise ProxyRefusalError(
            f"{PROXY_ENV} absent : fournir l'URL de proxy au lancement, jamais "
            "depuis le .env ni la configuration de production."
        )
    analyse = urlsplit(brut.strip())
    if analyse.scheme != EXPECTED_SCHEME:
        raise ProxyRefusalError(
            f"schéma {analyse.scheme!r} refusé : {EXPECTED_SCHEME}:// est exigé "
            "(résolution du nom côté relais)."
        )
    if not analyse.hostname:
        raise ProxyRefusalError("URL de proxy sans hôte.")
    return brut.strip()


def decrire_proxy(url: str) -> str:
    """Décrit le proxy sans divulguer son adresse."""
    analyse = urlsplit(url)
    return f"{analyse.scheme}://<hôte masqué>:{analyse.port or 'défaut'}"


def code_http_depuis_erreur(exc: BaseException) -> int | None:
    """Extrait le code HTTP d'une erreur typée du scraper, sans son message.

    `fetch_search_page` lève `LeboncoinBlockError("HTTP 403")` ou
    `LeboncoinCollectionError("HTTP 5xx")` : le code est la seule information de
    la page qui peut être publiée, et il ne peut pas contenir de contenu.
    """
    texte = str(exc).strip()
    if not texte.startswith("HTTP "):
        return None
    chiffres = texte[5:].strip()
    return int(chiffres) if chiffres.isdigit() else None


def categorie_depuis_erreur(exc: BaseException) -> str:
    """Classe l'échec sans reprendre un message qui pourrait contenir du contenu."""
    nom = type(exc).__name__
    if nom == "LeboncoinBlockError":
        return "blocage"
    if nom == "LeboncoinCollectionError":
        return "echec_reseau"
    return "echec_inattendu"


def executer(proxy_url: str, *, fetch: object, parse: object) -> Rapport:
    """Joue l'unique requête et rend le rapport. Aucune seconde tentative."""
    rapport = Rapport()
    debut = time.monotonic()
    try:
        html = fetch(proxy_url=proxy_url)  # type: ignore[operator]
    except Exception as exc:
        rapport.duree_s = round(time.monotonic() - debut, 2)
        rapport.categorie = categorie_depuis_erreur(exc)
        rapport.code_http = code_http_depuis_erreur(exc)
        rapport.detail = type(exc).__name__
        return rapport
    rapport.duree_s = round(time.monotonic() - debut, 2)
    rapport.categorie = "reponse"
    rapport.code_http = 200

    head = html[:2000] if isinstance(html, str) else ""
    rapport.challenge = CHALLENGE_MARKER in head
    if rapport.challenge:
        rapport.categorie = "challenge"

    try:
        annonces = parse(html)  # type: ignore[operator]
    except Exception as exc:
        rapport.parsing = "echoue"
        rapport.detail = type(exc).__name__
        return rapport

    rapport.parsing = "reussi"
    rapport.annonces = len(annonces)
    if rapport.categorie != "challenge" and rapport.annonces == 0:
        rapport.categorie = "zero_annonce"
    return rapport


def rendre(rapport: Rapport, *, dry_run: bool) -> list[str]:
    """Rend les lignes publiables, sans aucune donnée de page."""
    lignes = []
    if dry_run:
        lignes.append("mode=simulation (aucune requête émise)")
        return lignes
    lignes.append(f"code_http={rapport.code_http if rapport.code_http is not None else 'n/a'}")
    lignes.append(f"categorie={rapport.categorie}")
    lignes.append(f"challenge={str(rapport.challenge).lower()}")
    lignes.append(f"parsing={rapport.parsing}")
    lignes.append(
        "annonces="
        + (str(rapport.annonces) if rapport.annonces is not None else "n/a")
    )
    lignes.append(
        "duree_s=" + (str(rapport.duree_s) if rapport.duree_s is not None else "n/a")
    )
    if rapport.detail:
        lignes.append(f"detail={rapport.detail}")
    return lignes


def main(argv: list[str] | None = None, *, fetch: object = None, parse: object = None) -> int:
    parser = argparse.ArgumentParser(
        description="Une seule requête vers la recherche Leboncoin, compte rendu borné."
    )
    parser.add_argument(
        "--execute-once",
        action="store_true",
        help="émet réellement l'unique requête ; sans cette option, rien ne sort.",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help=f"URL de proxy socks5h:// (défaut : ${PROXY_ENV}).",
    )
    args = parser.parse_args(argv)

    brut = args.proxy if args.proxy is not None else os.environ.get(PROXY_ENV)
    try:
        proxy_url = valider_proxy(brut)
    except ProxyRefusalError as exc:
        print(f"refus: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if not args.execute_once:
        print("mode=simulation (aucune requête émise)")
        print(f"proxy={decrire_proxy(proxy_url)}")
        print("cible=URL de recherche par défaut du collecteur")
        print("action=une requête, sans boucle, sans nouvelle tentative")
        print("il faut --execute-once pour émettre la requête.")
        return EXIT_OK

    if fetch is None or parse is None:
        from app.services.leboncoin_parsing import parse_search_page
        from app.services.leboncoin_scraper import fetch_search_page

        fetch = fetch_search_page
        parse = parse_search_page

    rapport = executer(proxy_url, fetch=fetch, parse=parse)
    for ligne in rendre(rapport, dry_run=False):
        print(ligne)

    if rapport.categorie in {"blocage", "challenge"}:
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

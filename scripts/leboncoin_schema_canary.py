#!/usr/bin/env python3
"""Canari de schéma du flux Leboncoin, exécutable hors du conteneur.

Signale une page de recherche dont la structure a changé avant que le parsing
ne se dégrade silencieusement. Aucun réseau si `--html` est fourni.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.constants import LEBONCOIN_SEARCH_URL as DEFAULT_SEARCH_URL  # noqa: E402
from app.services.leboncoin_parsing import (  # noqa: E402
    NEXT_DATA_PATTERN,
    LeboncoinParseError,
    parse_search_page,
)

DEFAULT_URL = DEFAULT_SEARCH_URL
REQUIRED_AD_FIELDS = ("list_id", "subject", "url", "price", "first_publication_date")
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
SAMPLE_SIZE = 10


def _sample(ads: list[Any]) -> list[dict[str, Any]]:
    return [ad for ad in ads[:SAMPLE_SIZE] if isinstance(ad, dict)]


def validate_payload(payload: Any) -> list[str]:
    """Liste les écarts de structure d'un `__NEXT_DATA__` de recherche."""
    if not isinstance(payload, dict):
        return ["__NEXT_DATA__ n'est pas un objet"]
    page_props = (payload.get("props") or {}).get("pageProps") or {}
    search_data = page_props.get("searchData")
    if not isinstance(search_data, dict):
        return ["searchData absent"]

    problems: list[str] = []
    if not isinstance(search_data.get("total"), int):
        problems.append("total absent ou non entier")
    ads = search_data.get("ads")
    if not isinstance(ads, list) or not ads:
        return problems + ["ads absent ou vide"]
    sample = _sample(ads)
    if not sample:
        return problems + ["aucune annonce objet"]

    for ad in sample:
        missing = [field for field in REQUIRED_AD_FIELDS if field not in ad]
        if missing:
            problems.append("champs absents: " + ", ".join(missing))
            break
    for ad in sample:
        price = ad.get("price")
        is_list = isinstance(price, list)
        is_int = isinstance(price, int) and not isinstance(price, bool)
        if not (is_list or is_int):
            problems.append("price de type inattendu")
            break
    for ad in sample:
        value = ad.get("first_publication_date")
        if not isinstance(value, str):
            problems.append("first_publication_date absente")
            break
        try:
            datetime.strptime(value, DATE_FORMAT)
        except ValueError:
            problems.append("first_publication_date hors format")
            break
    if not any(isinstance(ad.get("images"), dict) and ad["images"].get("urls") for ad in sample):
        problems.append("images.urls absentes")
    if not any(
        isinstance(ad.get("attributes"), list)
        and any(
            isinstance(entry, dict) and entry.get("key") == "condition"
            for entry in ad["attributes"]
        )
        for ad in sample
    ):
        problems.append("attribut condition absent")
    return problems


def validate_html(html: str) -> list[str]:
    """Valide une page complète : structure, puis parsing réel."""
    match = NEXT_DATA_PATTERN.search(html)
    if match is None:
        return ["__NEXT_DATA__ absent"]
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ["__NEXT_DATA__ illisible"]

    problems = validate_payload(payload)
    try:
        items = parse_search_page(html)
    except LeboncoinParseError as exc:
        return problems + [str(exc)]
    if not items:
        problems.append("aucune annonce exploitable")
    return problems


def fetch(url: str, proxy: str | None, timeout: float) -> tuple[int, str]:
    from curl_cffi import requests

    response = requests.get(url, impersonate="chrome", proxy=proxy, timeout=timeout)
    return response.status_code, response.text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canari de schéma Leboncoin.")
    parser.add_argument("--url", default=os.environ.get("LEBONCOIN_SEARCH_URL", DEFAULT_URL))
    parser.add_argument("--proxy", default=os.environ.get("LEBONCOIN_PROXY_URL"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--html", help="fichier HTML local, aucun réseau")
    args = parser.parse_args(argv)

    if args.html:
        status: int | None = None
        html = Path(args.html).read_text(encoding="utf-8")
    else:
        try:
            status, html = fetch(args.url, args.proxy, args.timeout)
        except Exception as exc:
            report = {
                "ok": False,
                "problems": [f"transport: {type(exc).__name__}"],
                "status": None,
            }
            print(json.dumps(report))
            return 2
        if status != 200:
            print(json.dumps({"ok": False, "problems": [f"statut {status}"], "status": status}))
            return 2

    problems = validate_html(html)
    report = {
        "ok": not problems,
        "problems": problems,
        "status": status,
        "bytes": len(html),
        "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest()[:16],
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())

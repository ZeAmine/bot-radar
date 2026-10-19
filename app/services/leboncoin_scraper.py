"""Collecte HTTP Leboncoin : empreinte TLS de navigateur, sortie résidentielle.

DataDome refuse la signature TLS de `httpx` depuis une IP résidentielle comme
depuis un datacenter (mesuré : 403 dans les deux cas). `curl_cffi` imite Chrome
et passe ; la sortie doit rester résidentielle, donc un proxy SOCKS local
(alimenté par le tunnel du Pi) est attendu en production.
"""

import logging
from typing import Any

logger = logging.getLogger("radar.leboncoin")

DEFAULT_SEARCH_URL = (
    "https://www.leboncoin.fr/recherche?category=27&leisure_book_genre=comics%26mangas"
)

REQUEST_HEADERS: dict[str, str] = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "fr-FR,fr;q=0.9",
    "referer": "https://www.leboncoin.fr/",
}

BLOCK_STATUS_CODES = frozenset({403, 429})

_session: Any | None = None
_session_proxy: str | None = None


class LeboncoinBlockError(RuntimeError):
    """DataDome a refusé la requête (ou renvoyé un captcha)."""


class LeboncoinCollectionError(RuntimeError):
    """La page de recherche n'a pas pu être obtenue."""


def _get_session(proxy_url: str | None) -> Any:
    """Session curl_cffi réutilisée : connexion TLS et cookies conservés."""
    global _session, _session_proxy
    if _session is None or _session_proxy != proxy_url:
        from curl_cffi import requests as curl_requests

        _session = curl_requests.Session(
            impersonate="chrome",
            timeout=30.0,
            proxy=proxy_url or None,
        )
        _session_proxy = proxy_url
    return _session


def reset_session() -> None:
    """Oublie la session courante (tests et reprise après blocage)."""
    global _session, _session_proxy
    _session = None
    _session_proxy = None


def fetch_search_page(
    url: str = DEFAULT_SEARCH_URL,
    proxy_url: str | None = None,
) -> str:
    """Récupère le HTML d'une page de recherche, ou lève une erreur typée."""
    try:
        session = _get_session(proxy_url)
    except ImportError as exc:
        raise LeboncoinCollectionError("curl_cffi indisponible") from exc

    try:
        response = session.get(url, headers=REQUEST_HEADERS)
    except Exception as exc:
        reset_session()
        raise LeboncoinCollectionError(type(exc).__name__) from None

    body_head = response.text[:2000]
    if response.status_code in BLOCK_STATUS_CODES or "captcha-delivery" in body_head:
        raise LeboncoinBlockError(f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise LeboncoinCollectionError(f"HTTP {response.status_code}")
    html = response.text
    if not isinstance(html, str):
        raise LeboncoinCollectionError("réponse non textuelle")
    return html

"""Collecte HTTP Leboncoin : empreinte TLS de navigateur, sortie résidentielle.

DataDome refuse la signature TLS de `httpx` depuis une IP résidentielle comme
depuis un datacenter (mesuré : 403 dans les deux cas). `curl_cffi` imite Chrome
et passe ; la sortie doit rester résidentielle, donc un proxy SOCKS local
(alimenté par le tunnel du Pi) est attendu en production.
"""

import logging
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.constants import LEBONCOIN_SEARCH_URL

logger = logging.getLogger("radar.leboncoin")

DEFAULT_SEARCH_URL = LEBONCOIN_SEARCH_URL

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


COLLECT_PHASES = frozenset({"ProxyError", "ConnectTimeout", "ReadTimeout", "autre"})


def collection_phase(exc: BaseException) -> str:
    """Phase approchée d'un échec, déduite du nom de la classe levée par curl_cffi."""
    name = type(exc).__name__
    if name in {"ProxyError", "ConnectTimeout", "ReadTimeout"}:
        return name
    if "Timeout" in name:
        return "ReadTimeout"
    if "Proxy" in name:
        return "ProxyError"
    return "autre"


class LeboncoinCollectionError(RuntimeError):
    """La page de recherche n'a pas pu être obtenue."""

    def __init__(
        self,
        motif: str,
        *,
        phase: str = "autre",
        exception_class: str | None = None,
        duration_ms: int | None = None,
        session_reused: bool | None = None,
    ) -> None:
        super().__init__(motif)
        self.phase = phase if phase in COLLECT_PHASES else "autre"
        self.exception_class = exception_class
        self.duration_ms = duration_ms
        self.session_reused = session_reused


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


def _with_page(url: str, page: int) -> str:
    """Ajoute ou remplace le paramètre `page` sans perdre l'encodage des filtres."""
    if page <= 1:
        return url
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "page"
    ]
    query.append(("page", str(page)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def fetch_search_page(
    url: str = DEFAULT_SEARCH_URL,
    proxy_url: str | None = None,
    page: int = 1,
) -> str:
    """Récupère le HTML d'une page de recherche, ou lève une erreur typée."""
    target = _with_page(url, page)
    session_reused = _session is not None and _session_proxy == proxy_url
    try:
        session = _get_session(proxy_url)
    except ImportError as exc:
        raise LeboncoinCollectionError("curl_cffi indisponible") from exc

    started = time.monotonic()
    try:
        response = session.get(target, headers=REQUEST_HEADERS)
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        phase = collection_phase(exc)
        reset_session()
        logger.warning(
            "Échec de collecte Leboncoin",
            extra={
                "event": "leboncoin_collect_failed",
                "exception_class": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "phase": phase,
                "duration_ms": duration_ms,
                "session_reused": session_reused,
                "page": page,
            },
        )
        raise LeboncoinCollectionError(
            type(exc).__name__,
            phase=phase,
            exception_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            duration_ms=duration_ms,
            session_reused=session_reused,
        ) from None

    body_head = response.text[:2000]
    if response.status_code in BLOCK_STATUS_CODES or "captcha-delivery" in body_head:
        raise LeboncoinBlockError(f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise LeboncoinCollectionError(f"HTTP {response.status_code}")
    html = response.text
    if not isinstance(html, str):
        raise LeboncoinCollectionError("réponse non textuelle")
    return html

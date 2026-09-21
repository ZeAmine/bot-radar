"""Gestionnaire singleton de client HTTP asynchrone partagé avec pool de connexions."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx

logger = logging.getLogger("radar.http_client")

_shared_client: httpx.AsyncClient | None = None


def get_shared_http_client() -> httpx.AsyncClient:
    """Retourne l'instance partagée du client HTTP asynchrone (initialisation paresseuse)."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        limits = httpx.Limits(
            max_keepalive_connections=20,
            max_connections=50,
            keepalive_expiry=30.0,
        )
        timeout = httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0)
        _shared_client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            follow_redirects=True,
        )
        logger.info("Client HTTP partagé initialisé avec pool de connexions keep-alive.")
    return _shared_client


async def close_shared_http_client() -> None:
    """Ferme proprement le client HTTP partagé et libère tous les sockets ouverts."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        try:
            await _shared_client.aclose()
        except RuntimeError:
            pass
        finally:
            _shared_client = None
        logger.info("Client HTTP partagé fermé avec succès.")


@asynccontextmanager
async def http_client_lifespan() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Gestionnaire de contexte pour le cycle de vie applicatif ou les workers."""
    client = get_shared_http_client()
    try:
        yield client
    finally:
        await close_shared_http_client()

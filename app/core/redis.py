"""Gestionnaire singleton de connexion et pool Redis."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger("radar.redis")

_redis_pool: aioredis.ConnectionPool | None = None
_redis_client: aioredis.Redis | None = None


def get_redis_pool() -> aioredis.ConnectionPool:
    """Fournit le pool de connexions Redis partagé (initialisation paresseuse)."""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
    return _redis_pool


def get_redis_client() -> aioredis.Redis:
    """Fournit un client Redis réutilisable connecté au pool partagé."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis(connection_pool=get_redis_pool())
    return _redis_client


@asynccontextmanager
async def get_redis_session() -> AsyncGenerator[aioredis.Redis, None]:
    """Gestionnaire de contexte fournissant le client Redis avec gestion d'erreurs."""
    client = get_redis_client()
    try:
        yield client
    except Exception as exc:
        logger.debug("Exception lors d'une opération Redis : %s", exc)
        raise


async def close_redis() -> None:
    """Ferme proprement le client et le pool de connexions Redis."""
    global _redis_client, _redis_pool
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
    if _redis_pool is not None:
        await _redis_pool.disconnect()
        _redis_pool = None
    logger.info("Connexions Redis fermées avec succès.")

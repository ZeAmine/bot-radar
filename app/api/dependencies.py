"""Dépendances FastAPI (DB, Redis, Authentification API)."""

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import verify_ingest_token, verify_internal_token
from app.db.session import get_db_session

security_bearer = HTTPBearer(auto_error=True)

# Pool Redis réutilisable
_redis_pool: aioredis.ConnectionPool | None = None


def get_redis_pool() -> aioredis.ConnectionPool:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
    return _redis_pool


async def get_redis_client() -> AsyncGenerator[aioredis.Redis, None]:
    """Fournit une connexion Redis asynchrone."""
    pool = get_redis_pool()
    client = aioredis.Redis(connection_pool=pool)
    try:
        yield client
    finally:
        await client.aclose()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Fournit une session de base de données asynchrone."""
    async for session in get_db_session():
        yield session


def require_ingest_token(
    credentials: HTTPAuthorizationCredentials = Security(security_bearer),
) -> str:
    """Valide le token Bearer pour l'ingestion d'annonces."""
    token = credentials.credentials
    if not verify_ingest_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token d'ingestion invalide ou manquant.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def require_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security_bearer),
) -> str:
    """Valide le token Bearer pour les endpoints internes."""
    token = credentials.credentials
    if not verify_internal_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token interne invalide ou manquant.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token

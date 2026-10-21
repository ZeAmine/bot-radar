"""Fixture du Redis de test jetable.

Le régulateur CAL prend toutes ses décisions par script Lua : un double mémoire
ne prouverait rien de l'atomicité. Les tests qui en dépendent sont donc ignorés
quand aucune instance n'est fournie, et ne sont jamais présentés comme une
preuve de sérialisation Redis.
"""

import os
import uuid

import pytest

CAL_TEST_REDIS_ENV = "CAL_TEST_REDIS_URL"


def redis_test_url() -> str | None:
    value = os.environ.get(CAL_TEST_REDIS_ENV, "").strip()
    return value or None


@pytest.fixture
async def cal_redis():
    """Client Redis isolé : chaque test travaille dans son propre espace de clés."""
    import redis.asyncio as aioredis

    url = redis_test_url()
    if url is None:
        pytest.skip(f"{CAL_TEST_REDIS_ENV} absent : test Redis non exécuté")
    try:
        client = aioredis.from_url(url)
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis de test injoignable ({type(exc).__name__})")
    try:
        import sys
        if os.environ.get("CAL_DEBUG_STORE"):
            print("FIXTURE TYPE:", type(client), file=sys.stderr)
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def cal_namespace() -> str:
    return f"test-{uuid.uuid4().hex}"

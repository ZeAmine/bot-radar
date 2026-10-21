"""Fixture du Redis de test jetable.

Le régulateur CAL prend toutes ses décisions par script Lua : un double mémoire
ne prouverait rien de l'atomicité. Les tests qui en dépendent sont donc ignorés
quand aucune instance n'est fournie, et ne sont jamais présentés comme une
preuve de sérialisation Redis.

L'instance fournie peut être partagée : la fixture ne vide jamais la base et ne
supprime que les clés portant l'espace de noms du test.
"""

import os
import uuid

import pytest

CAL_TEST_REDIS_ENV = "CAL_TEST_REDIS_URL"
KEY_PREFIX = "radar:cal:governor"


def redis_test_url() -> str | None:
    value = os.environ.get(CAL_TEST_REDIS_ENV, "").strip()
    return value or None


def governor_key_pattern(namespace: str) -> str:
    return f"{KEY_PREFIX}:{namespace}*"


@pytest.fixture
async def cal_redis():
    """Client Redis isolé : chaque test travaille dans son propre espace de clés."""
    import redis.asyncio as aioredis

    url = redis_test_url()
    if url is None:
        pytest.skip(f"{CAL_TEST_REDIS_ENV} absent : test Redis non exécuté")
    client = aioredis.from_url(url)
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        pytest.fail(
            f"{CAL_TEST_REDIS_ENV} fourni mais Redis de test injoignable "
            f"({type(exc).__name__})",
            pytrace=False,
        )
    yield client
    await client.aclose()


@pytest.fixture
async def clean_cal_keys(cal_redis, cal_namespace):
    """Supprime uniquement les clés du test, à l'entrée et à la sortie."""
    pattern = governor_key_pattern(cal_namespace)
    await _delete_matching(cal_redis, pattern)
    yield cal_redis
    await _delete_matching(cal_redis, pattern)


async def _delete_matching(client, pattern: str) -> None:
    pending = [key async for key in client.scan_iter(match=pattern, count=100)]
    if pending:
        await client.delete(*pending)


@pytest.fixture
def cal_namespace() -> str:
    return f"test-{uuid.uuid4().hex}"

"""La base de test, en un seul endroit : moteur, schéma, session, fabrique.

Chaque suite qui joue de vraies lignes recopiait le même enchaînement — moteur, schéma,
`async_sessionmaker(expire_on_commit=False)`, session, libération. Dix copies, dix
occasions de dériver. Il vit ici **une fois** ; chaque fichier de test n'y garde que son
*semis*, qui est son sujet.

Deux saveurs, et l'écart est réel. La CI rejoue `tests/integration/` sur PostgreSQL (job
`test-postgres`, `TEST_DATABASE_URL` posée) : cette suite doit donc accepter la base
qu'on lui donne, sans quoi les requêtes JSON, les upserts et les types natifs
divergeraient entre les deux dialectes sans que rien ne le dise. `tests/unit/` et
`tests/performance/` ne sont rejoués nulle part ailleurs : ils gardent SQLite en
mémoire, où le poste de développement et la CI voient la même chose, et où les budgets
de `tests/performance/` ne dépendent d'aucun serveur. `replayed_on_postgres=True` est
donc une déclaration — « la CI me rejoue sur l'autre dialecte » —, pas un réglage.

Un module partagé, pas un module de test importé par un autre : ce fichier ne collecte
rien (`python_files = ["test_*.py"]`) et ne doit rien savoir des tests qui l'utilisent.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base

SQLITE_MEMORY_URL = "sqlite+aiosqlite:///:memory:"


@asynccontextmanager
async def fresh_database(
    *,
    replayed_on_postgres: bool,
) -> AsyncIterator[tuple[AsyncSession, async_sessionmaker[AsyncSession]]]:
    """Une base neuve, son schéma posé, une session ouverte et sa fabrique.

    Le schéma est effacé avant d'être créé : la base PostgreSQL partagée de la CI est
    persistante, et deux tests consécutifs y recréeraient les mêmes catégories ou le
    même `telegram_user_id` en violant une contrainte unique. En mémoire, la base est
    neuve par construction et `drop_all` n'y fait rien.

    La fabrique est rendue avec la session parce que le semis n'est pas toujours le
    sujet : un test qui doit relire dans une session n'ayant rien partagé avec celle
    qui écrit ouvre la sienne.
    """
    url = (
        os.environ.get("TEST_DATABASE_URL", SQLITE_MEMORY_URL)
        if replayed_on_postgres
        else SQLITE_MEMORY_URL
    )
    engine = create_async_engine(url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with maker() as session:
            yield session, maker
    finally:
        await engine.dispose()

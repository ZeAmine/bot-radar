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
de `tests/performance/` ne dépendent d'aucun serveur. Le nom de la fixture porte cette
déclaration — « la CI me rejoue sur l'autre dialecte » ou « je ne joue que sur SQLite »
—, pas un réglage à ajuster au cas par cas.

Un module partagé, pas un module de test importé par un autre : ce fichier ne collecte
rien (`python_files = ["test_*.py"]`) et ne doit rien savoir des tests qui l'utilisent.
Il est aussi le **seul** du dépôt à créer un moteur SQLAlchemy ou un schéma de test
(`tests/unit/test_database_owner.py` le verrouille).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base

SQLITE_MEMORY_URL = "sqlite+aiosqlite:///:memory:"


class FreshDatabase(NamedTuple):
    """Une base neuve : la session ouverte, et la fabrique pour en ouvrir d'autres.

    La fabrique accompagne la session parce que le semis n'est pas toujours le sujet :
    un test qui doit relire dans une session n'ayant rien partagé avec celle qui écrit
    ouvre la sienne. L'appelant ne nomme que ce qu'il utilise.
    """

    session: AsyncSession
    maker: async_sessionmaker[AsyncSession]


@asynccontextmanager
async def _fresh_database(url: str) -> AsyncIterator[FreshDatabase]:
    """Une base neuve, son schéma posé, une session ouverte et sa fabrique.

    Le schéma est effacé avant d'être créé : la base PostgreSQL partagée de la CI est
    persistante, et deux tests consécutifs y recréeraient les mêmes catégories ou le
    même `telegram_user_id` en violant une contrainte unique. En mémoire, la base est
    neuve par construction et `drop_all` n'y fait rien.
    """
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
            yield FreshDatabase(session, maker)
    finally:
        await engine.dispose()


@asynccontextmanager
async def postgres_replayed_database() -> AsyncIterator[FreshDatabase]:
    """Saveur « rejouée par la CI » : la suite accepte `TEST_DATABASE_URL`.

    Pour `tests/integration/`, la seule suite que le job `test-postgres` rejoue sur
    l'autre dialecte. Sans cette honnêteté, les requêtes JSON, les upserts et les types
    natifs divergeraient entre SQLite et PostgreSQL sans que rien ne le dise.
    """
    url = os.environ.get("TEST_DATABASE_URL", SQLITE_MEMORY_URL)
    async with _fresh_database(url) as fresh:
        yield fresh


@asynccontextmanager
async def sqlite_only_database() -> AsyncIterator[FreshDatabase]:
    """Saveur « SQLite seul » : la suite n'est rejouée nulle part ailleurs.

    Pour `tests/unit/` et `tests/performance/` : le poste de développement et la CI
    voient la même base, et les budgets de `tests/performance/` ne dépendent d'aucun
    serveur. Une suite unitaire qui honorerait `TEST_DATABASE_URL` ferait courir ses
    assertions sur une base que la CI ne lui offrira jamais.
    """
    async with _fresh_database(SQLITE_MEMORY_URL) as fresh:
        yield fresh

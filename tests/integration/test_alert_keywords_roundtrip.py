"""Les mots-clés d'une alerte, écrits puis relus **pour de vrai**.

Les tests unitaires de `keyword_hygiene` travaillent sur des listes en mémoire, et
les tests d'intégration qui créent une alerte passent par l'API : entre les deux,
personne n'exerce l'aller-retour. C'est là que les mots-clés se perdent — pas dans
la règle, mais entre la règle et la colonne.

Le symptôme vu en production était exactement celui-là : six exclusions
qualifiées par le titre sur l'alerte « Deep It » disparaissaient sans laisser de
trace. Aucun test ne le voyait, parce que tous les tests s'arrêtaient au moment
juste avant l'écriture.

Ces tests jouent donc les trois gestes du chemin réel — la règle, puis le
repository, puis le commit — et relisent dans une **session neuve** : ce qui est
lu vient de la base, pas de l'état Python qui vient d'écrire. Un `JSONB` qui
tronque, une colonne mal typée ou un commit absent se verraient ici.

Sur SQLite comme sur PostgreSQL : la colonne est un `JSON` avec une variante
`JSONB`, donc les deux chemins s'exercent.
"""

import os
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.constants import CategorySlug
from app.db.base import Base
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.domain.rules.keyword_hygiene import sanitize_exclusions, sanitize_variants

pytestmark = pytest.mark.asyncio

TEST_TELEGRAM_USER_ID = 424242


@pytest.fixture
async def session_maker() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Fabrique de sessions sur une base neuve : PostgreSQL si demandée, sinon SQLite.

    Elle rend une **fabrique** et non une session : le test doit relire dans une
    session qui n'a rien partagé avec celle qui écrit, sinon il ne prouve que
    Python.
    """
    engine = create_async_engine(
        os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    )
    async with engine.begin() as conn:
        # La base PostgreSQL de CI est partagée et persistante : sans cette remise à
        # zéro, l'unicité de `telegram_user_id` et de `slug` viole au deuxième test.
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    # L'identité de l'alerte est posée par une session qui se ferme ensuite.
    async with maker() as seeding:
        seeding.add(
            User(telegram_user_id=TEST_TELEGRAM_USER_ID, is_allowed=True),
        )
        seeding.add(
            Category(
                slug=CategorySlug.BD_COMICS.value,
                label="BD & Comics",
                emoji="📚",
                is_active=True,
            ),
        )
        await seeding.commit()

    yield maker

    await engine.dispose()


async def _write_alert(
    session: AsyncSession,
    *,
    name: str,
    exclude_keywords: list[str],
    variants: list[str] | None = None,
) -> uuid.UUID:
    """Écrit une alerte comme le fait `/add` : la règle d'abord, le repository ensuite."""
    user = await session.scalar(
        select(User).where(User.telegram_user_id == TEST_TELEGRAM_USER_ID)
    )
    category = await session.scalar(
        select(Category).where(Category.slug == CategorySlug.BD_COMICS.value)
    )
    # L'absence d'utilisateur ou de catégorie est un échec du semis, pas une
    # raison de laisser passer un None en cascade jusqu'à la colonne.
    assert user is not None
    assert category is not None

    alert = await AlertRepository(session).create(
        user_id=user.id,
        category_id=category.id,
        name=name,
        include_keywords=[name],
        exclude_keywords=sanitize_exclusions(exclude_keywords, name),
        variants=sanitize_variants(variants or [], name),
    )
    await session.commit()
    return alert.id


async def test_an_exclusion_qualified_by_the_title_survives_the_database(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """« deep it coffret » ne contient pas le titre : il doit être relu tel quel.

    Le qualificatif de l'exemple était « deep it tome 1 » : depuis l'ADR-029, un
    qualificatif qui n'est qu'un **numéro de tome** ajouté au titre est retiré, parce
    qu'il ferait taire le tome visé. La forme décrite ici est donc « deep it tomes »,
    sans numéro — celle qu'ADR-027 protège et qu'ADR-029 laisse intacte.
    """
    name = "Deep It"
    # Cas réel de l'incident : des qualificatifs qui éliminent des lots entiers, à
    # côté des termes que la règle refuse — un chiffre nu et un caractère seul.
    # « hs », lui, est volontairement gardé : une exclusion de deux lettres tranche
    # encore quelque chose, et la règle le dit.
    exclusions = [
        "deep it coffret",
        "deep it intégrale",
        "deep it tomes",
        "hs",
        "1",
        "x",
    ]

    async with session_maker() as write_session:
        alert_id = await _write_alert(
            write_session, name=name, exclude_keywords=exclusions
        )

    async with session_maker() as read_session:
        stored = await AlertRepository(read_session).get_by_id(alert_id)

    assert stored is not None
    # Dans l'ordre d'écriture : la colonne rend une liste, pas un ensemble trié.
    assert stored.exclude_keywords == [
        "deep it coffret",
        "deep it intégrale",
        "deep it tomes",
        "hs",
    ]


async def test_an_exclusion_that_only_adds_a_volume_number_is_never_stored(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Le titre plus un numéro n'atteint pas la base : il ferait taire le tome visé.

    Forme relevée en production sur `/add Absolute Martian Manhunter, 10€` : le prompt
    demandait « 3 à 6 » exclusions, donc le moteur a collé un numéro au titre. La règle
    tombe à l'écriture ; ce test vérifie qu'aucune ne survit au chemin réel d'écriture.
    """
    name = "Deep It"
    exclusions = ["deep it #1", "deep it tome 2", "deep it vol 3", "deep it coffret"]

    async with session_maker() as write_session:
        alert_id = await _write_alert(
            write_session, name=name, exclude_keywords=exclusions
        )

    async with session_maker() as read_session:
        stored = await AlertRepository(read_session).get_by_id(alert_id)

    assert stored is not None
    assert stored.exclude_keywords == ["deep it coffret"]


async def test_the_reloaded_exclusions_are_not_cleaned_a_second_time(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Le nettoyage rejoué sur ce qui est relu ne doit plus rien enlever.

    C'est le filet du nettoyage jouable en production : une seconde passe sur des
    exclusions déjà passées par la règle doit être un no-op. Sans relecture en base,
    on ne sait pas que la colonne rend les mêmes termes — et un nouveau passage du
    nettoyeur finirait par les retirer pour de bon.
    """
    name = "Deep It"
    exclusions = ["deep it coffret", "deep it intégrale", "one piece", "1", "x"]

    async with session_maker() as write_session:
        alert_id = await _write_alert(
            write_session, name=name, exclude_keywords=exclusions
        )

    async with session_maker() as read_session:
        stored = await AlertRepository(read_session).get_by_id(alert_id)
        assert stored is not None
        reloaded = list(stored.exclude_keywords)
        # Nettoyeur relu, sur la forme relue de la base.
        assert sanitize_exclusions(list(stored.exclude_keywords), stored.name) == reloaded
        assert sanitize_variants(list(stored.variants), stored.name) == list(stored.variants)


async def test_an_empty_title_does_not_wipe_the_exclusions_on_the_way_in(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Un brouillon sans nom ne décide de rien : la comparaison est une inclusion.

    `'' in terme` est toujours vrai, si bien qu'une garde absente effacerait toutes
    les variantes et toutes les exclusions d'un brouillon sans nom. Le cas est
    verrouillé ici parce que c'est un chemin d'écriture, pas une règle isolée.
    """
    async with session_maker() as write_session:
        alert_id = await _write_alert(
            write_session,
            name="",
            exclude_keywords=["coffret", "intégrale"],
            variants=["coffret", "intégrale"],
        )

    async with session_maker() as read_session:
        stored = await AlertRepository(read_session).get_by_id(alert_id)

    assert stored is not None
    assert sorted(stored.exclude_keywords) == ["coffret", "intégrale"]
    assert sorted(stored.variants) == ["coffret", "intégrale"]
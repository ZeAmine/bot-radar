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

Le même aller-retour couvre le changement d'ISBN depuis « Modifier » : les mots-clés
de l'ouvrage précédent partent dans le commit du handler, et la recherche de fond —
qui ouvre sa **propre** session — écrit ceux du nouveau. Un commit oublié ou une tâche
qui écrit ailleurs ne se verraient nulle part ailleurs : les tests de parcours simulent
ces deux frontières-là.
"""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers.alerts_manage import (
    confirm_isbn_replacement,
    prompt_new_field_value,
    save_edited_field_value,
)
from app.core.constants import CategorySlug
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.domain.rules.keyword_hygiene import sanitize_exclusions, sanitize_variants
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from app.services.book_metadata_service import BookMetadata
from tests.database import postgres_replayed_database

pytestmark = pytest.mark.asyncio

TEST_TELEGRAM_USER_ID = 424242


@pytest.fixture
async def session_maker() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Fabrique de sessions sur une base neuve : PostgreSQL si la CI la rejoue, sinon SQLite.

    Elle rend une **fabrique** et non une session : le test doit relire dans une
    session qui n'a rien partagé avec celle qui écrit, sinon il ne prouve que
    Python.
    """
    async with postgres_replayed_database() as database:
        maker = database.maker
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


async def _write_alert(
    session: AsyncSession,
    *,
    name: str,
    exclude_keywords: list[str],
    variants: list[str] | None = None,
) -> uuid.UUID:
    """Écrit une alerte comme le fait `/add` : la règle d'abord, le repository ensuite."""
    user = await session.scalar(select(User).where(User.telegram_user_id == TEST_TELEGRAM_USER_ID))
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
        alert_id = await _write_alert(write_session, name=name, exclude_keywords=exclusions)

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
        alert_id = await _write_alert(write_session, name=name, exclude_keywords=exclusions)

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
        alert_id = await _write_alert(write_session, name=name, exclude_keywords=exclusions)

    async with session_maker() as read_session:
        stored = await AlertRepository(read_session).get_by_id(alert_id)
        assert stored is not None
        reloaded = list(stored.exclude_keywords)
        # Nettoyeur relu, sur la forme relue de la base.
        assert sanitize_exclusions(list(stored.exclude_keywords), stored.name) == reloaded
        assert sanitize_variants(list(stored.variants), stored.name) == list(stored.variants)


# ---------------------------------------------------------------------------------
# Changer d'ISBN depuis « Modifier » : les mots-clés de l'ouvrage précédent partent,
# ceux du nouveau sont cherchés puis écrits — dans la base, pas seulement en mémoire.
# ---------------------------------------------------------------------------------

ANCIEN_ISBN = "9782369903086"
NOUVEL_ISBN = "9782369904151"


class _EditState:
    """Fausse FSM de l'écran « Modifier » : le champ ISBN ouvert."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.state = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, data: dict | None = None, **kwargs) -> None:
        self.data.update(data or {})
        self.data.update(kwargs)

    async def set_state(self, state) -> None:
        self.state = state

    async def clear(self) -> None:
        self.data = {}


def _capture_task(tasks: list):
    """Remplace `safe_create_task` : la recherche est retenue, puis jouée par le test."""

    def _run(coro, name=None):
        tasks.append((coro, name))
        return None

    return _run


async def _edit_the_isbn(
    session_maker: async_sessionmaker[AsyncSession],
    alert_id: uuid.UUID,
    isbn: str,
    *,
    notice: BookMetadata | None,
    outcome: KeywordVariantsOutcome,
    pricing=None,
) -> AsyncMock:
    """Joue « Modifier → ISBN → enregistrer », puis la recherche de fond.

    Les frontières réseau sont simulées — notice et moteur — mais la base, les
    repositories et les commits sont les vrais : l'ordre des gestes est déjà verrouillé
    par les tests de parcours, ce qui se vérifie ici est l'écriture.
    """
    tasks: list = []
    state = _EditState()
    card_message = AsyncMock(spec=Message)
    card_message.answer = AsyncMock(return_value=card_message)
    card_message.edit_text = AsyncMock()
    card_message.edit_reply_markup = AsyncMock()
    message = AsyncMock()
    message.answer = AsyncMock(return_value=card_message)
    callback = AsyncMock()
    callback.data = f"ed_f:{alert_id}:isbn"
    callback.message = AsyncMock()

    async with session_maker() as session:
        user = await session.scalar(
            select(User).where(User.telegram_user_id == TEST_TELEGRAM_USER_ID)
        )
        assert user is not None
        with (
            # La tâche de fond ouvre sa propre session : elle doit écrire dans la base
            # du test, et non dans celle d'un processus sans base.
            patch("app.bot.handlers.alerts_manage.AsyncSessionLocal", new=session_maker),
            # Ici la recherche doit vraiment **tourner** : elle est retenue, puis jouée,
            # comme la boucle d'événements le ferait après le retour du handler.
            patch(
                "app.bot.handlers.alerts_manage.safe_create_task",
                new=_capture_task(tasks),
            ),
            patch("app.bot.handlers.alerts_manage.price_book_now", new=pricing or AsyncMock()),
            patch(
                "app.bot.handlers.alerts_manage.keyword_variants_module."
                "generate_variants_and_exclusions",
                new=AsyncMock(return_value=outcome),
            ),
            patch(
                "app.services.book_metadata_service.BookMetadataService.lookup_by_isbn",
                new=AsyncMock(return_value=notice),
            ),
        ):
            await prompt_new_field_value(callback, state=state, db_user=user, db_session=session)
            message.text = isbn
            await save_edited_field_value(message, state=state, db_user=user, db_session=session)
            pending = (await state.get_data()).get("pending_isbn")
            if pending:
                callback.data = f"ed_isbn:yes:{pending['token']}"
                callback.message = card_message
                await confirm_isbn_replacement(callback, state, user, session)
            for coro, _name in tasks:
                await coro

    card_message.sent_texts = [call.args[0] for call in message.answer.call_args_list] + [
        call.args[0] for call in card_message.answer.call_args_list
    ]
    return card_message


async def test_changing_the_isbn_replaces_the_keywords_left_by_the_previous_work(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """L'ancien ouvrage avait laissé ses mots-clés : le nouveau les remplace, en base.

    Le défaut tenait à un commit oublié ou à une tâche écrivant ailleurs aussi sûrement
    qu'à un ordre de gestes inversé. La valeur écrite est donc relue dans une session
    neuve : ce qui est lu vient de la colonne.
    """
    async with session_maker() as write_session:
        alert_id = await _write_alert(
            write_session,
            name="La couleur des choses",
            exclude_keywords=["coffret"],
            variants=["couleurdeschose"],
        )
        alert = await AlertRepository(write_session).get_by_id(alert_id)
        assert alert is not None
        alert.isbn = ANCIEN_ISBN
        await write_session.commit()

    await _edit_the_isbn(
        session_maker,
        alert_id,
        NOUVEL_ISBN,
        notice=BookMetadata(
            title="La couleur des choses",
            author="Martin Panchaud",
            publisher="Editions çà et là",
            isbn13=NOUVEL_ISBN,
        ),
        outcome=KeywordVariantsOutcome(
            variants=["couleurdeschoses"],
            exclusions=["keeping two"],
            answered=True,
        ),
    )

    async with session_maker() as read_session:
        stored = await AlertRepository(read_session).get_by_id(alert_id)

    assert stored is not None
    assert stored.isbn == NOUVEL_ISBN
    assert stored.variants == ["couleurdeschoses"]
    assert stored.exclude_keywords == ["keeping two"]
    assert "search_text=" + NOUVEL_ISBN in stored.vinted_search_url


async def test_a_new_isbn_that_nobody_knows_leaves_the_alert_mute_and_says_so(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Aucune notice, moteur muet : l'alerte n'a plus aucun mot-clé et le dit.

    L'ISBN saisi est gardé — personne ne prouve qu'il désigne un autre ouvrage —, les
    mots-clés de l'ancien partent, et le silence du moteur est annoncé à l'utilisateur
    au lieu de se lire comme un avis rendu (ADR-005).
    """
    async with session_maker() as write_session:
        alert_id = await _write_alert(
            write_session,
            name="La couleur des choses",
            exclude_keywords=["coffret"],
            variants=["couleurdeschose"],
        )
        alert = await AlertRepository(write_session).get_by_id(alert_id)
        assert alert is not None
        alert.isbn = ANCIEN_ISBN
        await write_session.commit()

    card_message = await _edit_the_isbn(
        session_maker,
        alert_id,
        NOUVEL_ISBN,
        notice=None,
        outcome=KeywordVariantsOutcome.unanswered(),
    )

    async with session_maker() as read_session:
        stored = await AlertRepository(read_session).get_by_id(alert_id)

    assert stored is not None
    assert stored.isbn == NOUVEL_ISBN
    assert stored.variants == []
    assert stored.exclude_keywords == []
    assert "L'IA n'a pas répondu après une relance" in card_message.answer.call_args[0][0]


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


@pytest.mark.parametrize("previous_status", ["ACTIVE", "PAUSED"])
@pytest.mark.parametrize("collection", ["price", "empty", "failed"])
async def test_new_edition_is_prepared_in_database_before_surveillance_resumes(
    session_maker,
    previous_status,
    collection,
):
    """La nouvelle identité et sa cote sont préparées avant l’activation."""
    from app.core.constants import AlertStatus, MatchConfidence
    from app.db.models.book import Book
    from app.db.repositories.book_repo import BookRepository
    from app.providers.base import MarketOffer, MarketOffers
    from app.services.book_market_pricing import price_book_now

    async with session_maker() as session:
        alert_id = await _write_alert(
            session,
            name="La guerre éternelle — Intégrale",
            exclude_keywords=["roman"],
            variants=["guerreternelle"],
        )
        alert = await AlertRepository(session).get_by_id(alert_id)
        alert.isbn = "9782800142449"
        alert.status = AlertStatus[previous_status]
        old = Book(
            title=alert.name,
            normalized_title="la guerre eternelle integrale",
            isbn13=alert.isbn,
            author="Marvano",
        )
        session.add(old)
        await session.flush()
        await BookRepository(session).link_alert_to_book(alert_id, old.id)
        await session.commit()

    async def collect(book, **_kwargs):
        async with session_maker() as observing:
            preparing = await AlertRepository(observing).get_by_id(alert_id)
            assert preparing.status == AlertStatus.PAUSED
            assert preparing.isbn == "9782800164861"
        assert book.author == "Marvano"
        assert book.publisher == "Dupuis"
        assert book.publication_year == 2015
        if collection == "price":
            return MarketOffers(
                [
                    MarketOffer(
                        provider_name="test_market",
                        item_price_cents=2000,
                        shipping_price_cents=300,
                        delivered_price_cents=2300,
                        currency="EUR",
                        item_condition="Très bon état",
                        is_available=True,
                        source_url="https://example.com/book",
                        confidence=MatchConfidence.HIGH,
                        raw_payload={"historical_low_1y_cents": 1900},
                    )
                ]
            )
        return MarketOffers(failed_providers=("test_market",) if collection == "failed" else ())

    with (
        patch("app.services.book_market_pricing.AsyncSessionLocal", new=session_maker),
        patch(
            "app.services.book_market_pricing.fetch_offers_for_book",
            new=collect,
        ),
    ):
        card = await _edit_the_isbn(
            session_maker,
            alert_id,
            "9782800164861",
            notice=BookMetadata(
                title="La guerre éternelle (Nouv. éd.) Marvano, Haldeman",
                author="Marvano",
                publisher="Dupuis",
                isbn13="9782800164861",
                publication_year=2015,
                format_detail="144 p.",
            ),
            outcome=KeywordVariantsOutcome.unanswered(),
            pricing=price_book_now,
        )
    async with session_maker() as reading:
        stored = await AlertRepository(reading).get_by_id(alert_id)
        books = await BookRepository(reading).get_all_books_for_alert(alert_id)
        assert stored.status == AlertStatus[previous_status]
        assert stored.name == "La guerre éternelle (Nouv. éd.) Marvano, Haldeman"
        assert len(books) == 1
        book = books[0]
        assert book.isbn13 == "9782800164861"
        assert book.author == "Marvano"
        assert book.publisher == "Dupuis"
        assert book.edition == "144 p."
        cache = book.market_cache
        assert cache is not None
        if collection == "price":
            assert cache.lowest_delivered_price_cents == 2300
            assert cache.historical_low_1y_cents == 1900
        elif collection == "empty":
            assert cache.collection_status == "empty"
        else:
            assert cache.collection_status == "failed"
    texts = card.sent_texts
    assert not any("en cours de calcul" in text for text in texts)
    # Le résultat de préparation doit être visible, pas seulement enregistré en base.
    shown = "\n".join(texts)
    if collection == "price":
        assert "19,00 €" in shown
        assert "23,00 €" in shown
    elif collection == "empty":
        assert "Aucune offre" in shown
    else:
        assert "Cote indisponible" in shown

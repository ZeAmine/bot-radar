"""Benchmark de la résolution en série BNF : coter une série ne retarde pas son alerte.

Une alerte BD suivie par son nom (« The Grocery ») n'a aucune fiche : ses annonces
partaient sans cote tant qu'aucun tome n'avait été lié. La série se compose donc
en arrière-plan — `lookup_series_notices` remonte les tomes, `link_books_from_bnf`
les lie et les cote un par un. C'est un travail qui touche la BNF, la base et les
marchands ; l'invariant n°1 est qu'il ne se produit **qu'après** l'envoi.

Ce fichier mesure cette séparation, avec un réseau entièrement mocké :

1. **l'ordre** — une BNF qui répond en 300 ms, et la preuve que l'alerte est partie
   *avant* que la résolution ne démarre. La durée seule ne prouverait rien : une
   tâche rapide mais exécutée avant l'envoi passerait le budget. Ce qui est mesuré,
   c'est la séquence `envoi → retour du use case → BNF` ;
2. **le coût de la sélection** — `lookup_series_notices` sur une réponse SRU
   réaliste (60 notices, dont 6 tomes, un doublon d'ISBN et des hors-série) ;
3. **le coût du vol** — `link_books_from_bnf` écrit 6 liaisons et déclenche 6
   cotations, BNF et marchands mockés.

Les seuils sont des garde-fous anti-régression, pas des jauges de précision : le
budget de 500 ms est celui de l'invariant n°1 et offre une marge de ~100x.

Usage :
    make bench
    .venv/bin/pytest tests/performance -v -s
"""

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator, Coroutine
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.constants import AlertStatus, ProviderQueryStatus
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.user import User
from app.providers.google_books import ProviderQueryResult
from app.schemas.listing import ListingIngestPayload
from app.services.book_metadata_service import BookMetadata, BookMetadataService
from app.use_cases import ingest_listing
from app.use_cases.ingest_listing import ProcessListingUseCase
from tests.database import sqlite_only_database

# ---------------------------------------------------------------- scorers fixés
SERIES_NAME = "The Grocery"
SERIES_KEYWORD = "the grocery"
WARMUP_ITERATIONS = 3
MEASURED_ITERATIONS = 20
BUDGET_MS = 500.0  # Invariant n°1 : l'alerte part en moins de 500 ms
BNF_LATENCY_MS = 300.0  # Latence simulée de la BNF, bien au-dessus du budget
SERIES_NOTICES = 6  # taille du jeu de tomes simulé dans ce fichier
# Sélection sur 60 notices : CPU pur de filtrage. Le seuil est serré parce que
# l'opération est locale — une régression ici se voit en quelques dizaines de µs.
SERIES_LOOKUP_BUDGET_MS = 5.0
# Vol de 6 tomes : 6 liaisons en base + 6 cotations déclenchées. Mesuré à ~9 ms,
# budget à 100 ms : il attrape l'introduction d'un appel bloquant (une vraie
# requête BNF dépasse 300 ms) sans dépendre de la machine qui exécute.
SERIES_LINK_BUDGET_MS = 100.0


async def _empty_openlibrary(
    *_args: Any, **_kwargs: Any
) -> ProviderQueryResult[dict[str, Any]]:
    """OpenLibrary neutralisée : ces benchmarks mesurent le chemin BNF, pas le repli.

    Depuis que `lookup_series_notices` complète par OpenLibrary quand la BNF rend
    moins de tomes que le plafond, un stub BNF seul laisserait partir une vraie
    requête réseau. Le réseau est entièrement mocké ici : il doit le rester.
    """
    return ProviderQueryResult(status=ProviderQueryStatus.SUCCESS, items=())


def _percentile(values: list[float], pct: float) -> float:
    """Percentile interpolé (pct entre 0 et 100).

    Copié de `test_alert_path_benchmark` plutôt qu'importé : `tests/performance`
    n'est pas un paquet, et un import entre fichiers de test ne fonctionnerait
    que par l'ajout silencieux de `sys.path` fait par pytest.
    """
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def _report_line(label: str, values_ms: list[float]) -> str:
    """Formate une ligne de rapport p50/p95/p99/max pour un scénario."""
    return (
        f"  {label:<34} "
        f"p50={_percentile(values_ms, 50):8.2f} ms  "
        f"p95={_percentile(values_ms, 95):8.2f} ms  "
        f"p99={_percentile(values_ms, 99):8.2f} ms  "
        f"max={max(values_ms):8.2f} ms"
    )


def _series_notices(count: int = SERIES_NOTICES) -> list[BookMetadata]:
    """Notices BNF d'une série : chaque tome a un titre qui commence par le nom."""
    return [
        BookMetadata(
            title=f"{SERIES_NAME} — Tome {i}",
            author="Tyler Johnson",
            publisher="Soleil US Comics",
            isbn13=f"978235{i:04d}0",
        )
        for i in range(1, count + 1)
    ]


def _typed_bnf(
    records: list[BookMetadata],
) -> tuple[ProviderQueryResult[BookMetadata], None]:
    """Joint typé moqué : la BNF a répondu, avec ces notices et sans panne.

    Le service ne rend plus une liste nue : une liste vide y disait aussi bien
    « cette série n'existe pas » que « la requête a échoué ». Le benchmark
    remplace donc le joint typé, pas la lecture qu'on en fait.
    """
    return (
        ProviderQueryResult(
            status=ProviderQueryStatus.SUCCESS,
            items=tuple(records),
            has_results=bool(records),
        ),
        None,
    )


def _bnf_response(records_total: int = 60) -> list[BookMetadata]:
    """Réponse SRU réaliste : des tomes, un doublon d'ISBN, du hors-série.

    Le filtrage fait le travail que la série lui impose : écarter ce qui ne commence
    pas par le nom de la série, dédupliquer par ISBN, refuser un auteur divergent.
    """
    records: list[BookMetadata] = []
    tomes = _series_notices()
    for tome in tomes:
        records.append(tome)
        records.append(tome)  # doublon d'ISBN : une notice, un livre
    for i in range(records_total - len(records)):
        records.append(
            BookMetadata(
                title=f"Collection{i} — Les archives Ajax",
                author="Auteur sans rapport",
                publisher="Éditions ZiF",
                isbn13=f"978000{i:05d}",
            )
        )
    records.append(
        BookMetadata(
            title=f"{SERIES_NAME} — Revue du chapitre 4",
            author="Auteur sans rapport",
            publisher="Zventre",
            isbn13="9789999999",
        )
    )
    return records


def _make_payload(iteration: int) -> ListingIngestPayload:
    """Annonce BD qui matche l'alerte de série, sans ISBN : elle partira sans cote."""
    return ListingIngestPayload(
        source_name="vinted",
        external_id=f"series-{uuid.uuid4()}-{iteration}",
        url=f"https://www.vinted.fr/items/9000{iteration:04d}-the-grocery",
        title="The Grocery - l'intégrale, très bon état EO",
        description="Édition Soleil US Comics, collection complète, pages niées.",
        category_slug="bd_comics",
        item_price_cents=1800,
        estimated_protection_cents=180,
        estimated_shipping_cents=288,
        condition="Très bon état",
        seller_name="bd_collectionneur",
        seller_rating=Decimal("4.90"),
        photos=["https://images1.vinted.net/t/01/f800/bbb11111.webp?s=1"],
    )


def _make_mock_bot() -> AsyncMock:
    """Bot aiogram mocké : seul l'I/O Telegram est simulé, le formatage est réel."""
    bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 42
    bot.send_photo.return_value = sent
    bot.send_message.return_value = sent
    return bot


@pytest.fixture
async def series_db() -> AsyncGenerator[tuple[AsyncSession, async_sessionmaker], None]:
    """Base SQLite en mémoire : une alerte BD de série, sans ISBN et sans livre lié.

    C'est précisément l'alerte qui partsait sans cote : aucun ISBN, aucun tome
    dans le catalogue. Le chemin mesuré est donc celui de la résolution complète.
    """
    async with sqlite_only_database() as database:
        session, session_maker = database
        category = Category(slug="bd_comics", label="BD & Comics", emoji="📚", is_active=True)
        session.add(category)
        await session.flush()

        user = User(telegram_user_id=123456, username="bench-series", is_allowed=True)
        session.add(user)
        await session.flush()

        session.add(
            Alert(
                user_id=user.id,
                category_id=category.id,
                name=SERIES_NAME,
                include_keywords=[SERIES_KEYWORD],
                exclude_keywords=[],
                variants=[],
                max_item_price_cents=5000,
                status=AlertStatus.ACTIVE,
            )
        )
        await session.commit()

        yield session, session_maker



@pytest.mark.asyncio
async def test_benchmark_the_alert_is_not_delayed_by_a_slow_series_lookup(
    series_db: tuple[AsyncSession, async_sessionmaker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'alerte part avant que le catalogue de la série ne soit cherché, et sans l'attendre.

    La BNF répond en 300 ms, soit plus que la moitié du budget de l'invariant n°1.
    Si cette résolution précédait l'envoi, chaque annonce d'une série retarderait
    l'alerte de 300 ms — l'exact contraire de ce qu'elle doit faire gagner, qui
    est de donner une cote à l'*alerte suivante*.

    Deux mesures, dont la seconde est la vraie : l'ordre. La durée seule ne
    prouverait rien, une tâche rapide mais exécutée avant l'envoi passerait le
    budget sans jamais faire attendre l'utilisateur.

    Le travail différé ici est celui du chemin d'ingestion : il lie le livre que
    l'annonce désigne et le cote. Le vol de six tomes appartient à la création de
    l'alerte (`link_books_from_bnf`) et se mesure dans
    `test_benchmark_series_link_writes_six_links_and_prices_them`.
    """
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    session, session_maker = series_db
    events: list[str] = []
    deferred: list[tuple[str, Coroutine[Any, Any, Any]]] = []

    def _collect(coro: Coroutine[Any, Any, Any], name: str | None = None) -> None:
        label = name or ""
        # La mesure de cote est un compteur Redis : hors du chemin mesuré, et
        # sans Redis elle nuirait à la mesure plus qu'elle ne l informing.
        if label.startswith("cote_state"):
            coro.close()
            return
        deferred.append((label, coro))

    monkeypatch.setattr(ingest_listing, "safe_create_task", _collect)
    # `AsyncSessionLocal` est importé *dans* la fonction : c'est le module source
    # qu'il faut patcher, sinon la tâche différée ouvre une vraie session et
    # échoue avant même d'atteindre la BNF.
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", session_maker)

    async def _slow_bnf(
        *_args: Any, **_kwargs: Any
    ) -> tuple[ProviderQueryResult[BookMetadata], None]:
        events.append("bnf")
        await asyncio.sleep(BNF_LATENCY_MS / 1000.0)
        return _typed_bnf(_series_notices())

    monkeypatch.setattr(BookMetadataService, "_bnf_records_result", _slow_bnf)
    monkeypatch.setattr(
        BookMetadataService, "_query_openlibrary_result", _empty_openlibrary
    )

    priced: list[tuple[uuid.UUID, str | None]] = []
    monkeypatch.setattr(
        ingest_listing,
        "schedule_book_pricing",
        lambda book_id, reason=None: priced.append((book_id, reason)),
    )

    # Le marqueur d'envoi est posé par le bot : c'est la sortie réelle de la
    # fonction d'alerte, pas une instrumentation du use case.
    bot = _make_mock_bot()
    bot.send_photo.side_effect = lambda *_a, **_k: _mark_sent(events)

    durations: list[float] = []
    for i in range(WARMUP_ITERATIONS + MEASURED_ITERATIONS):
        use_case = ProcessListingUseCase(db=session, bot=bot)
        start = time.perf_counter()
        response = await use_case.execute(_make_payload(i), raw_payload={"bench": i})
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        assert response.matches[0].notification_sent is True

        measured = i >= WARMUP_ITERATIONS
        if measured:
            durations.append(elapsed_ms)
        # On ne mesure pas la résolution ici : elle est exécutée plus bas, une
        # fois pour toutes, avec son ordre. Les coroutines jetées sont fermées
        # pour ne pas laisser d'avertissement « never awaited ».
        for _label, coro in deferred:
            coro.close()
        deferred.clear()

    # Une itération de plus, cette fois gardée : c'est elle qui porte la preuve
    # de l'ordre. Les précédentes ne servaient qu'à mesurer le chemin d'envoi.
    events.clear()
    use_case = ProcessListingUseCase(db=session, bot=bot)
    await use_case.execute(_make_payload(999), raw_payload={"bench": "order"})
    events.append("retour")
    assert [label for label, _ in deferred if label.startswith("bd_catalog")], deferred

    series_started = time.perf_counter()
    for _label, coro in deferred:
        await coro
    series_ms = (time.perf_counter() - series_started) * 1000.0
    deferred.clear()

    order = [event for event in events if event in {"envoi", "retour", "bnf"}]
    print(
        "\n📊 Benchmark résolution de série — alerte BD sans ISBN "
        f"(n={MEASURED_ITERATIONS}, SQLite in-memory, BNF mockée à {BNF_LATENCY_MS:.0f} ms)"
    )
    print(_report_line("envoi (alerte partie)", durations))
    print(_report_line("résolution de série (après)", [series_ms]))
    print(f"  Ordre observé : {' → '.join(order)}")
    print(f"  Livres cotés en arrière-plan : {len(priced)} ({priced})")

    p95 = _percentile(durations, 95)
    assert p95 < BUDGET_MS, f"p95={p95:.1f} ms dépasse l'invariant des {BUDGET_MS:.0f} ms"
    assert series_ms >= BNF_LATENCY_MS, (
        f"la BNF mockée a mis {series_ms:.0f} ms : le test ne prouve plus rien si elle "
        "répond instantanément"
    )
    # L'ordre est la preuve : l'alerte est partie, le use case est rendu, et
    # seulement ensuite le catalogue est cherché.
    assert order[:3] == ["envoi", "retour", "bnf"], order
    # Et la tâche différée a fait son travail, après coup : le chemin d'ingestion
    # relance la découverte de **série** (`series=not alert.isbn`), donc les six
    # tomes liés partent en cote, pas le seul titre de l'annonce. Une tâche qui ne
    # travaille pas ne prouverait rien non plus. Le motif « bnf_link » dit que la
    # cote part après la liaison, jamais avant.
    assert len(priced) == SERIES_NOTICES, priced
    assert {reason for _book_id, reason in priced} == {"bnf_link"}, priced


def _mark_sent(events: list[str]) -> MagicMock:
    events.append("envoi")
    sent = MagicMock()
    sent.message_id = 42
    return sent


@pytest.mark.asyncio
async def test_benchmark_series_lookup_selects_six_tomes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sélection des tomes d'une série : CPU pur sur une réponse SRU réaliste.

    Le coût utile de `lookup_series_notices` n'est pas la requête — c'est le tri :
    écarter le hors-série, dédupliquer par ISBN, refuser un auteur divergent, et
    borner à `MAX_SERIES_BOOKS`. Ce benchmark garde ce tri honnête en lui donnant
    60 notices dont 7 ressemblent à la série.
    """
    service = BookMetadataService()
    response = _bnf_response()

    async def _instant_bnf(
        *_args: Any, **_kwargs: Any
    ) -> tuple[ProviderQueryResult[BookMetadata], None]:
        return _typed_bnf(response)

    monkeypatch.setattr(BookMetadataService, "_bnf_records_result", _instant_bnf)
    monkeypatch.setattr(
        BookMetadataService, "_query_openlibrary_result", _empty_openlibrary
    )

    async def _one_lookup() -> list[BookMetadata]:
        verdict = await service.lookup_series_notices(SERIES_NAME, author_hint="Tyler Johnson")
        return list(verdict.notices)

    for _ in range(WARMUP_ITERATIONS):
        assert len(await _one_lookup()) == SERIES_NOTICES

    durations: list[float] = []
    for _ in range(MEASURED_ITERATIONS * 5):
        start = time.perf_counter()
        found = await _one_lookup()
        durations.append((time.perf_counter() - start) * 1000.0)
        assert len(found) == SERIES_NOTICES, [notice.title for notice in found]

    p95 = _percentile(durations, 95)
    print(f"\n📊 Benchmark sélection de série — {len(response)} notices BNF, 6 tomes attendus")
    print(_report_line("lookup_series_notices", durations))
    marge = SERIES_LOOKUP_BUDGET_MS / p95
    print(f"  Budget sélection : {SERIES_LOOKUP_BUDGET_MS:.0f} ms — marge observée ×{marge:.0f}")

    assert p95 < SERIES_LOOKUP_BUDGET_MS, (
        f"p95={p95:.3f} ms dépasse le budget de sélection de {SERIES_LOOKUP_BUDGET_MS:.0f} ms"
    )


@pytest.mark.asyncio
async def test_benchmark_series_link_writes_six_links_and_prices_them(
    series_db: tuple[AsyncSession, async_sessionmaker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le vol de 6 tomes : 6 liaisons écrites, 6 cotations déclenchées, une seule fois.

    C'est le travail que la série demande en plus d'une alerte simple. Il est
    entièrement en arrière-plan : la mesure compte donc son coût, pas sa place
    dans le chemin critique, et vérifie qu'il fait bien son travail — six livres,
    six cotations, aucune perdue.
    """
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    session, session_maker = series_db
    result = await session.execute(select(Alert.id))
    alert_id = result.scalar_one()
    assert alert_id is not None

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", session_maker)

    async def _instant_bnf(
        *_args: Any, **_kwargs: Any
    ) -> tuple[ProviderQueryResult[BookMetadata], None]:
        return _typed_bnf(_series_notices())

    monkeypatch.setattr(BookMetadataService, "_bnf_records_result", _instant_bnf)
    monkeypatch.setattr(
        BookMetadataService, "_query_openlibrary_result", _empty_openlibrary
    )

    priced: list[tuple[uuid.UUID, str | None]] = []
    monkeypatch.setattr(
        ingest_listing,
        "schedule_book_pricing",
        lambda book_id, reason=None: priced.append((book_id, reason)),
    )

    async def _one_flight() -> None:
        await ingest_listing.link_books_from_bnf(
            SERIES_NAME, alert_id, author="Tyler Johnson", series=True
        )

    durations: list[float] = []
    for index in range(WARMUP_ITERATIONS + MEASURED_ITERATIONS):
        # Un vol ne compte que ses propres cotations : la liste est remise à zéro
        # à chaque itération, l'accumulation dirait « au moins six » sans plus.
        priced.clear()
        start = time.perf_counter()
        await _one_flight()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if index >= WARMUP_ITERATIONS:
            durations.append(elapsed_ms)
        # Chaque vol repart d'un catalogue vierge : on mesure toujours six tomes
        # à écrire, jamais un catalogue déjà rempli par le vol précédent.
        async with session_maker() as cleanup:
            for model in (AlertBook, BookMarketCache, Listing, Book, Alert):
                await cleanup.execute(delete(model))
            await cleanup.commit()

    p95 = _percentile(durations, 95)
    print(
        f"\n📊 Benchmark vol de série — {SERIES_NOTICES} tomes liées et cotées "
        "(BNF et marchands mockés)"
    )
    print(_report_line("link_books_from_bnf", durations))
    print(f"  Cotations déclenchées au dernier vol : {len(priced)}")

    # Six tomes liés, six cotations, et chacune nommée « bnf_link » : la cote
    # part après le commit, jamais avant.

    assert p95 < SERIES_LINK_BUDGET_MS, (
        f"p95={p95:.1f} ms dépasse le budget de vol de {SERIES_LINK_BUDGET_MS:.0f} ms"
    )
    assert len(priced) == SERIES_NOTICES, priced
    assert {reason for _book_id, reason in priced} == {"bnf_link"}, priced

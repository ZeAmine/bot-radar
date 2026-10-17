"""Benchmark reproductible du chemin critique ingestion → matching → alerte Telegram.

L'invariant n°1 impose l'ordre Telegram avant les fournisseurs. Le seuil existant
500 ms est un garde-fou p95/p99 avec doubles, pas un délai de livraison réelle.

1. le chrono débute juste avant execute et finit à son retour : admission,
   dédoublonnage, SQL, matching/cache, formatage, accusé Telegram doublé, marquage,
   commit de notification et commit final sont inclus. Payload et bot sont créés
   avant ; collecte, réseau réel et achèvement des tâches de fond sont exclus.
   81 alertes sont semées, dont 41 BD évaluées sur ce scénario filtré par catégorie.
2. le doublon utilise la même frontière, sans envoi.
3. le CPU mesure une passe sur 81 alertes ; le seuil ADR-003 compare la médiane
   des moyennes par évaluation sur 20 passes, pas un percentile par appel.

Les tâches de fond peuvent partager l'event loop et la DB en production ; leur
programmation peut précéder le retour. Réseau doublé et IA coupée ne mesurent pas
leur contention réelle. Aucun chronomètre ne mesure publication ou lecture humaine.

Usage :
    make bench
    .venv/bin/pytest tests/performance -v -s
"""

import statistics
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import AlertStatus, ComparisonStatus, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.domain.rules.listing_match import evaluate_listing_against_alert
from app.schemas.listing import ListingIngestPayload
from app.use_cases.ingest_listing import ProcessListingUseCase
from tests.database import sqlite_only_database
from tests.telegram_fakes import telegram_reply

N_BD_ALERTS = 40
N_TECH_ALERTS = 40
N_ACTIVE_ALERTS = N_BD_ALERTS + N_TECH_ALERTS
WARMUP_ITERATIONS = 10
MEASURED_ITERATIONS = 150
BLACKSAD_ISBN = "9782205049657"
BUDGET_MS = 500.0  # Garde-fou existant : cas d’usage complet avec doubles.

# ADR-003 : < 100 µs par évaluation alerte×annonce ; seuil et message partagent la constante.
ADR003_MATCHING_BUDGET_US = 100.0


def _percentile(values: list[float], pct: float) -> float:
    """Percentile interpolé (pct entre 0 et 100)."""
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


@pytest.fixture
async def benchmark_db() -> AsyncGenerator[AsyncSession, None]:
    """Base SQLite en mémoire ensemencée avec un catalogue réaliste d'alertes.

    - 40 alertes BD actives (mots-clés distincts, aucune ne matche l'annonce) ;
    - 1 alerte BD avec ISBN lié à un livre du catalogue + cache de marché frais ;
    - 40 alertes tech actives (catégorie différente, sortie immédiate du matcher).
    """
    async with sqlite_only_database() as database:
        session = database.session
        cat_bd = Category(slug="bd_comics", label="BD & Comics", emoji="📚", is_active=True)
        cat_tech = Category(slug="tech", label="Électronique", emoji="💻", is_active=True)
        session.add_all([cat_bd, cat_tech])
        await session.flush()

        user = User(telegram_user_id=123456, username="bench", is_allowed=True)
        session.add(user)
        await session.flush()

        for i in range(N_BD_ALERTS):
            session.add(
                Alert(
                    user_id=user.id,
                    category_id=cat_bd.id,
                    name=f"Série de test {i}",
                    include_keywords=[f"seriedetest{i}"],
                    exclude_keywords=[],
                    variants=[],
                    max_item_price_cents=5000,
                    status=AlertStatus.ACTIVE,
                )
            )

        isbn_alert = Alert(
            user_id=user.id,
            category_id=cat_bd.id,
            name="Blacksad — Tome 6",
            isbn=BLACKSAD_ISBN,
            include_keywords=["blacksad"],
            exclude_keywords=[],
            variants=[],
            max_item_price_cents=5000,
            status=AlertStatus.ACTIVE,
        )
        session.add(isbn_alert)
        await session.flush()

        for i in range(N_TECH_ALERTS):
            session.add(
                Alert(
                    user_id=user.id,
                    category_id=cat_tech.id,
                    name=f"Tech de test {i}",
                    include_keywords=[f"techdetest{i}"],
                    exclude_keywords=[],
                    variants=[],
                    max_item_price_cents=5000,
                    status=AlertStatus.ACTIVE,
                )
            )

        book = Book(
            isbn13=BLACKSAD_ISBN,
            title="Blacksad Tome 6 — Ce qui est caché",
            normalized_title="blacksad tome 6 ce qui est cache",
            author="Juan Díaz Canales",
            publisher="Dargaud",
        )
        session.add(book)
        await session.flush()

        session.add(
            AlertBook(
                alert_id=isbn_alert.id,
                book_id=book.id,
                match_confidence=MatchConfidence.HIGH,
            )
        )
        session.add(
            BookMarketCache(
                book_id=book.id,
                lowest_delivered_price_cents=512,
                best_provider_name="Momox",
                best_source_url="https://example.com/blacksad-t6",
                confidence=MatchConfidence.HIGH,
                checked_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=24),
                comparison_status=ComparisonStatus.AVAILABLE,
                historical_low_1y_cents=412,
                historical_low_at=datetime.now(UTC),
            )
        )
        await session.commit()

        yield session


def _make_payload(iteration: int | None) -> ListingIngestPayload:
    """Annonce BD réaliste qui matche l'alerte ISBN, avec sa photo de couverture."""
    external_id = f"bench-{uuid.uuid4()}" if iteration is not None else "bench-dup"
    item_num = iteration if iteration is not None else 0
    return ListingIngestPayload(
        source_name="vinted",
        external_id=external_id,
        url=f"https://www.vinted.fr/items/900000{item_num:05d}-blacksad-tome-6",
        title="Blacksad Tome 6 - Ce qui est caché, très bon état EO",
        description="Édition originale Dargaud, jet de caractères 'Très bon état', aucune rature.",
        category_slug="bd_comics",
        item_price_cents=1400,
        estimated_protection_cents=140,
        estimated_shipping_cents=288,
        condition="Très bon état",
        seller_name="bd_collectionneur",
        seller_rating=Decimal("4.90"),
        photos=["https://images1.vinted.net/t/01/f800/aaa11111.webp?s=1"],
        isbn13=BLACKSAD_ISBN,
    )


def _make_mock_bot() -> AsyncMock:
    """Bot aiogram mocké : uniquement l'I/O Telegram est simulée, le formatage est réel."""
    bot = AsyncMock()
    bot.id = 123456789
    bot.send_photo.side_effect = telegram_reply
    bot.send_message.side_effect = telegram_reply
    return bot


async def _run_full_path(session: AsyncSession, iteration: int) -> float:
    """Une ingestion complète (nouvelle annonce) mesurée en ms. Retourne la durée."""
    payload = _make_payload(iteration)
    bot = _make_mock_bot()
    use_case = ProcessListingUseCase(db=session, bot=bot)

    start = time.perf_counter()
    response = await use_case.execute(payload, raw_payload={"bench": iteration})
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    # Le chemin mesuré doit être fonctionnellement correct
    assert response.is_duplicate is False
    assert len(response.matches) == 1
    assert response.matches[0].notification_sent is True
    return elapsed_ms


@pytest.mark.asyncio
async def test_benchmark_full_alert_path(
    benchmark_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute complet avec doubles, marquage et commit final inclus : p99 < 500 ms."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [123456])
    session = benchmark_db

    for i in range(WARMUP_ITERATIONS):
        await _run_full_path(session, i)

    durations: list[float] = []
    for i in range(MEASURED_ITERATIONS):
        durations.append(await _run_full_path(session, WARMUP_ITERATIONS + i))

    p95 = _percentile(durations, 95)
    p99 = _percentile(durations, 99)
    print(
        "\n📊 Benchmark chemin d'alerte — ingestion complète "
        f"(n={MEASURED_ITERATIONS}, {N_ACTIVE_ALERTS} alertes actives, "
        "SQLite in-memory, réseau mocké)"
    )
    print(_report_line("cas d’usage complet (doubles)", durations))
    print(
        f"  Garde-fou local avec doubles : {BUDGET_MS:.0f} ms "
        f"— marge observée ×{BUDGET_MS / p95:.0f}"
    )

    assert p95 < BUDGET_MS, (
        f"p95={p95:.1f} ms dépasse le garde-fou local avec doubles des {BUDGET_MS:.0f} ms"
    )
    assert p99 < BUDGET_MS, (
        f"p99={p99:.1f} ms dépasse le garde-fou local avec doubles des {BUDGET_MS:.0f} ms"
    )


@pytest.mark.asyncio
async def test_benchmark_duplicate_ingestion_short_circuit(
    benchmark_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ré-ingestion d'un doublon : dédoublonnage immédiat, aucun renvoi d'alerte."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [123456])
    session = benchmark_db

    payload = _make_payload(None)
    bot = _make_mock_bot()
    use_case = ProcessListingUseCase(db=session, bot=bot)

    first = await use_case.execute(payload, raw_payload={})
    assert first.is_duplicate is False

    for _ in range(WARMUP_ITERATIONS):
        await use_case.execute(payload, raw_payload={})

    durations: list[float] = []
    for _ in range(MEASURED_ITERATIONS):
        start = time.perf_counter()
        response = await use_case.execute(payload, raw_payload={})
        durations.append((time.perf_counter() - start) * 1000.0)
        assert response.is_duplicate is True
        assert response.matches == []

    p95 = _percentile(durations, 95)
    print(
        f"\n📊 Benchmark ré-ingestion doublon (n={MEASURED_ITERATIONS}, "
        f"{N_ACTIVE_ALERTS} alertes actives)"
    )
    print(_report_line("dédoublonnage (short-circuit)", durations))

    assert p95 < BUDGET_MS, (
        f"p95={p95:.1f} ms dépasse le garde-fou local avec doubles des {BUDGET_MS:.0f} ms"
    )


@pytest.mark.asyncio
async def test_benchmark_cpu_matching_only(benchmark_db: AsyncSession) -> None:
    """Matching CPU seul : ~81 évaluations réelles par annonce (ADR-003 : < 100 µs/éval).

    Mesure le temps CPU pur d'une passe de matching sur toutes les alertes actives,
    hors SQL et hors Telegram. Le seuil est celui de l'ADR-003 lui-même, pas un multiple
    de celui-ci : un garde-fou relaxé à 500 µs avait fini par couvrir la régression
    qu'il devait au contraire signaler (le texte de l'annonce était renormalisé à chaque
    mot-clé, 341 µs/éval sur 113 alertes réelles). Le correctif est mesuré à ~51 µs.
    """
    from sqlalchemy import select

    from app.db.models.alert import Alert as AlertModel

    result = await benchmark_db.execute(
        select(AlertModel)
        .where(AlertModel.status == AlertStatus.ACTIVE)
        .order_by(AlertModel.created_at)
    )
    alerts: list[Any] = list(result.scalars().all())
    assert len(alerts) == N_ACTIVE_ALERTS + 1  # + l'alerte ISBN

    listing_args: dict[str, Any] = {
        "listing_category_slug": "bd_comics",
        "listing_normalized_title": "blacksad tome 6 ce qui est cache tres bon etat eo",
        "listing_normalized_description": (
            "edition originale dargaud jet de caracteres tres bon etat aucune rature"
        ),
        "listing_condition": "Très bon état",
        "listing_item_price_cents": 1400,
        "listing_isbn13": BLACKSAD_ISBN,
        "listing_raw_url": "https://www.vinted.fr/items/90000000123-blacksad-tome-6",
    }

    for alert in alerts:
        evaluate_listing_against_alert(alert=alert, **listing_args)

    passes = 20
    per_eval_us: list[float] = []
    for _ in range(passes):
        start = time.perf_counter()
        matches = 0
        for alert in alerts:
            result_eval = evaluate_listing_against_alert(alert=alert, **listing_args)
            if result_eval.is_match:
                matches += 1
        elapsed_us = (time.perf_counter() - start) * 1_000_000.0
        per_eval_us.append(elapsed_us / len(alerts))
        assert matches == 1, "seule l'alerte ISBN doit matcher cette annonce"

    median_us = statistics.median(per_eval_us)
    print(
        f"\n📊 Benchmark matching CPU ({len(alerts)} alertes × {passes} passes, "
        "hors SQL et hors Telegram)"
    )
    print(
        f"  médiane par évaluation alerte×annonce : {median_us:.1f} µs "
        f"(ADR-003 : < {ADR003_MATCHING_BUDGET_US:.0f} µs)"
    )
    pass_ms = [us * len(alerts) / 1000 for us in per_eval_us]
    print(_report_line("passe complète de matching", pass_ms))

    assert median_us < ADR003_MATCHING_BUDGET_US, (
        f"matching CPU dégradé : {median_us:.0f} µs par évaluation "
        f"(ADR-003 : < {ADR003_MATCHING_BUDGET_US:.0f} µs)"
    )

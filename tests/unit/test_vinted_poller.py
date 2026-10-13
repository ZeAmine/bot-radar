"""Tests des métriques du poller Vinted : cadence et couverture de fenêtre."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.vinted_parsing import CATALOG_PAGE_SIZE
from app.services.vinted_scraper import ScrapedVintedItem
from app.tasks.vinted_poller import (
    MAX_MEANINGFUL_CYCLE_GAP_S,
    MIN_CYCLE_GAP_SECONDS,
    WINDOW_LOG_EVERY_CYCLES,
    CycleCadence,
    dedupe_custom_searches,
    load_active_custom_searches,
    load_initial_seen_ids,
    poll_vinted_once,
    remaining_cycle_sleep,
    run_vinted_poller,
)


def test_cycle_cadence_ignores_the_first_cycle() -> None:
    """Le premier cycle n'ouvre aucun échantillon : son écart porte sur l'init."""
    cadence = CycleCadence()
    cadence.record(1_000.0)

    assert cadence.percentile(50) is None
    assert cadence.percentile(90) is None


async def test_initial_seen_read_failure_keeps_its_visible_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Le repli vide reste accompagné du diagnostic de lecture existant."""
    with (
        patch("app.tasks.vinted_poller.AsyncSessionLocal") as factory,
        caplog.at_level(logging.WARNING, logger="radar.vinted_poller"),
    ):
        session = AsyncMock()
        session.execute.side_effect = RuntimeError("Base temporairement indisponible")
        factory.return_value.__aenter__.return_value = session
        seen = await load_initial_seen_ids()
    assert seen == set()
    assert "Impossible de charger les anciens IDs d'annonces" in caplog.text
    assert "Base temporairement indisponible" in caplog.text


def _record_cycles(cadence: CycleCadence, gaps: list[float], start: float = 0.0) -> float:
    """Joue des cycles d'une durée donnée et rend l'horodatage du dernier.

    Le premier `record` sert d'amorce : c'est lui qui ouvre le premier écart.
    """
    now = start
    cadence.record(now)
    for gap in gaps:
        now += gap
        cadence.record(now)
    return now


def test_cycle_cadence_ignores_interruptions() -> None:
    """Une reprise du mode nuit ou une pause anti-blocage ne sont pas des cycles.

    Les inclure ferait afficher une cadence dégradée en p50 alors que la scrutation,
    elle, tourne toujours au rythme prévu.
    """
    cadence = CycleCadence()
    now = _record_cycles(cadence, [15.0, 17.0, 14.0, 16.0])
    # Reprise après une interruption de 3 heures : le p50 doit rester ~15 s.
    cadence.record(now + 3 * 3_600)

    assert cadence.percentile(50) == 15.5
    assert cadence.percentile(90) is not None
    assert cadence.percentile(90) <= MAX_MEANINGFUL_CYCLE_GAP_S


def test_cycle_cadence_ignores_non_positive_gaps() -> None:
    """Un écart nul ou négatif signale une horloge incohérente, pas une cadence."""
    cadence = CycleCadence()
    cadence.record(100.0)
    cadence.record(100.0)
    cadence.record(90.0)

    assert cadence.percentile(50) is None


def test_cycle_cadence_keeps_a_bounded_history() -> None:
    """L'historique est borné, sinon il garderait les cycles de la nuit précédente."""
    cadence = CycleCadence(samples=3)
    _record_cycles(cadence, [10.0, 11.0, 12.0, 13.0])

    assert len(cadence.gaps) == 3
    assert cadence.percentile(50) == 12.0


def test_cycle_cadence_percentiles_are_interpolated() -> None:
    """Percentiles interpolés, comme le p50/p95 de la latence : la même lecture."""
    cadence = CycleCadence()
    _record_cycles(cadence, [10.0, 20.0, 30.0])

    assert cadence.percentile(50) == 20.0
    assert cadence.percentile(90) == 28.0


def _items(count: int) -> list[ScrapedVintedItem]:
    return [
        ScrapedVintedItem(
            external_id=f"v_{index}",
            url=f"https://vinted.fr/items/{index}",
            title="Tintin",
            condition="Très bon état",
            item_price_cents=1000,
            photo_url=None,
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_window_truncation_is_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Une fenêtre servie en dessous de la demande est un avertissement, pas un détail.

    C'est le seul moyen de savoir que l'API plafonne sans erreur : la couverture
    réelle peut rester à quelques dizaines de secondes pendant que le code annonce
    plusieurs minutes.
    """
    with (
        patch(
            "app.tasks.vinted_poller.fetch_vinted_page",
            new_callable=AsyncMock,
            return_value=_items(24),
        ),
        patch(
            "app.tasks.vinted_poller._ingest_scraped_item",
            new_callable=AsyncMock,
            return_value=_MockIngest(),
        ),
        caplog.at_level(logging.WARNING, logger="radar.vinted_poller"),
    ):
        # `seen_ids` déjà rempli : cycle sans nouveauté, celui qui n'était jamais journalisé.
        await poll_vinted_once(
            seen_ids={f"v_{i}" for i in range(24)},
            vinted_client=AsyncMock(spec=httpx.AsyncClient),
            cycle_index=3,
        )

    assert "Fenêtre catalogue tronquée : 24 items reçus pour 96 demandés" in caplog.text


@pytest.mark.asyncio
async def test_full_window_logs_a_sparse_heartbeat(caplog: pytest.LogCaptureFixture) -> None:
    """Fenêtre complète : un heartbeat tous les N cycles, et rien entre."""
    seen = {f"v_{i}" for i in range(CATALOG_PAGE_SIZE)}
    items = _items(CATALOG_PAGE_SIZE)

    def heartbeats() -> list[str]:
        return [line for line in caplog.text.splitlines() if "Fenêtre catalogue :" in line]

    with (
        patch(
            "app.tasks.vinted_poller.fetch_vinted_page",
            new_callable=AsyncMock,
            return_value=items,
        ),
        caplog.at_level(logging.INFO, logger="radar.vinted_poller"),
    ):
        for cycle in range(1, WINDOW_LOG_EVERY_CYCLES):
            await poll_vinted_once(
                seen_ids=seen,
                vinted_client=AsyncMock(spec=httpx.AsyncClient),
                cycle_index=cycle,
            )
        assert heartbeats() == [], "le cas nominal ne doit pas parler à chaque cycle"

        await poll_vinted_once(
            seen_ids=seen,
            vinted_client=AsyncMock(spec=httpx.AsyncClient),
            cycle_index=WINDOW_LOG_EVERY_CYCLES,
        )

    assert len(heartbeats()) == 1
    assert f"{CATALOG_PAGE_SIZE} items reçus pour {CATALOG_PAGE_SIZE} demandés" in (
        heartbeats()[0]
    )
    assert "tronc" not in caplog.text


class _MockIngest:
    """Ce que `_ingest_scraped_item` rend : un résultat sans appel réseau."""

    notified_count = 0
    needs_retry = False


def test_remaining_cycle_sleep_subtracts_the_work_from_the_pause() -> None:
    """Le travail du cycle est déduit de la pause : 15 s de pause, 0,4 s de travail → 14,6 s."""
    assert remaining_cycle_sleep(15.0, 0.4) == pytest.approx(14.6)


def test_remaining_cycle_sleep_keeps_the_floor_on_a_slow_cycle() -> None:
    """Un cycle lent ne descend pas sous le plancher : 12 s de travail sur 15 s de pause → 6 s."""
    assert remaining_cycle_sleep(15.0, 12.0) == MIN_CYCLE_GAP_SECONDS


def test_remaining_cycle_sleep_ignores_a_negative_work_measure() -> None:
    """Une horloge incohérente ne rallonge pas la pause au-delà de la cible."""
    assert remaining_cycle_sleep(15.0, -3.0) == 15.0


def test_dedupe_custom_searches_keeps_categories_apart() -> None:
    """Les couples identiques fusionnent ; deux catégories sur une URL restent distinctes."""
    searches = [
        ("https://v.fr/a", "tech"),
        ("https://v.fr/a", "tech"),
        ("https://v.fr/a", "clothes"),
        ("https://v.fr/b", "tech"),
    ]

    assert dedupe_custom_searches(searches) == [
        ("https://v.fr/a", "tech"),
        ("https://v.fr/a", "clothes"),
        ("https://v.fr/b", "tech"),
    ]


@pytest.mark.asyncio
async def test_load_active_custom_searches_dedupes_identical_pairs() -> None:
    """Deux alertes de même catégorie sur la même URL ne prennent qu'un tour de round-robin."""
    rows = [("https://v.fr/a", "tech"), ("https://v.fr/a", "tech"), (None, "tech")]
    with patch("app.tasks.vinted_poller.AsyncSessionLocal") as factory:
        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = rows
        session.execute.return_value = result
        factory.return_value.__aenter__.return_value = session

        searches = await load_active_custom_searches()

    assert searches == [("https://v.fr/a", "tech")]


@pytest.mark.asyncio
async def test_cycle_sleep_subtracts_the_cycle_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """La boucle retire le travail du cycle de la pause au lieu de l'y ajouter.

    Le poller mesurait le travail (collecte, ingestion, envoi) puis dormait la pause
    complète : une rafale d'annonces allongeait la cadence et retardait la détection des
    annonces suivantes. Ici, le sommeil demandé doit valoir la pause cible moins le temps
    réellement écoulé, et le travail mesuré doit être strictement positif.
    """
    import asyncio

    from app.core.config import settings

    monkeypatch.setattr(settings, "VINTED_POLLER_ENABLED", True)
    stop_event = asyncio.Event()
    calls: list[tuple[float, float]] = []

    def spy_remaining(sleep_time: float, work_seconds: float) -> float:
        calls.append((sleep_time, work_seconds))
        return 0.0

    async def fake_poll(*_args: object, **_kwargs: object) -> int:
        await asyncio.sleep(0.05)
        stop_event.set()
        return 0

    with (
        patch("app.tasks.vinted_poller.is_night_time", return_value=False),
        patch(
            "app.tasks.vinted_poller.load_initial_seen_ids",
            new_callable=AsyncMock,
            return_value=set(),
        ),
        patch("app.tasks.vinted_poller.poll_vinted_once", side_effect=fake_poll),
        patch(
            "app.tasks.vinted_poller.get_active_custom_searches_cached",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("app.tasks.vinted_poller.save_poller_status", new_callable=AsyncMock),
        patch("app.tasks.vinted_poller.get_adaptive_poll_interval", return_value=15.0),
        patch("app.tasks.vinted_poller.random.uniform", return_value=0.0),
        patch("app.tasks.vinted_poller.random.randint", return_value=10_000),
        patch("app.tasks.vinted_poller.remaining_cycle_sleep", side_effect=spy_remaining),
    ):
        await run_vinted_poller(stop_event)

    assert len(calls) == 1
    sleep_time, work_seconds = calls[0]
    assert sleep_time == 15.0
    assert work_seconds >= 0.05

"""Tests des métriques du poller Vinted : cadence et couverture de fenêtre."""

import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.vinted_parsing import CATALOG_PAGE_SIZE
from app.services.vinted_scraper import ScrapedVintedItem
from app.tasks.vinted_poller import (
    MAX_MEANINGFUL_CYCLE_GAP_S,
    WINDOW_LOG_EVERY_CYCLES,
    CycleCadence,
    load_initial_seen_ids,
    poll_vinted_once,
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

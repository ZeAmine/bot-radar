"""Tests des métriques du poller Vinted : cadence et couverture de fenêtre."""

import hashlib
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.vinted_parsing import CATALOG_PAGE_SIZE
from app.services.vinted_scraper import (
    DEFAULT_GLOBAL_BD_URL,
    ScrapedVintedItem,
    VintedBlockError,
    VintedCollectionError,
    vinted_request_stats,
)
from app.tasks.vinted_poller import (
    COLLECTION_FAILURE_ALERT_THRESHOLD,
    MAX_MEANINGFUL_CYCLE_GAP_S,
    MIN_CYCLE_GAP_SECONDS,
    WINDOW_LOG_EVERY_CYCLES,
    AvailabilityLedger,
    CycleCadence,
    CycleFrontier,
    FifoSet,
    dedupe_custom_searches,
    load_active_custom_searches,
    load_initial_seen_ids,
    poll_vinted_once,
    remaining_cycle_sleep,
    run_vinted_poller,
    search_freshness_snapshot,
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


def _items_with_ids(identifiers: list[str]) -> list[ScrapedVintedItem]:
    return [
        ScrapedVintedItem(
            external_id=identifier,
            url=f"https://vinted.fr/items/{identifier}",
            title="Tintin",
            condition="Très bon état",
            item_price_cents=1000,
            photo_url=None,
        )
        for identifier in identifiers
    ]


@pytest.mark.asyncio
async def test_batch_frontier_is_the_numeric_maximum_without_extra_fetch() -> None:
    """Le lot est déjà en mémoire : un seul appel catalogue, un maximum comparé en nombre."""
    frontier = CycleFrontier(started_monotonic=1234.5)
    with (
        patch(
            "app.tasks.vinted_poller.fetch_vinted_page",
            new_callable=AsyncMock,
            return_value=_items_with_ids(["999", "2700", "100"]),
        ) as fetch,
        patch(
            "app.tasks.vinted_poller._ingest_scraped_item",
            new_callable=AsyncMock,
            return_value=_MockIngest(),
        ) as ingest,
    ):
        await poll_vinted_once(
            seen_ids=set(),
            vinted_client=AsyncMock(spec=httpx.AsyncClient),
            frontier=frontier,
        )

    fetch.assert_awaited_once()
    assert frontier.previous(DEFAULT_GLOBAL_BD_URL) == "2700"
    assert {call.kwargs["frontier_id"] for call in ingest.await_args_list} == {"2700"}
    assert {call.kwargs["cycle_started_monotonic"] for call in ingest.await_args_list} == {1234.5}


def test_batch_frontier_ignores_identifiers_that_are_not_numbers() -> None:
    """Un identifiant non numérique ne casse pas le cycle et ne fabrique pas de frontière."""
    from app.tasks.vinted_poller import _max_external_id

    assert _max_external_id(_items(2)) is None
    assert _max_external_id(_items_with_ids(["9", "10"])) == "10"


@pytest.mark.asyncio
async def test_previous_frontier_is_null_at_restart_then_carried() -> None:
    """Sans itération précédente, la frontière est null — jamais 0 — puis elle est portée."""
    frontier = CycleFrontier()
    with (
        patch(
            "app.tasks.vinted_poller.fetch_vinted_page",
            new_callable=AsyncMock,
            return_value=_items_with_ids(["42"]),
        ),
        patch(
            "app.tasks.vinted_poller._ingest_scraped_item",
            new_callable=AsyncMock,
            return_value=_MockIngest(),
        ) as ingest,
    ):
        await poll_vinted_once(
            seen_ids=set(),
            vinted_client=AsyncMock(spec=httpx.AsyncClient),
            frontier=frontier,
        )
        cold = ingest.await_args.kwargs
        await poll_vinted_once(
            seen_ids=set(),
            vinted_client=AsyncMock(spec=httpx.AsyncClient),
            frontier=frontier,
        )
        warm = ingest.await_args.kwargs

    assert cold["frontier_id"] == "42"
    assert cold["previous_frontier_id"] is None
    assert cold["previous_frontier_id"] != 0
    assert warm["previous_frontier_id"] == "42"


CUSTOM_SEARCH_URL = "https://www.vinted.fr/catalog?search_text=wahl+magic+clip&category_id=1234"


async def _poll_url(
    frontier: CycleFrontier, url: str, identifiers: list[str]
) -> list[dict[str, object]]:
    """Un lot ramené pour une URL, et les champs de frontière de ses ingestions."""
    with (
        patch(
            "app.tasks.vinted_poller.fetch_vinted_page",
            new_callable=AsyncMock,
            return_value=_items_with_ids(identifiers),
        ),
        patch(
            "app.tasks.vinted_poller._ingest_scraped_item",
            new_callable=AsyncMock,
            return_value=_MockIngest(),
        ) as ingest,
    ):
        await poll_vinted_once(
            seen_ids=set(),
            vinted_client=AsyncMock(spec=httpx.AsyncClient),
            target_url=url,
            frontier=frontier,
        )
    return [dict(call.kwargs) for call in ingest.await_args_list]


@pytest.mark.asyncio
async def test_each_url_keeps_its_own_frontier_across_cycles() -> None:
    """Flux BD et recherche personnalisée sont deux catalogues : aucune frontière croisée."""
    frontier = CycleFrontier(started_monotonic=987.0)

    cold_bd = await _poll_url(frontier, DEFAULT_GLOBAL_BD_URL, ["100", "2700"])
    cold_custom = await _poll_url(frontier, CUSTOM_SEARCH_URL, ["9000"])
    warm_bd = await _poll_url(frontier, DEFAULT_GLOBAL_BD_URL, ["2800"])
    warm_custom = await _poll_url(frontier, CUSTOM_SEARCH_URL, ["9100"])

    assert {entry["frontier_id"] for entry in cold_bd} == {"2700"}
    assert {entry["previous_frontier_id"] for entry in cold_bd} == {None}
    assert {entry["frontier_id"] for entry in cold_custom} == {"9000"}
    # La recherche personnalisée arrive après le flux BD dans le même cycle : elle ne doit
    # jamais hériter du maximum du flux BD, ni y laisser le sien.
    assert {entry["previous_frontier_id"] for entry in cold_custom} == {None}
    assert {entry["frontier_id"] for entry in warm_bd} == {"2800"}
    assert {entry["previous_frontier_id"] for entry in warm_bd} == {"2700"}
    assert {entry["frontier_id"] for entry in warm_custom} == {"9100"}
    assert {entry["previous_frontier_id"] for entry in warm_custom} == {"9000"}
    assert {entry["cycle_started_monotonic"] for entry in warm_bd + warm_custom} == {987.0}
    assert frontier.previous(DEFAULT_GLOBAL_BD_URL) == "2800"
    assert frontier.previous(CUSTOM_SEARCH_URL) == "9100"


@pytest.mark.asyncio
async def test_a_url_that_disappears_and_returns_inherits_no_ghost_frontier() -> None:
    """Une recherche désactivée puis réactivée repart sans frontière héritée."""
    frontier = CycleFrontier(started_monotonic=555.0)

    first = await _poll_url(frontier, CUSTOM_SEARCH_URL, ["9000"])
    frontier.forget_inactive({DEFAULT_GLOBAL_BD_URL})
    returned = await _poll_url(frontier, CUSTOM_SEARCH_URL, ["9500"])

    assert {entry["previous_frontier_id"] for entry in first} == {None}
    assert {entry["previous_frontier_id"] for entry in returned} == {None}
    assert {entry["frontier_id"] for entry in returned} == {"9500"}


@pytest.mark.asyncio
async def test_poller_forgets_the_frontier_of_a_search_that_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Désactivée puis réactivée, une recherche ne récupère pas la frontière d'avant."""
    import asyncio
    from typing import cast

    from app.core.config import settings

    monkeypatch.setattr(settings, "VINTED_POLLER_ENABLED", True)
    stop_event = asyncio.Event()
    active_by_cycle: dict[int, list[tuple[str, str]]] = {
        1: [(CUSTOM_SEARCH_URL, "tech")],
        2: [],
        3: [(CUSTOM_SEARCH_URL, "tech")],
    }
    polls: list[tuple[int, str, dict[str, str | None]]] = []
    state = {"cycle": 1}

    async def fake_searches() -> list[tuple[str, str]]:
        return active_by_cycle[state["cycle"]]

    async def fake_poll(*_args: object, **kwargs: object) -> int:
        cycle_index = cast(int, kwargs["cycle_index"])
        target_url = str(kwargs["target_url"])
        frontier = kwargs["frontier"]
        assert isinstance(frontier, CycleFrontier)
        polls.append((cycle_index, target_url, dict(frontier.frontier_by_url)))
        if target_url == DEFAULT_GLOBAL_BD_URL:
            state["cycle"] = cycle_index
        frontier.record(target_url, f"{cycle_index}000")
        if cycle_index == 3 and target_url == CUSTOM_SEARCH_URL:
            stop_event.set()
        return 0

    async def fake_save(_data: dict[str, object]) -> None:
        return None

    with (
        patch("app.tasks.vinted_poller.is_night_time", return_value=False),
        patch(
            "app.tasks.vinted_poller.load_initial_seen_ids",
            new_callable=AsyncMock,
            return_value=FifoSet(),
        ),
        patch("app.tasks.vinted_poller.poll_vinted_once", side_effect=fake_poll),
        patch(
            "app.tasks.vinted_poller.get_active_custom_searches_cached",
            side_effect=fake_searches,
        ),
        patch("app.tasks.vinted_poller.save_poller_status", side_effect=fake_save),
        patch("app.tasks.vinted_poller.get_adaptive_poll_interval", return_value=15.0),
        patch("app.tasks.vinted_poller.random.uniform", return_value=0.0),
        patch("app.tasks.vinted_poller.remaining_cycle_sleep", return_value=0.0),
    ):
        await run_vinted_poller(stop_event)

    seen_at = {(cycle, url): snapshot for cycle, url, snapshot in polls}
    # Cycle 1 : la recherche arrive après le flux BD et ne voit que sa propre entrée vide.
    assert set(seen_at[(1, CUSTOM_SEARCH_URL)]) == {DEFAULT_GLOBAL_BD_URL}
    # Cycle 2 : la recherche est désactivée ; son entrée, jamais relue ce cycle-là, est oubliée
    # après la lecture des recherches actives, donc avant tous les cycles suivants.
    assert CUSTOM_SEARCH_URL not in seen_at[(3, DEFAULT_GLOBAL_BD_URL)]
    # Cycle 3 : elle revient sans frontière fantôme.
    assert CUSTOM_SEARCH_URL not in seen_at[(3, CUSTOM_SEARCH_URL)]
    assert DEFAULT_GLOBAL_BD_URL in seen_at[(3, CUSTOM_SEARCH_URL)]


@pytest.mark.asyncio
async def test_catalogue_read_leaves_its_line_on_an_early_return(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un scan sans nouveauté sort tôt : la lecture laisse quand même sa ligne, en dernier."""
    frontier = CycleFrontier(started_monotonic=321.0)
    with (
        patch(
            "app.tasks.vinted_poller.fetch_vinted_page",
            new_callable=AsyncMock,
            return_value=_items_with_ids(["2700", "9000"]),
        ),
        patch("app.tasks.vinted_poller._ingest_scraped_item", new_callable=AsyncMock) as ingest,
        caplog.at_level(logging.INFO),
    ):
        ingested = await poll_vinted_once(
            seen_ids={"2700", "9000"},
            vinted_client=AsyncMock(spec=httpx.AsyncClient),
            target_url=CUSTOM_SEARCH_URL,
            category_slug="tech",
            cycle_index=12,
            frontier=frontier,
        )

    assert ingested == 0
    ingest.assert_not_awaited()
    reads = [r for r in caplog.records if getattr(r, "phase", None) == "catalogue_read"]
    assert len(reads) == 1
    read = reads[0]
    assert read.event == "alert_latency"
    assert read.result == "returned"
    assert read.cycle == 12
    assert read.category == "tech"
    assert read.items_received == 2
    assert read.items_new == 0
    assert read.frontier_id == "9000"
    assert read.previous_frontier_id is None
    assert read.target_hash == hashlib.sha256(CUSTOM_SEARCH_URL.encode()).hexdigest()[:8]
    assert CUSTOM_SEARCH_URL not in caplog.text
    phases = [
        getattr(record, "phase", "")
        for record in caplog.records
        if getattr(record, "event", "") == "alert_latency"
    ]
    assert phases.index("collection") < phases.index("catalogue_read")
    assert phases[-1] == "catalogue_read"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "expected"),
    [(VintedCollectionError("réseau indisponible"), "error"), (None, "cancelled")],
)
async def test_an_unread_catalogue_claims_no_received_count(
    caplog: pytest.LogCaptureFixture,
    raised: BaseException | None,
    expected: str,
) -> None:
    """Une lecture échouée garde son marqueur d'erreur et ne fabrique aucun compteur."""
    import asyncio

    failure = raised if raised is not None else asyncio.CancelledError()
    with (
        patch(
            "app.tasks.vinted_poller.fetch_vinted_page",
            new_callable=AsyncMock,
            side_effect=failure,
        ),
        caplog.at_level(logging.INFO),
        pytest.raises(type(failure)),
    ):
        await poll_vinted_once(
            seen_ids=set(),
            vinted_client=AsyncMock(spec=httpx.AsyncClient),
            target_url=CUSTOM_SEARCH_URL,
            category_slug="tech",
            cycle_index=3,
            frontier=CycleFrontier(started_monotonic=9.0),
        )

    reads = [r for r in caplog.records if getattr(r, "phase", None) == "catalogue_read"]
    assert len(reads) == 1
    read = reads[0]
    assert read.result == expected
    assert read.cycle == 3
    assert read.category == "tech"
    assert read.items_received is None
    assert read.items_new is None
    assert read.frontier_id is None
    assert read.previous_frontier_id is None
    collections = [r for r in caplog.records if getattr(r, "phase", None) == "collection"]
    assert [record.result for record in collections] == [expected]
    phases = [
        getattr(record, "phase", "")
        for record in caplog.records
        if getattr(record, "event", "") == "alert_latency"
    ]
    assert phases.index("collection") < phases.index("catalogue_read")


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
            return_value=FifoSet(),
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
        patch("app.tasks.vinted_poller.remaining_cycle_sleep", side_effect=spy_remaining),
    ):
        await run_vinted_poller(stop_event)

    assert len(calls) == 1
    sleep_time, work_seconds = calls[0]
    assert sleep_time == 15.0
    assert work_seconds >= 0.05


def test_availability_ledger_sums_and_expires_old_events() -> None:
    """Le registre somme par catégorie et oublie les cycles sortis de la fenêtre."""
    ledger = AvailabilityLedger(window_seconds=100.0)
    ledger.add("scanned", 30.0, now=0.0)
    ledger.add("blocked", 10.0, now=30.0)
    ledger.add("scanned", 20.0, now=40.0)

    snapshot = ledger.snapshot(now=50.0)
    assert snapshot["scanned_s"] == pytest.approx(50.0)
    assert snapshot["blocked_s"] == pytest.approx(10.0)
    assert snapshot["window_s"] == pytest.approx(60.0)

    expired = ledger.snapshot(now=500.0)
    assert expired["window_s"] == pytest.approx(0.0)
    assert expired["scanned_s"] == pytest.approx(0.0)


def test_availability_ledger_ignores_zero_span_and_rejects_unknown_category() -> None:
    """Une durée nulle n'ouvre aucun événement ; une catégorie inconnue est refusée."""
    ledger = AvailabilityLedger()
    ledger.add("scanned", 0.0, now=0.0)
    assert ledger.snapshot(now=0.0)["window_s"] == 0.0
    with pytest.raises(ValueError, match="catégorie de disponibilité inconnue"):
        ledger.add("inconnue", 1.0, now=0.0)


def test_search_freshness_snapshot_reports_the_oldest_scan() -> None:
    """L'âge rendu est celui du plus ancien dernier scan réussi."""
    first_seen = {("u1", "tech"): 100.0, ("u2", "jeux"): 100.0}
    last_success = {("u1", "tech"): 130.0}

    assert search_freshness_snapshot(first_seen, last_success, now=140.0) == {
        "count": 2,
        "worst_age_s": 40.0,
    }
    assert search_freshness_snapshot({}, {}, now=140.0) is None


@pytest.mark.asyncio
async def test_cycle_journal_and_status_expose_observability(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Des cycles nominaux laissent une ligne structurée et alimentent les champs de /status."""
    import asyncio

    from app.core.config import settings

    monkeypatch.setattr(settings, "VINTED_POLLER_ENABLED", True)
    stop_event = asyncio.Event()
    calls = {"count": 0}
    statuses: list[dict[str, object]] = []

    async def fake_poll(*_args: object, **_kwargs: object) -> int:
        calls["count"] += 1
        if calls["count"] == 2:
            stop_event.set()
        return 0

    async def fake_save(data: dict[str, object]) -> None:
        statuses.append(dict(data))

    with (
        patch("app.tasks.vinted_poller.is_night_time", return_value=False),
        patch(
            "app.tasks.vinted_poller.load_initial_seen_ids",
            new_callable=AsyncMock,
            return_value=FifoSet(),
        ),
        patch("app.tasks.vinted_poller.poll_vinted_once", side_effect=fake_poll),
        patch(
            "app.tasks.vinted_poller.get_active_custom_searches_cached",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("app.tasks.vinted_poller.save_poller_status", side_effect=fake_save),
        patch("app.tasks.vinted_poller.get_adaptive_poll_interval", return_value=15.0),
        patch("app.tasks.vinted_poller.random.uniform", return_value=0.0),
        patch("app.tasks.vinted_poller.remaining_cycle_sleep", return_value=0.0),
        caplog.at_level(logging.INFO, logger="radar.vinted_poller"),
    ):
        await run_vinted_poller(stop_event)

    journal = [r for r in caplog.records if getattr(r, "event", None) == "poller_cycle"]
    assert len(journal) == 2
    assert all(record.outcome == "scanned" for record in journal)
    assert all(record.requests == {"api": 0, "html": 0, "mint": 0} for record in journal)

    active = [status for status in statuses if status.get("status") == "active"]
    assert len(active) == 2
    availability = active[-1]["availability"]
    assert isinstance(availability, dict)
    assert set(availability) == {
        "window_s",
        "scanned_s",
        "night_s",
        "blocked_s",
        "network_error_s",
        "unexpected_error_s",
    }
    assert availability["window_s"] == pytest.approx(
        availability["scanned_s"]
        + availability["night_s"]
        + availability["blocked_s"]
        + availability["network_error_s"]
        + availability["unexpected_error_s"]
    )
    assert active[-1]["requests_60s"] == {"api": 0, "html": 0, "mint": 0}
    assert active[-1]["search_stats"] is None


@pytest.mark.asyncio
async def test_consecutive_collection_failures_alert_once_and_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cinq cycles sans collecte notifient une fois ; la reprise notifie une fois."""
    import asyncio

    from app.core.config import settings

    monkeypatch.setattr(settings, "VINTED_POLLER_ENABLED", True)
    stop_event = asyncio.Event()
    calls = {"count": 0}
    statuses: list[dict[str, object]] = []

    async def fake_poll(*_args: object, **_kwargs: object) -> int:
        calls["count"] += 1
        if calls["count"] <= COLLECTION_FAILURE_ALERT_THRESHOLD:
            raise VintedCollectionError("réseau indisponible")
        stop_event.set()
        return 0

    async def fake_save(data: dict[str, object]) -> None:
        statuses.append(dict(data))

    with (
        patch("app.tasks.vinted_poller.is_night_time", return_value=False),
        patch(
            "app.tasks.vinted_poller.load_initial_seen_ids",
            new_callable=AsyncMock,
            return_value=FifoSet(),
        ),
        patch("app.tasks.vinted_poller.poll_vinted_once", side_effect=fake_poll),
        patch(
            "app.tasks.vinted_poller.get_active_custom_searches_cached",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("app.tasks.vinted_poller.save_poller_status", side_effect=fake_save),
        patch("app.tasks.vinted_poller.get_adaptive_poll_interval", return_value=15.0),
        patch("app.tasks.vinted_poller.random.uniform", return_value=0.0),
        patch("app.tasks.vinted_poller.remaining_cycle_sleep", return_value=0.0),
        patch("app.tasks.vinted_poller.notify_admins", new_callable=AsyncMock) as notify,
    ):
        await run_vinted_poller(stop_event)
        for _ in range(3):
            await asyncio.sleep(0)

    assert calls["count"] == COLLECTION_FAILURE_ALERT_THRESHOLD + 1
    assert statuses
    assert statuses[0]["status"] == "active"
    texts = [call.kwargs["text"] for call in notify.await_args_list]
    assert len(texts) == 2
    assert "dégradée" in texts[0]
    assert "rétablie" in texts[1]
    # Un titre d'alerte se lit avec un tiret normal, sans cadratin.
    for text in texts:
        assert "—" not in text


@pytest.mark.asyncio
async def test_block_cycle_reports_the_origin_hint_and_keeps_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un 403 s'arrête avec l'indice d'en-tête, un statut cooldown et la disponibilité."""
    import asyncio

    from app.core.config import settings

    monkeypatch.setattr(settings, "VINTED_POLLER_ENABLED", True)
    stop_event = asyncio.Event()
    statuses: list[dict[str, object]] = []

    async def fake_poll(*_args: object, **_kwargs: object) -> int:
        stop_event.set()
        raise VintedBlockError(403, "Blocage Vinted (403)", origin_hint="cloudflare")

    async def fake_save(data: dict[str, object]) -> None:
        statuses.append(dict(data))

    with (
        patch("app.tasks.vinted_poller.is_night_time", return_value=False),
        patch(
            "app.tasks.vinted_poller.load_initial_seen_ids",
            new_callable=AsyncMock,
            return_value=FifoSet(),
        ),
        patch("app.tasks.vinted_poller.poll_vinted_once", side_effect=fake_poll),
        patch("app.tasks.vinted_poller.save_poller_status", side_effect=fake_save),
        patch(
            "app.tasks.vinted_poller.vinted_session_manager.clear_session",
            new_callable=AsyncMock,
        ),
        patch("app.tasks.vinted_poller.notify_admins", new_callable=AsyncMock) as notify,
    ):
        await run_vinted_poller(stop_event)
        for _ in range(3):
            await asyncio.sleep(0)

    assert statuses[0]["status"] == "cooldown"
    assert statuses[0]["reason"] == "Blocage Vinted (403, indice: cloudflare)"
    assert isinstance(statuses[0]["availability"], dict)
    texts = [call.kwargs["text"] for call in notify.await_args_list]
    assert any("Pause de sécurité" in text for text in texts)
    for text in texts:
        assert "—" not in text


@pytest.mark.asyncio
async def test_custom_search_scan_updates_the_freshness_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un scan personnalisé réussi alimente la fraîcheur rendue par /status."""
    import asyncio

    from app.core.config import settings

    monkeypatch.setattr(settings, "VINTED_POLLER_ENABLED", True)
    stop_event = asyncio.Event()
    statuses: list[dict[str, object]] = []
    custom_url = "https://v.fr/custom"

    async def fake_poll(*_args: object, **kwargs: object) -> int:
        if kwargs.get("target_url") == custom_url:
            stop_event.set()
        return 0

    async def fake_save(data: dict[str, object]) -> None:
        statuses.append(dict(data))

    with (
        patch("app.tasks.vinted_poller.is_night_time", return_value=False),
        patch(
            "app.tasks.vinted_poller.load_initial_seen_ids",
            new_callable=AsyncMock,
            return_value=FifoSet(),
        ),
        patch("app.tasks.vinted_poller.poll_vinted_once", side_effect=fake_poll),
        patch(
            "app.tasks.vinted_poller.get_active_custom_searches_cached",
            new_callable=AsyncMock,
            return_value=[(custom_url, "tech")],
        ),
        patch("app.tasks.vinted_poller.save_poller_status", side_effect=fake_save),
        patch("app.tasks.vinted_poller.get_adaptive_poll_interval", return_value=15.0),
        patch("app.tasks.vinted_poller.random.uniform", return_value=0.0),
        patch("app.tasks.vinted_poller.remaining_cycle_sleep", return_value=0.0),
    ):
        await run_vinted_poller(stop_event)

    active = [status for status in statuses if status.get("status") == "active"]
    assert len(active) == 1
    search_stats = active[0]["search_stats"]
    assert isinstance(search_stats, dict)
    assert search_stats["count"] == 1
    assert 0 <= search_stats["worst_age_s"] < 5


@pytest.mark.asyncio
async def test_night_transitions_leave_structured_journal_lines(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """L'entrée et la sortie de la veille de nuit laissent chacune une ligne dédiée."""
    import asyncio

    from app.core.config import settings

    monkeypatch.setattr(settings, "VINTED_POLLER_ENABLED", True)
    stop_event = asyncio.Event()
    statuses: list[dict[str, object]] = []

    async def fake_poll(*_args: object, **_kwargs: object) -> int:
        stop_event.set()
        return 0

    async def fake_save(data: dict[str, object]) -> None:
        statuses.append(dict(data))

    with (
        patch(
            "app.tasks.vinted_poller.is_night_time",
            side_effect=[True, False, False, False],
        ),
        patch(
            "app.tasks.vinted_poller.get_night_mode_remaining_seconds",
            return_value=3600.0,
        ),
        patch(
            "app.tasks.vinted_poller.load_initial_seen_ids",
            new_callable=AsyncMock,
            return_value=FifoSet(),
        ),
        patch("app.tasks.vinted_poller.poll_vinted_once", side_effect=fake_poll),
        patch(
            "app.tasks.vinted_poller.get_active_custom_searches_cached",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("app.tasks.vinted_poller.save_poller_status", side_effect=fake_save),
        patch(
            "app.tasks.vinted_poller.vinted_session_manager.clear_session",
            new_callable=AsyncMock,
        ),
        patch("app.tasks.vinted_poller.get_adaptive_poll_interval", return_value=15.0),
        patch("app.tasks.vinted_poller.random.uniform", return_value=0.0),
        patch("app.tasks.vinted_poller.remaining_cycle_sleep", return_value=0.0),
        caplog.at_level(logging.INFO, logger="radar.vinted_poller"),
    ):
        await run_vinted_poller(stop_event)

    night = [r for r in caplog.records if getattr(r, "event", None) == "poller_night"]
    assert [record.phase for record in night] == ["start", "end"]
    assert night[0].remaining_s == 3600.0
    assert night[1].duration_s >= 0.0
    assert "Veille de nuit : entrée" in caplog.text
    assert "Veille de nuit : sortie" in caplog.text
    assert statuses[0]["status"] == "night_mode"


@pytest.mark.asyncio
async def test_proxy_url_without_the_flag_is_ignored_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Une URL de proxy sans flag n'active rien et laisse un avertissement."""
    import asyncio

    from app.core.config import settings

    monkeypatch.setattr(settings, "VINTED_POLLER_ENABLED", True)
    monkeypatch.setattr(settings, "VINTED_PROXY_ENABLED", False)
    monkeypatch.setattr(settings, "VINTED_PROXY_URL", "http://10.0.0.1:8888")
    stop_event = asyncio.Event()

    async def fake_poll(*_args: object, **_kwargs: object) -> int:
        stop_event.set()
        return 0

    with (
        patch("app.tasks.vinted_poller.is_night_time", return_value=False),
        patch(
            "app.tasks.vinted_poller.load_initial_seen_ids",
            new_callable=AsyncMock,
            return_value=FifoSet(),
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
        patch("app.tasks.vinted_poller.remaining_cycle_sleep", return_value=0.0),
        caplog.at_level(logging.WARNING, logger="radar.vinted_poller"),
    ):
        await run_vinted_poller(stop_event)

    assert "VINTED_PROXY_ENABLED" in caplog.text
    assert "proxy ignoré" in caplog.text


@pytest.fixture(autouse=True)
async def isolated_baseline_storage(monkeypatch):
    """Les nouveaux jalons du poller sont persistés dans une base propre à chaque test."""
    from tests.database import sqlite_only_database

    vinted_request_stats.reset()
    async with sqlite_only_database() as database:
        monkeypatch.setattr("app.tasks.vinted_poller.AsyncSessionLocal", database.maker)
        yield

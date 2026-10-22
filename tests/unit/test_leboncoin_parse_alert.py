"""Tests de l'alerte sur structure de page Leboncoin inattendue."""

import pytest

from app.core.config import settings
from app.tasks import leboncoin_poller
from app.tasks.leboncoin_poller import COLLECTION_FAILURE_ALERT_THRESHOLD, SILENCE_ALERT_SECONDS
from tests.unit.test_leboncoin_block_stop import LoopHarness


def _parse_alerts(harness: LoopHarness) -> list[str]:
    return [text for text in harness.alerts if "Structure de page" in text]


def _parse_recoveries(harness: LoopHarness) -> list[str]:
    return [text for text in harness.alerts if "structure connue" in text]


@pytest.mark.asyncio
async def test_parse_failures_raise_a_dedicated_alert_once(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["parse"] * 8)
    await harness.run()

    alerts = _parse_alerts(harness)
    assert len(alerts) == 1, "une seule alerte par série"
    assert "inattendue" in alerts[0]
    assert str(COLLECTION_FAILURE_ALERT_THRESHOLD) in alerts[0]


@pytest.mark.asyncio
async def test_the_parse_counter_is_reported_in_the_status(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["parse"] * 3)
    await harness.run()

    parse_statuses = [s for s in harness.statuses if s.get("status") == "parse_failure"]
    assert parse_statuses, "l'état signale la rupture de structure"
    assert [s.get("consecutive_parse_failures") for s in parse_statuses] == [1, 2, 3]


@pytest.mark.asyncio
async def test_no_alert_below_the_threshold(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(
        monkeypatch, ["parse"] * (COLLECTION_FAILURE_ALERT_THRESHOLD - 1) + ["stop"]
    )
    await harness.run()

    assert _parse_alerts(harness) == []


@pytest.mark.asyncio
async def test_a_successful_cycle_resets_the_counter_and_announces_recovery(
    monkeypatch: pytest.MonkeyPatch,
):
    series = ["parse"] * (COLLECTION_FAILURE_ALERT_THRESHOLD - 1)
    harness = LoopHarness(monkeypatch, series + ["ok"] + series + ["stop"])
    await harness.run()

    assert _parse_alerts(harness) == [], "le compteur a bien été remis à zéro par le succès"


@pytest.mark.asyncio
async def test_recovery_is_announced_after_an_alerted_series(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(
        monkeypatch, ["parse"] * COLLECTION_FAILURE_ALERT_THRESHOLD + ["ok", "stop"]
    )
    await harness.run()

    assert len(_parse_alerts(harness)) == 1
    assert len(_parse_recoveries(harness)) == 1


@pytest.mark.asyncio
async def test_a_second_series_alerts_again(monkeypatch: pytest.MonkeyPatch):
    series = ["parse"] * COLLECTION_FAILURE_ALERT_THRESHOLD
    harness = LoopHarness(monkeypatch, series + ["ok"] + series + ["stop"])
    await harness.run()

    assert len(_parse_alerts(harness)) == 2, "chaque nouvelle série alerte une fois"
    assert len(_parse_recoveries(harness)) == 1, "un seul rétablissement constaté"


@pytest.mark.asyncio
async def test_parse_failures_never_open_an_episode_or_stop_the_collector(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["parse"] * 15 + ["ok", "stop"])
    await harness.run()

    assert leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY not in harness.double.store
    assert not any(s.get("status") == "durable_block" for s in harness.statuses)
    assert harness.requests, "la collecte continue après une rupture de structure"


@pytest.mark.asyncio
async def test_the_silence_alert_applies_to_the_parse_path(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["parse"] * 30)
    await harness.run()

    silence = [text for text in harness.alerts if "muette" in text]
    assert silence, "l'alerte de silence couvre aussi les erreurs de parsing"
    assert harness.waits, "des attentes ont bien eu lieu"
    total = sum(harness.waits)
    assert total > SILENCE_ALERT_SECONDS


@pytest.mark.asyncio
async def test_parse_errors_during_an_open_episode_respect_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block"] + ["parse"] * 20 + ["ok"])
    await harness.run()

    deadline = harness.clock_start + settings.LEBONCOIN_BLOCK_STOP_SECONDS
    assert all(attempt < deadline for attempt in harness.requests)
    assert any(s.get("status") == "durable_block" for s in harness.statuses)


@pytest.mark.asyncio
async def test_the_parse_alert_leaks_no_page_content(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["parse"] * COLLECTION_FAILURE_ALERT_THRESHOLD)
    await harness.run()

    alert = _parse_alerts(harness)[0]
    for forbidden in ("__NEXT_DATA__", "http://", "https://", "searchData"):
        assert forbidden not in alert, f"aucun détail brut dans l'alerte ({forbidden})"


@pytest.mark.asyncio
async def test_a_failing_parse_alert_does_not_stop_collection(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["parse"] * COLLECTION_FAILURE_ALERT_THRESHOLD + ["ok"])
    harness.send_error = RuntimeError("Telegram indisponible")
    await harness.run()

    assert harness.tasks
    assert all(task.done() for task in harness.tasks)
    assert harness.requests, "la collecte continue malgré l'échec d'alerte"

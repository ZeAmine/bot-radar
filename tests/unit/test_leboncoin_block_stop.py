"""Tests d'acceptation de l'arrêt Leboncoin sur blocage durable (A3)."""

import json

import pytest

from app.core.config import settings
from app.tasks import leboncoin_poller
from app.tasks.leboncoin_poller import (
    BlockEpisode,
    BlockEpisodeError,
    _bounded_wait,
    _parse_block_episode,
)
from tests.unit._leboncoin_harness import (
    LoopHarness,
    _install_redis,
    _RedisDouble,
)


def test_default_stop_threshold_is_two_hours():
    assert settings.LEBONCOIN_BLOCK_STOP_SECONDS == 7200


def test_block_stop_seconds_rejects_non_positive(monkeypatch: pytest.MonkeyPatch):
    from pydantic import ValidationError

    from app.core.config import Settings

    for value in ("0", "-1"):
        with monkeypatch.context() as patch:
            patch.setenv("LEBONCOIN_BLOCK_STOP_SECONDS", value)
            with pytest.raises(ValidationError):
                Settings()


def test_bounded_wait_is_capped_by_remaining_episode_time(monkeypatch: pytest.MonkeyPatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(leboncoin_poller, "_now", lambda: clock["now"])
    monkeypatch.setattr(settings, "LEBONCOIN_BLOCK_STOP_SECONDS", 7200)

    episode = BlockEpisode(started_at=clock["now"])

    assert _bounded_wait(7200.0, None) == 7200.0
    assert _bounded_wait(7200.0, episode) == 7200.0

    clock["now"] += 6900.0
    assert _bounded_wait(7200.0, episode) == pytest.approx(300.0)

    clock["now"] += 400.0
    assert _bounded_wait(7200.0, episode) == 0.0


def test_bounded_wait_applies_to_network_backoff_and_normal_cadence(
    monkeypatch: pytest.MonkeyPatch,
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(leboncoin_poller, "_now", lambda: clock["now"])
    monkeypatch.setattr(settings, "LEBONCOIN_BLOCK_STOP_SECONDS", 7200)
    episode = BlockEpisode(started_at=clock["now"])
    clock["now"] += 7180.0

    assert _bounded_wait(1800.0, episode) == pytest.approx(20.0)
    assert _bounded_wait(600.0, episode) == pytest.approx(20.0)


# --- lecture, validation et écriture de l'épisode --------------------------


def test_parse_block_episode_accepts_absence_and_valid_state():
    assert _parse_block_episode(None) is None
    parsed = _parse_block_episode({"started_at": 10.0})
    assert parsed is not None
    assert parsed.started_at == 10.0
    stopped = _parse_block_episode({"started_at": 10.0, "stopped_at": 20.0})
    assert stopped is not None
    assert stopped.stopped_at == 20.0


@pytest.mark.parametrize(
    "raw",
    [
        "not-an-object",
        {},
        {"started_at": "hier"},
        {"started_at": -5},
        {"started_at": 10.0, "stopped_at": 5.0},
        {"started_at": 10.0, "stopped_at": "jamais"},
        {"started_at": float("nan")},
        {"started_at": float("inf")},
        {"started_at": float("-inf")},
        {"started_at": 10.0, "stopped_at": float("nan")},
        {"started_at": 10.0, "stopped_at": float("inf")},
        {"started_at": 10**400},
        {"started_at": True},
        {"started_at": 10.0, "stopped_at": False},
    ],
)
def test_parse_block_episode_rejects_invalid_state(raw: object):
    with pytest.raises(BlockEpisodeError):
        _parse_block_episode(raw)


@pytest.mark.parametrize(
    "payload",
    [
        '{"started_at": NaN}',
        '{"started_at": Infinity}',
        '{"started_at": -Infinity}',
        '{"started_at": 1e999}',
        '{"started_at": 1e309, "stopped_at": 1e309}',
    ],
)
@pytest.mark.asyncio
async def test_non_finite_persisted_state_fails_closed(
    monkeypatch: pytest.MonkeyPatch, payload: str
):
    double = _RedisDouble()
    double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = payload
    _install_redis(monkeypatch, double)

    with pytest.raises(BlockEpisodeError):
        await leboncoin_poller.load_block_episode()


@pytest.mark.asyncio
async def test_load_block_episode_distinguishes_absence_from_read_error(
    monkeypatch: pytest.MonkeyPatch,
):
    double = _RedisDouble()
    _install_redis(monkeypatch, double)

    assert await leboncoin_poller.load_block_episode() is None

    double.read_error = RuntimeError("redis down")
    with pytest.raises(BlockEpisodeError):
        await leboncoin_poller.load_block_episode()


@pytest.mark.asyncio
async def test_load_block_episode_rejects_unreadable_json(monkeypatch: pytest.MonkeyPatch):
    double = _RedisDouble()
    double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = "{pas du json"
    _install_redis(monkeypatch, double)

    with pytest.raises(BlockEpisodeError):
        await leboncoin_poller.load_block_episode()


@pytest.mark.asyncio
async def test_block_episode_is_persisted_without_ttl(monkeypatch: pytest.MonkeyPatch):
    double = _RedisDouble()
    _install_redis(monkeypatch, double)

    await leboncoin_poller.save_block_episode(BlockEpisode(started_at=123.0))

    assert double.writes == [
        (leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY, '{"started_at": 123.0}', None)
    ]


@pytest.mark.asyncio
async def test_save_block_episode_reports_write_failure(monkeypatch: pytest.MonkeyPatch):
    double = _RedisDouble()
    double.write_error = RuntimeError("écriture refusée")
    _install_redis(monkeypatch, double)

    with pytest.raises(BlockEpisodeError):
        await leboncoin_poller.save_block_episode(BlockEpisode(started_at=1.0))


@pytest.mark.asyncio
async def test_clear_block_episode_reports_delete_failure(monkeypatch: pytest.MonkeyPatch):
    double = _RedisDouble()
    double.delete_error = RuntimeError("suppression refusée")
    _install_redis(monkeypatch, double)

    with pytest.raises(BlockEpisodeError):
        await leboncoin_poller.clear_block_episode()


# --- échec de persistance à la transition vers l'arrêt ----------------------


@pytest.mark.asyncio
async def test_stop_with_unpersisted_state_reports_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block", "block", "block", "block", "ok"])
    original_set = harness.double.set
    stop_write_attempts: list[str] = []

    async def failing_set(key: str, value: str, ex: int | None = None) -> None:
        if "stopped_at" in value:
            stop_write_attempts.append(value)
            raise RuntimeError("écriture refusée à l'arrêt")
        await original_set(key, value, ex)

    harness.double.set = failing_set  # type: ignore[method-assign]
    await harness.run()

    assert stop_write_attempts, "l'écriture d'arrêt a été tentée"
    assert harness.statuses[-1].get("status") == "durable_block"
    assert harness.statuses[-1].get("reason") == "persistence_failed"
    assert not any(s.get("reason") == "durable_block" for s in harness.statuses)
    assert not any(s.get("status") == "stopped" for s in harness.statuses)
    assert len(harness.stop_alerts()) == 1
    assert "n'a pas pu être enregistré" in harness.stop_alerts()[0]
    deadline = harness.clock_start + settings.LEBONCOIN_BLOCK_STOP_SECONDS
    assert all(attempt < deadline for attempt in harness.requests)


@pytest.mark.asyncio
async def test_expired_episode_at_startup_with_unpersisted_stop(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["ok"])
    started_at = harness.clock_start - 10_000.0
    harness.double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = (
        f'{{"started_at": {started_at}}}'
    )
    original_set = harness.double.set

    async def failing_set(key: str, value: str, ex: int | None = None) -> None:
        if "stopped_at" in value:
            raise RuntimeError("écriture refusée à l'arrêt")
        await original_set(key, value, ex)

    harness.double.set = failing_set  # type: ignore[method-assign]
    await harness.run()

    assert harness.requests == [], "aucune tentative malgré l'échec d'écriture"
    assert harness.statuses[-1].get("status") == "durable_block"
    assert harness.statuses[-1].get("reason") == "persistence_failed"
    assert len(harness.stop_alerts()) == 1
    assert "n'a pas pu être enregistré" in harness.stop_alerts()[0]


# --- isolation du harnais ---------------------------------------------------


def test_the_harness_does_not_replace_global_wait_for(monkeypatch: pytest.MonkeyPatch):
    import asyncio as stdlib_asyncio

    original = stdlib_asyncio.wait_for
    LoopHarness(monkeypatch, ["stop"])

    assert leboncoin_poller.asyncio is stdlib_asyncio
    assert stdlib_asyncio.wait_for is original, "asyncio.wait_for reste intact"


@pytest.mark.asyncio
async def test_a_wait_outside_the_collector_is_not_intercepted(
    monkeypatch: pytest.MonkeyPatch,
):
    import asyncio as stdlib_asyncio

    harness = LoopHarness(monkeypatch, ["stop"])

    async def completes_late() -> str:
        await stdlib_asyncio.sleep(10)
        return "terminé"

    with pytest.raises(TimeoutError):
        await stdlib_asyncio.wait_for(completes_late(), timeout=0.01)

    assert harness.waits == [], "l'attente externe n'est pas passée par le double"


# --- harnais de boucle ------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_stops_without_further_request(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["block", "block", "block", "block", "ok"])
    await harness.run()

    deadline = harness.clock_start + settings.LEBONCOIN_BLOCK_STOP_SECONDS
    assert harness.requests, "au moins un refus doit être observé"
    assert all(t < deadline for t in harness.requests), (
        "aucune tentative ne doit démarrer à la deadline ni après"
    )
    assert any(s.get("status") == "durable_block" for s in harness.statuses)
    assert not any(
        s.get("status") == "stopped" for s in harness.statuses
    ), "l'arrêt propre ne doit pas écraser durable_block"
    assert len(harness.stop_alerts()) == 1


@pytest.mark.asyncio
async def test_episode_closes_after_a_successful_cycle(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(
        monkeypatch, ["block", "block", "ok", "block", "block", "block", "ok", "stop"]
    )
    await harness.run()

    assert leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY not in harness.double.store
    assert not any(s.get("status") == "durable_block" for s in harness.statuses)


@pytest.mark.asyncio
async def test_network_errors_do_not_clear_the_open_episode(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["block"] + ["network"] * 12 + ["ok"])
    await harness.run()

    assert len(harness.requests) >= 2
    assert any(s.get("status") == "durable_block" for s in harness.statuses)
    assert len(harness.stop_alerts()) == 1


@pytest.mark.asyncio
async def test_parse_errors_do_not_open_the_episode(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["parse"] * 6 + ["stop"])
    await harness.run()

    assert leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY not in harness.double.store
    assert harness.stop_alerts() == []


@pytest.mark.asyncio
async def test_parse_errors_respect_an_open_episode_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block"] + ["parse"] * 12 + ["ok"])
    await harness.run()

    assert any(s.get("status") == "durable_block" for s in harness.statuses)


@pytest.mark.asyncio
async def test_generic_errors_respect_the_open_episode_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block"] + ["generic"] * 12 + ["ok"])
    await harness.run()

    assert any(s.get("status") == "durable_block" for s in harness.statuses)


@pytest.mark.asyncio
async def test_no_stop_before_the_deadline(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["block", "ok", "block", "ok", "stop"])
    await harness.run()

    assert harness.stop_alerts() == []
    assert not any(s.get("status") == "durable_block" for s in harness.statuses)


@pytest.mark.asyncio
async def test_persistence_failure_suspends_collection(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["block", "block", "block"])
    harness.double.write_error = RuntimeError("redis en écriture seule refusée")
    await harness.run()

    assert any(s.get("reason") == "persistence_failed" for s in harness.statuses)
    assert not any(s.get("status") == "stopped" for s in harness.statuses)


@pytest.mark.asyncio
async def test_clear_failure_suspends_instead_of_collecting_silently(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block", "ok", "ok"])
    harness.double.delete_error = RuntimeError("suppression refusée")
    await harness.run()

    assert any(s.get("reason") == "persistence_failed" for s in harness.statuses)


@pytest.mark.asyncio
async def test_a_failing_telegram_send_does_not_prevent_the_stop(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block", "block", "block", "block", "ok"])
    harness.send_error = RuntimeError("Telegram 403 : bot bloqué par l'utilisateur")
    await harness.run()

    deadline = harness.clock_start + settings.LEBONCOIN_BLOCK_STOP_SECONDS
    assert harness.sent, "un envoi a bien été tenté"
    assert all(task.done() for task in harness.tasks), "les envois en échec se sont terminés"
    assert all(
        isinstance(task.exception(), RuntimeError) for task in harness.tasks
    ), "chaque envoi a réellement échoué"
    assert any(task.get_name() == "notify_leboncoin_durable_stop" for task in harness.tasks)
    assert harness.statuses[-1].get("status") == "durable_block"
    assert all(attempt < deadline for attempt in harness.requests)
    assert harness.requests[-1] < deadline, "aucune tentative après l'arrêt"
    persisted = harness.double.store.get(leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY)
    assert persisted is not None
    assert "stopped_at" in persisted, "l'arrêt reste enregistré malgré l'échec d'envoi"


@pytest.mark.asyncio
async def test_an_attempt_never_starts_at_the_exact_deadline(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["block", "ok", "ok", "ok"])
    started_at = harness.clock_start - settings.LEBONCOIN_BLOCK_STOP_SECONDS
    harness.double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = (
        f'{{"started_at": {started_at}}}'
    )
    await harness.run()

    assert started_at + settings.LEBONCOIN_BLOCK_STOP_SECONDS == harness.clock_start, (
        "l'horloge vaut exactement la deadline au démarrage"
    )
    assert harness.requests == [], "aucune tentative ne démarre à la deadline"
    assert harness.statuses[-1].get("status") == "durable_block"
    assert len(harness.stop_alerts()) == 1


# --- redémarrage ------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_resumes_window_with_preserved_start(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["block", "ok", "stop"])
    original_start = 999_000.0
    harness.double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = (
        f'{{"started_at": {original_start}}}'
    )
    await harness.run()

    attempts = [(kind, value) for kind, value in harness.double.events if kind == "attempt"]
    first_attempt = json.loads(attempts[0][1] or "{}")
    second_attempt = json.loads(attempts[1][1] or "{}")

    assert first_attempt.get("started_at") == original_start, (
        "la tentative de reprise voit la date de départ d'origine"
    )
    assert second_attempt.get("started_at") == original_start, (
        "le refus suivant ne remplace pas la date de départ"
    )
    assert original_start + settings.LEBONCOIN_BLOCK_STOP_SECONDS > harness.clock_start, (
        "la deadline d'origine n'est pas atteinte au redémarrage"
    )

    deletes_before_success = [
        value for kind, value in harness.double.events[:2] if kind == "delete"
    ]
    assert deletes_before_success == [], "aucun effacement avant un cycle réussi"

    kinds = [kind for kind, _ in harness.double.events]
    assert kinds.index("delete") > kinds.index("attempt", kinds.index("attempt") + 1)

    assert leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY not in harness.double.store
    assert harness.double.deletes == [leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY]


@pytest.mark.asyncio
async def test_restart_without_success_keeps_the_original_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block"] * 12)
    original_start = harness.clock_start - 7_000.0
    harness.double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = (
        f'{{"started_at": {original_start}}}'
    )
    await harness.run()

    deadline = original_start + settings.LEBONCOIN_BLOCK_STOP_SECONDS
    assert deadline == harness.clock_start + 200.0, "la deadline dérive de la date d'origine"
    assert all(t < deadline for t in harness.requests)
    assert any(s.get("status") == "durable_block" for s in harness.statuses)
    assert harness.double.deletes == [], "aucun effacement sans cycle réussi"


@pytest.mark.asyncio
async def test_restart_with_expired_episode_stops_without_request(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["ok"])
    harness.double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = (
        '{"started_at": 990000.0}'
    )
    await harness.run()

    assert harness.requests == []
    assert any(s.get("status") == "durable_block" for s in harness.statuses)
    assert len(harness.stop_alerts()) == 1


@pytest.mark.asyncio
async def test_restart_with_recorded_stop_stays_stopped_without_replaying_alert(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["ok"])
    harness.double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = (
        '{"started_at": 990000.0, "stopped_at": 990100.0}'
    )
    await harness.run()

    assert harness.requests == []
    assert harness.alerts == [], "aucune alerte rejouée au redémarrage"
    assert any(s.get("reason") == "durable_block" for s in harness.statuses)


@pytest.mark.asyncio
async def test_unreadable_episode_state_fails_closed(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["ok"])
    harness.double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = "{cassé"
    await harness.run()

    assert harness.requests == []
    assert any(s.get("reason") == "episode_unreadable" for s in harness.statuses)


@pytest.mark.asyncio
async def test_redis_read_failure_fails_closed(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["ok"])
    harness.double.read_error = RuntimeError("redis injoignable")
    await harness.run()

    assert harness.requests == []
    assert any(s.get("reason") == "episode_unreadable" for s in harness.statuses)


@pytest.mark.asyncio
async def test_missing_state_starts_collection(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["ok", "stop"])
    await harness.run()

    assert harness.requests
    assert not any(s.get("status") == "durable_block" for s in harness.statuses)


# --- /status ---------------------------------------------------------------


def test_status_line_reports_durable_stop(monkeypatch: pytest.MonkeyPatch):
    from datetime import UTC, datetime

    from app.bot.handlers.status import _leboncoin_status_line

    monkeypatch.setattr(settings, "LEBONCOIN_POLLER_ENABLED", True)
    now = datetime.now(UTC)

    durable = _leboncoin_status_line(
        {"status": "durable_block", "reason": "durable_block"}, now
    )
    assert "Arrêté sur blocage durable" in durable
    assert "reprise manuelle" in durable

    unreadable = _leboncoin_status_line(
        {"status": "durable_block", "reason": "episode_unreadable"}, now
    )
    assert "illisible" in unreadable

    failed = _leboncoin_status_line(
        {"status": "durable_block", "reason": "persistence_failed"}, now
    )
    assert "persistance impossible" in failed
    assert "reprise non garantie" in failed

    assert "Arrêté" in _leboncoin_status_line({"status": "stopped"}, now)


def test_status_line_keeps_disabled_and_unknown_states(monkeypatch: pytest.MonkeyPatch):
    from datetime import UTC, datetime

    from app.bot.handlers.status import _leboncoin_status_line

    monkeypatch.setattr(settings, "LEBONCOIN_POLLER_ENABLED", False)
    assert "Désactivé" in _leboncoin_status_line(None, datetime.now(UTC))

    monkeypatch.setattr(settings, "LEBONCOIN_POLLER_ENABLED", True)
    assert "Inconnu" in _leboncoin_status_line(None, datetime.now(UTC))


# --- visibilité de /status au-delà de 24 h ----------------------------------


@pytest.mark.asyncio
async def test_status_shows_a_recorded_stop_after_the_status_key_expired(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "LEBONCOIN_POLLER_ENABLED", True)
    from datetime import UTC, datetime

    from app.bot.handlers.status import _leboncoin_status_line, _resolve_leboncoin_status

    double = _RedisDouble()
    double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = (
        '{"started_at": 999000.0, "stopped_at": 1000000.0}'
    )
    _install_redis(monkeypatch, double)

    resolved = await _resolve_leboncoin_status(None)
    line = _leboncoin_status_line(resolved, datetime.now(UTC))

    assert "Arrêté sur blocage durable" in line
    assert "reprise manuelle" in line


@pytest.mark.asyncio
async def test_status_prefers_the_durable_stop_over_a_stale_status(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.bot.handlers.status import _resolve_leboncoin_status

    double = _RedisDouble()
    double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = (
        '{"started_at": 999000.0, "stopped_at": 1000000.0}'
    )
    _install_redis(monkeypatch, double)

    resolved = await _resolve_leboncoin_status({"status": "active", "last_poll_at": 1.0})
    assert resolved is not None
    assert resolved.get("status") == "durable_block"


@pytest.mark.asyncio
async def test_status_absence_stays_a_normal_state(monkeypatch: pytest.MonkeyPatch):
    from app.bot.handlers.status import _resolve_leboncoin_status

    double = _RedisDouble()
    _install_redis(monkeypatch, double)

    assert await _resolve_leboncoin_status(None) is None
    assert await _resolve_leboncoin_status({"status": "active"}) == {"status": "active"}


@pytest.mark.asyncio
async def test_status_reports_an_unreadable_episode_instead_of_a_healthy_state(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "LEBONCOIN_POLLER_ENABLED", True)
    from datetime import UTC, datetime

    from app.bot.handlers.status import _leboncoin_status_line, _resolve_leboncoin_status

    double = _RedisDouble()
    double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = "{cassé"
    _install_redis(monkeypatch, double)

    resolved = await _resolve_leboncoin_status(None)
    line = _leboncoin_status_line(resolved, datetime.now(UTC))
    assert "illisible" in line, "un état douteux ne doit pas être présenté comme sain"


@pytest.mark.asyncio
async def test_status_keeps_an_unpersisted_stop_visible(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "LEBONCOIN_POLLER_ENABLED", True)
    from datetime import UTC, datetime

    from app.bot.handlers.status import _leboncoin_status_line, _resolve_leboncoin_status

    double = _RedisDouble()
    double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = (
        '{"started_at": 999000.0, "stopped_at": 1000000.0}'
    )
    _install_redis(monkeypatch, double)

    unpersisted = {"status": "durable_block", "reason": "persistence_failed"}
    resolved = await _resolve_leboncoin_status(unpersisted)

    assert resolved == unpersisted, "persistence_failed n'est pas masqué par l'état persistant"
    assert "persistance impossible" in _leboncoin_status_line(resolved, datetime.now(UTC))


# --- alerte de suspension hors transition d'arrêt ---------------------------


@pytest.mark.asyncio
async def test_suspension_on_episode_opening_alerts_once(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["block", "block", "block"])
    harness.double.write_error = RuntimeError("redis refuse l'écriture")
    await harness.run()

    assert any(s.get("reason") == "persistence_failed" for s in harness.statuses)
    suspensions = [text for text in harness.alerts if "suspendue" in text]
    assert len(suspensions) == 1, "une seule alerte par suspension"
    assert "suspendue pour ce processus" in suspensions[0]
    assert "n'est pas garantie" in suspensions[0]


@pytest.mark.asyncio
async def test_suspension_on_clear_failure_alerts_once(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["block", "ok", "ok"])
    harness.double.delete_error = RuntimeError("redis refuse la suppression")
    await harness.run()

    suspensions = [text for text in harness.alerts if "suspendue" in text]
    assert len(suspensions) == 1
    assert any(s.get("reason") == "persistence_failed" for s in harness.statuses)


@pytest.mark.asyncio
async def test_the_suspension_alert_never_leaks_redis_detail(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block", "block", "block"])
    harness.double.write_error = RuntimeError("127.0.0.1:6379 ECONNREFUSED")
    await harness.run()

    suspensions = [text for text in harness.alerts if "suspendue" in text]
    assert len(suspensions) == 1
    assert "127.0.0.1" not in suspensions[0]
    assert "6379" not in suspensions[0]
    assert "ECONNREFUSED" not in suspensions[0]


@pytest.mark.asyncio
async def test_a_failing_suspension_alert_still_suspends(monkeypatch: pytest.MonkeyPatch):
    harness = LoopHarness(monkeypatch, ["block", "block", "block"])
    harness.double.write_error = RuntimeError("redis refuse l'écriture")
    harness.send_error = RuntimeError("Telegram indisponible")
    await harness.run()

    assert any(s.get("reason") == "persistence_failed" for s in harness.statuses)
    assert harness.tasks, "l'alerte a bien été tentée"
    assert all(task.done() for task in harness.tasks)


# --- ordre des messages à la réussite ---------------------------------------


@pytest.mark.asyncio
async def test_recovery_is_announced_only_after_the_episode_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block", "ok", "stop"])
    await harness.run()

    events = harness.double.events
    cleared_at = next(i for i, (kind, _) in enumerate(events) if kind == "delete")
    recovery_at = next(
        i for i, (kind, value) in enumerate(events) if kind == "alert" and "rétablie" in value
    )

    assert recovery_at > cleared_at, "l'annonce de rétablissement suit l'effacement"
    assert not any(
        kind == "write" and "stopped_at" in value for kind, value in events
    ), "aucun arrêt enregistré : l'épisode a bien été refermé par un succès"
    assert harness.double.deletes == [leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY]


@pytest.mark.asyncio
async def test_a_failed_clear_never_announces_a_full_recovery(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block", "ok", "ok"])
    harness.double.delete_error = RuntimeError("suppression refusée")
    await harness.run()

    assert not any("rétablie" in text for text in harness.sent), (
        "aucun rétablissement annoncé si l'épisode n'a pas pu être effacé"
    )
    assert not any("rétablie" in value for kind, value in harness.double.events if kind == "alert")
    assert any(s.get("reason") == "persistence_failed" for s in harness.statuses)


# --- précision de l'alerte d'arrêt ------------------------------------------


@pytest.mark.asyncio
async def test_the_stop_alert_reports_the_observed_duration_not_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["ok"])
    started_at = harness.clock_start - (7200.0 + 3600.0)
    harness.double.store[leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY] = (
        f'{{"started_at": {started_at}}}'
    )
    await harness.run()

    alert = harness.stop_alerts()[0]
    assert "180 min" in alert, "la durée observée est annoncée"
    assert "seuil d'arrêt : 120 min" in alert, "le seuil reste nommé comme tel"
    assert "depuis 120 min sans rétablissement" not in alert


@pytest.mark.asyncio
async def test_the_stop_alert_at_the_deadline_reports_the_threshold_duration(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = LoopHarness(monkeypatch, ["block", "block", "block", "block", "ok"])
    await harness.run()

    alert = harness.stop_alerts()[0]
    assert "min" in alert
    assert "seuil d'arrêt : 120 min" in alert


# --- aucune relance automatique après sortie de la tâche --------------------


def test_the_collector_is_created_once_and_never_supervised():
    """Aucun superviseur ni boucle de redémarrage ne relance la tâche terminée."""
    import ast
    from pathlib import Path

    import app.main as main_module

    main_source = Path(main_module.__file__).read_text()
    tree = ast.parse(main_source)

    creations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "safe_create_task"
        and any(
            isinstance(arg, ast.Call) and getattr(arg.func, "id", None) == "run_leboncoin_poller"
            for arg in node.args
        )
    ]
    assert len(creations) == 1, "une seule création de la tâche Leboncoin"

    referenced = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "leboncoin_task"
    ]
    assert referenced, "la tâche est suivie par la référence locale"
    for node in referenced:
        assert isinstance(node.ctx, (ast.Load, ast.Store))

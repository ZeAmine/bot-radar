"""Tests d'acceptation de l'arrêt Leboncoin sur blocage durable (A3)."""

import asyncio
import contextlib
import json

import pytest

from app.core.config import settings
from app.services.leboncoin_parsing import LeboncoinParseError, ScrapedLeboncoinItem
from app.services.leboncoin_scraper import LeboncoinBlockError, LeboncoinCollectionError
from app.tasks import leboncoin_poller
from app.tasks.leboncoin_poller import (
    BlockEpisode,
    BlockEpisodeError,
    IngestOutcome,
    _bounded_wait,
    _parse_block_episode,
)


def _item(external_id: str) -> ScrapedLeboncoinItem:
    from datetime import UTC, datetime

    return ScrapedLeboncoinItem(
        external_id=external_id,
        title=f"Blacksad — Intégrale {external_id}",
        url=f"https://www.leboncoin.fr/ad/livres/{external_id}",
        item_price_cents=2500,
        condition=None,
        photo_url=None,
        published_at=datetime.now(UTC),
    )


class _StopLoop(BaseException):
    """Fin de scénario simulée : BaseException pour ne pas être avalée par la boucle."""


class _RedisDouble:
    """Double Redis minimal : état mémoire, pannes pilotables."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.writes: list[tuple[str, str, int | None]] = []
        self.deletes: list[str] = []
        self.events: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        if self.read_error is not None:
            raise self.read_error
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append((key, value, ex))
        self.events.append(("write", value))
        self.store[key] = value

    async def delete(self, key: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deletes.append(key)
        self.events.append(("delete", key))
        self.store.pop(key, None)


def _install_redis(monkeypatch: pytest.MonkeyPatch, double: _RedisDouble) -> None:
    monkeypatch.setattr(leboncoin_poller, "get_redis_client", lambda: double)


# --- seuil et plafonnement des attentes ------------------------------------


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
    monkeypatch.setattr(leboncoin_poller.time, "time", lambda: clock["now"])
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
    monkeypatch.setattr(leboncoin_poller.time, "time", lambda: clock["now"])
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


class LoopHarness:
    """Exécute run_leboncoin_poller avec horloge simulée et réseau doublé."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, behaviours: list[str]) -> None:
        self.monkeypatch = monkeypatch
        self.behaviours = behaviours
        self.clock = {"now": 1_000_000.0}
        self.clock_start = self.clock["now"]
        self.tasks: list[asyncio.Task[object]] = []
        self.waits: list[float] = []
        self.requests: list[float] = []
        self.statuses: list[dict[str, object]] = []
        self.double = _RedisDouble()
        self.sent: list[object] = []
        self.send_error: BaseException | None = None
        self._install()

    def _install(self) -> None:
        monkeypatch = self.monkeypatch
        self.double.store[leboncoin_poller.LEBONCOIN_POLLER_STATUS_KEY] = (
            '{"status": "active", "last_poll_at": 999000.0}'
        )
        _install_redis(monkeypatch, self.double)

        async def fake_fetch(*_args: object, **_kwargs: object):
            self.requests.append(self.clock["now"])
            self.double.events.append(
                ("attempt", self.double.store.get(leboncoin_poller.LEBONCOIN_BLOCK_EPISODE_KEY, ""))
            )
            behaviour = self.behaviours.pop(0) if self.behaviours else "stop"
            if behaviour == "stop":
                raise _StopLoop
            if behaviour == "block":
                raise LeboncoinBlockError("HTTP 403")
            if behaviour == "network":
                raise LeboncoinCollectionError("tunnel mort")
            if behaviour == "parse":
                raise LeboncoinParseError("__NEXT_DATA__ absent")
            if behaviour == "generic":
                raise RuntimeError("inattendu")
            return [_item("vu")]

        async def fake_ingest(_item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
            return IngestOutcome(notified_count=0, needs_retry=False)

        def scheduled(coro, **kwargs: object) -> object:
            task = asyncio.create_task(coro)
            task.set_name(str(kwargs.get("name") or ""))
            self.tasks.append(task)
            return task

        async def fake_wait(_stop_event: asyncio.Event, timeout: float) -> None:
            self.waits.append(timeout)
            self.clock["now"] += max(timeout, 1.0)

        async def fake_status() -> dict[str, object]:
            return {"status": "active", "last_poll_at": self.clock["now"]}

        async def fake_save(payload: dict[str, object]) -> None:
            self.statuses.append(payload)

        async def fake_seen() -> set[str]:
            return set()

        async def fake_empty() -> bool:
            return True

        monkeypatch.setattr(settings, "LEBONCOIN_POLLER_ENABLED", True)
        monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
        monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)
        monkeypatch.setattr(leboncoin_poller, "get_poller_status", fake_status)
        monkeypatch.setattr(leboncoin_poller, "save_poller_status", fake_save)
        monkeypatch.setattr(leboncoin_poller, "load_initial_seen_ids", fake_seen)
        monkeypatch.setattr(leboncoin_poller, "_source_is_empty", fake_empty)

        async def fake_notify_admins(text: str, **_kwargs: object) -> bool:
            self.sent.append(text)
            if self.send_error is not None:
                raise self.send_error
            return True

        monkeypatch.setattr(leboncoin_poller, "notify_admins", fake_notify_admins)
        monkeypatch.setattr(leboncoin_poller, "safe_create_task", scheduled)
        monkeypatch.setattr(leboncoin_poller, "_wait_until_stop_or_delay", fake_wait)
        monkeypatch.setattr(leboncoin_poller, "reset_session", lambda: None)
        monkeypatch.setattr(leboncoin_poller, "_now", lambda: self.clock["now"])
        monkeypatch.setattr(leboncoin_poller, "_monotonic", lambda: self.clock["now"])
        monkeypatch.setattr(leboncoin_poller.random, "uniform", lambda _a, _b: 1.0)

    async def run(self) -> None:
        """Le collecteur sort par arrêt durable ou par la sentinelle de fin de scénario."""
        stop_event = asyncio.Event()
        with contextlib.suppress(_StopLoop):
            await leboncoin_poller.run_leboncoin_poller(stop_event)
        await self.drain()

        assert not stop_event.is_set(), "le collecteur ne doit jamais poser l'event partagé"

    async def drain(self) -> None:
        """Attend la fin réelle des tâches de notification, exceptions avalées comme en prod."""
        await asyncio.gather(*self.tasks, return_exceptions=True)

    @property
    def alerts(self) -> list[str]:
        return [text for text in self.sent if isinstance(text, str)]

    def stop_alerts(self) -> list[str]:
        return [text for text in self.alerts if "arrêtée" in text]


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

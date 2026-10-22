"""Doubles partagés par les tests du collecteur Leboncoin (horloge simulée)."""

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest

from app.core.config import settings
from app.services.leboncoin_parsing import LeboncoinParseError, ScrapedLeboncoinItem
from app.services.leboncoin_scraper import LeboncoinBlockError, LeboncoinCollectionError
from app.tasks import leboncoin_poller
from app.tasks.leboncoin_poller import IngestOutcome


def _item(external_id: str) -> ScrapedLeboncoinItem:

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
            self.double.events.append(("alert", text))
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

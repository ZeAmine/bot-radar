"""Régulation des requêtes Chasse-aux-livres : concurrence, espacement, coupe-circuit.

Le budget est exprimé en requêtes HTTP réellement émises, sondages et reprises
compris. Une restriction ferme la porte aux appels suivants, y compris ceux qui
attendent déjà un créneau. L'espacement et le quota de fenêtre font attendre ;
seule une restriction ou un budget de collecte épuisé font refuser.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from app.core.redis import get_redis_client

logger = logging.getLogger("radar.cal_governor")

CAL_MAX_CONCURRENT = 1
CAL_MIN_INTERVAL_SECONDS = 2.0
CAL_MAX_WAIT_SECONDS = 5.0
CAL_MAX_REQUESTS_PER_COLLECTION = 12
CAL_WINDOW_SECONDS = 60.0
CAL_MAX_REQUESTS_PER_WINDOW = 30
CAL_CHALLENGE_REST_SECONDS = 6 * 3600.0
CAL_429_DEFAULT_REST_SECONDS = 900.0
GOVERNOR_KEY = "radar:cal:governor"


class CalSuspendedError(Exception):
    """Le régulateur refuse l'appel : ce n'est pas un échec réseau."""

    def __init__(self, reason: str, retry_at: datetime | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_at = retry_at


@dataclass
class GovernorState:
    open_until: float = 0.0
    reason: str | None = None
    window: list[float] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {"open_until": self.open_until, "reason": self.reason}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GovernorState":
        open_until = payload.get("open_until")
        reason = payload.get("reason")
        return cls(
            open_until=float(open_until) if isinstance(open_until, (int, float)) else 0.0,
            reason=reason if isinstance(reason, str) else None,
        )


class CalGovernor:
    def __init__(
        self,
        *,
        max_concurrent: int = CAL_MAX_CONCURRENT,
        min_interval_seconds: float = CAL_MIN_INTERVAL_SECONDS,
        max_wait_seconds: float = CAL_MAX_WAIT_SECONDS,
        max_requests_per_collection: int = CAL_MAX_REQUESTS_PER_COLLECTION,
        window_seconds: float = CAL_WINDOW_SECONDS,
        max_requests_per_window: int = CAL_MAX_REQUESTS_PER_WINDOW,
        store: Any = None,
        clock: Any = time.monotonic,
        wall_clock: Any = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.max_wait_seconds = max(0.0, max_wait_seconds)
        self.max_requests_per_collection = max(1, max_requests_per_collection)
        self.window_seconds = max(1.0, window_seconds)
        self.max_requests_per_window = max(1, max_requests_per_window)
        self.store = store
        self.clock = clock
        self.wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.state = GovernorState()
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._lock = asyncio.Lock()
        self._last_emit = 0.0
        self._load_lock = asyncio.Lock()
        self._loaded = False
        self.emitted = 0
        self.denied = 0
        self.throttled = 0
        self.storage_unavailable = False

    async def _resolve_store(self) -> Any:
        if self.store is False:
            return None
        if self.store is not None:
            return self.store
        try:
            return get_redis_client()
        except Exception:
            return None

    async def _load(self) -> None:
        async with self._load_lock:
            if self._loaded:
                return
            self._loaded = True
            store = await self._resolve_store()
            if store is None:
                if self.store is not False:
                    self.storage_unavailable = True
                return
            try:
                raw = await asyncio.wait_for(store.get(GOVERNOR_KEY), timeout=2)
            except Exception as exc:
                self.storage_unavailable = True
                logger.warning("État du régulateur CAL illisible : %s", type(exc).__name__)
                return
            if not raw:
                return
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                return
            if isinstance(payload, dict):
                self.state = GovernorState.from_payload(payload)

    async def _persist(self) -> None:
        store = await self._resolve_store()
        if store is None:
            if self.store is not False:
                self.storage_unavailable = True
            return
        try:
            await asyncio.wait_for(
                store.set(GOVERNOR_KEY, json.dumps(self.state.to_payload())), timeout=2
            )
        except Exception as exc:
            self.storage_unavailable = True
            logger.warning("État du régulateur CAL non persisté : %s", type(exc).__name__)

    def is_open(self) -> bool:
        return bool(self.clock() < self.state.open_until)

    def retry_at(self) -> datetime | None:
        if not self.is_open():
            return None
        return self.wall_clock() + timedelta(
            seconds=max(0.0, self.state.open_until - self.clock())
        )

    async def suspend(self, reason: str, seconds: float) -> None:
        async with self._lock:
            until = self.clock() + max(1.0, seconds)
            if until > self.state.open_until:
                self.state.open_until = until
                self.state.reason = reason
                await self._persist()
            logger.warning(
                "Régulateur CAL ouvert", extra={"reason": reason,
                                              "seconds": round(seconds, 1)}
            )

    async def _acquire_slot(self, collection: "CalCollectionBudget") -> None:
        deadline = self.clock() + self.max_wait_seconds
        while True:
            async with self._lock:
                await self._load()
                if self.is_open():
                    self.denied += 1
                    raise CalSuspendedError(self.state.reason or "rest", self.retry_at())
                if collection.used >= self.max_requests_per_collection:
                    self.denied += 1
                    raise CalSuspendedError("collection_budget", None)
                now = self.clock()
                self.state.window = [
                    stamp for stamp in self.state.window
                    if now - stamp < self.window_seconds
                ]
                wait_for = 0.0
                if self._last_emit and collection.used == 0:
                    wait_for = max(wait_for, self.min_interval_seconds - (now - self._last_emit))
                if len(self.state.window) >= self.max_requests_per_window:
                    oldest = min(self.state.window)
                    wait_for = max(wait_for, self.window_seconds - (now - oldest))
                if wait_for <= 0:
                    self.state.window.append(now)
                    self._last_emit = now
                    collection.used += 1
                    self.emitted += 1
                    return
                if now + wait_for > deadline:
                    self.denied += 1
                    raise CalSuspendedError("spacing", None)
            self.throttled += 1
            await self.sleep(wait_for)

    async def request(
        self, client: httpx.AsyncClient, collection: "CalCollectionBudget",
        *args: Any, **kwargs: Any,
    ) -> httpx.Response:
        async with self._semaphore:
            await self._acquire_slot(collection)
            response = await client.get(*args, **kwargs)
            await self.observe(response)
            return response

    async def observe(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            delay = retry_after_seconds(response)
            await self.suspend(
                "rate_limited",
                delay if delay is not None else CAL_429_DEFAULT_REST_SECONDS,
            )
        elif challenged(response):
            await self.suspend("bot_challenge", CAL_CHALLENGE_REST_SECONDS)


def challenged(response: httpx.Response) -> bool:
    from app.providers.chasse_session import source_restriction

    return source_restriction(response) == "bot_challenge"


def retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class CalHttpClient(Protocol):
    async def get(self, *args: Any, **kwargs: Any) -> httpx.Response: ...


class CalCollectionBudget:
    """Requêtes restantes pour une collecte de livre."""

    def __init__(self, governor: CalGovernor, limit: int | None = None) -> None:
        self.governor = governor
        self.limit = limit or governor.max_requests_per_collection
        self.used = 0

    async def get(self, client: httpx.AsyncClient, *args: Any, **kwargs: Any) -> httpx.Response:
        return await self.governor.request(client, self, *args, **kwargs)


class GovernedClient:
    """Client HTTP dont chaque .get() passe par le régulateur."""

    def __init__(self, client: httpx.AsyncClient, budget: CalCollectionBudget) -> None:
        self._client = client
        self._budget = budget

    async def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return await self._budget.get(self._client, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


cal_governor = CalGovernor()


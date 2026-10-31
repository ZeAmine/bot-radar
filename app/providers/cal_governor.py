"""Régulation atomique des requêtes Chasse-aux-livres.

Toutes les décisions partagées entre processus sont prises par un script Lua
exécuté par Redis : lecture, décision et écriture forment une seule opération.
Aucune décision ne repose sur un `get` suivi d'un `set` séparé.

Le temps y est exprimé en secondes epoch, pour que deux processus partagent la
même horloge. Cinq plafonds distincts, tous des choix prudents internes et non
des seuils garantis par CAL :

- livres admis par passe planifiée ;
- requêtes HTTP d'une collecte, sondages et reprises compris ;
- concurrence CAL, acquise par jeton et non par processus ;
- espacement entre deux ouvertures de collecte ;
- quota de requêtes sur une fenêtre glissante.
"""

import asyncio
import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Protocol, cast

import httpx

from app.core.redis import get_redis_client

logger = logging.getLogger("radar.cal_governor")

CAL_MAX_CONCURRENT = 1
CAL_MIN_INTERVAL_SECONDS = 2.0
CAL_MAX_WAIT_SECONDS = 5.0
CAL_MAX_REQUESTS_PER_COLLECTION = 12
CAL_MAX_BOOKS_PER_PASS = 2
CAL_WINDOW_SECONDS = 60.0
CAL_MAX_REQUESTS_PER_WINDOW = 30
CAL_CHALLENGE_REST_SECONDS = 6 * 3600.0
CAL_429_DEFAULT_REST_SECONDS = 900.0
CAL_PROBE_REST_SECONDS = 1800.0
CAL_PROBE_TTL_SECONDS = 60.0
CAL_LEASE_TTL_SECONDS = 45.0
CAL_PASS_WINDOW_SECONDS = 900.0
GOVERNOR_KEY = "radar:cal:governor"
OUTSIDE_PASS = "hors-passe"
ADMINISTRATIVE_DEFERRAL_REASONS = frozenset({
    "pass_budget", "probe_in_flight", "storage_unavailable", "concurrency",
    "rate_limited", "bot_challenge", "probe", "readmission_required",
})
CAL_ADMINISTRATIVE_RETRY_SECONDS = 1800.0
CAL_STORAGE_RETRY_SECONDS = 300.0


def administrative_deferral(reason: str | None) -> bool:
    """Un refus du régulateur prononcé avant toute requête HTTP."""
    return reason in ADMINISTRATIVE_DEFERRAL_REASONS

CLOSED = "closed"
OPEN = "open"
PROBING = "probing"

pass_occurrence: ContextVar[str | None] = ContextVar("cal_pass_occurrence", default=None)

ADMIT_SCRIPT = """
local now = tonumber(ARGV[1])
local key = KEYS[1]
local pass = ARGV[2]
local max_books = tonumber(ARGV[3])
local probe_ttl = tonumber(ARGV[4])
local pass_window = tonumber(ARGV[5])

local open_until = tonumber(redis.call('HGET', key, 'open_until') or '0')
if now < open_until then
  return {'denied', redis.call('HGET', key, 'reason') or 'rest', tostring(open_until)}
end

local probe_until = tonumber(redis.call('HGET', key, 'probe_until') or '0')
local probe_pass = redis.call('HGET', key, 'probe_pass')
if probe_pass and now < probe_until then
  return {'denied', 'probe_in_flight', ''}
end

if probe_pass then
  redis.call('HDEL', key, 'probe_pass', 'probe_until', 'probe_token')
end

redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - pass_window)
local counts_as_book = ARGV[8] == '1'
local admitted_total = redis.call('ZCARD', KEYS[2])
if counts_as_book and admitted_total >= max_books then
  return {'denied', 'pass_budget', ''}
end

if counts_as_book then
  redis.call('ZADD', KEYS[2], now, ARGV[6])
end

if open_until > 0 then
  local token = ARGV[7]
  redis.call('HSET', key, 'reason', 'probe',
             'probe_pass', pass, 'probe_token', token,
             'probe_generation', tostring(now),
             'probe_until', tostring(now + probe_ttl))
  return {'admitted', 'probe', token}
end

local admitted_pass = redis.call('HGET', key, 'admitted_pass')
local admitted_at = tonumber(redis.call('HGET', key, 'admitted_at') or '0')
if admitted_pass ~= pass or (now - admitted_at) > pass_window then
  redis.call('HSET', key, 'admitted_pass', pass, 'admitted_at', tostring(now))
end

return {'admitted', 'pass', ''}
"""

FREE_PLACES_SCRIPT = """
local now = tonumber(ARGV[1])
local pass_window = tonumber(ARGV[2])
local max_books = tonumber(ARGV[3])

local open_until = tonumber(redis.call('HGET', KEYS[1], 'open_until') or '0')
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - pass_window)
local free = max_books - redis.call('ZCARD', KEYS[2])
if free < 0 then free = 0 end
if now < open_until then
  return {'0', redis.call('HGET', KEYS[1], 'reason') or 'rest'}
end
return {tostring(free), ''}
"""

ACQUIRE_SCRIPT = """
local now = tonumber(ARGV[1])
local token = ARGV[2]
local max_concurrent = tonumber(ARGV[3])
local min_interval = tonumber(ARGV[4])
local max_per_window = tonumber(ARGV[5])
local window = tonumber(ARGV[6])
local lease_ttl = tonumber(ARGV[7])
local probe_token = ARGV[9]
local key = KEYS[1]
local lease_key = KEYS[2]
local window_key = KEYS[3]

local open_until = tonumber(redis.call('HGET', key, 'open_until') or '0')
if now < open_until then
  return {'denied', redis.call('HGET', key, 'reason') or 'rest', tostring(open_until)}
end

local probe_until = tonumber(redis.call('HGET', key, 'probe_until') or '0')
local current_probe = redis.call('HGET', key, 'probe_token')
local in_probe = current_probe and now < probe_until
local holds_probe = probe_token ~= '' and current_probe == probe_token and in_probe
if open_until > 0 and not holds_probe then
  return {'denied', 'probe_in_flight', tostring(probe_until)}
end

redis.call('ZREMRANGEBYSCORE', lease_key, '-inf', now)
local held = redis.call('ZCARD', lease_key)
if held >= max_concurrent then
  return {'wait', 'concurrency', ''}
end

local distinct_collection = ARGV[8] == '1'
local last_emit = tonumber(redis.call('HGET', key, 'last_emit') or '0')
if distinct_collection and last_emit > 0 then
  local due = last_emit + min_interval
  if now < due then
    return {'wait', 'spacing', tostring(due)}
  end
end

redis.call('ZREMRANGEBYSCORE', window_key, '-inf', now - window)
local used = redis.call('ZCARD', window_key)
if used >= max_per_window then
  local oldest = redis.call('ZRANGE', window_key, 0, 0, 'WITHSCORES')
  local due = now
  if oldest[2] then due = tonumber(oldest[2]) + window end
  return {'wait', 'window', tostring(due)}
end

redis.call('ZADD', lease_key, now + lease_ttl, token)
redis.call('ZADD', window_key, now, token)
redis.call('HSET', key, 'last_emit', tostring(now))
return {'admitted', '', ''}
"""

RELEASE_SCRIPT = """
local token = ARGV[1]
redis.call('ZREM', KEYS[2], token)
return 1
"""

SUSPEND_SCRIPT = """
local until_at = tonumber(ARGV[1])
local reason = ARGV[2]
local key = KEYS[1]
local current = tonumber(redis.call('HGET', key, 'open_until') or '0')
if until_at <= current then
  return 0
end
redis.call('HSET', key, 'open_until', tostring(until_at), 'reason', reason)
return 1
"""

CONCLUDE_SCRIPT = """
local now = tonumber(ARGV[1])
local token = ARGV[2]
local succeeded = ARGV[3] == '1'
local reason = ARGV[4]
local challenge_rest = tonumber(ARGV[5])
local probe_rest = tonumber(ARGV[6])
local key = KEYS[1]

local probe_token = redis.call('HGET', key, 'probe_token')
if not probe_token then
  return {'ignored', 'no_probe'}
end
if probe_token ~= token then
  return {'ignored', 'foreign_probe'}
end
if not redis.call('HGET', key, 'probe_pass') then
  return {'ignored', 'no_probe'}
end

redis.call('HDEL', key, 'probe_pass', 'probe_token', 'probe_until')

if succeeded then
  local open_until = tonumber(redis.call('HGET', key, 'open_until') or '0')
  local probe_gen = tonumber(redis.call('HGET', key, 'probe_generation') or '0')
  if open_until > probe_gen then
    return {'ignored', 'newer_restriction'}
  end
  redis.call('HSET', key, 'open_until', '0', 'reason', 'closed')
  return {'closed', ''}
end

local rest = probe_rest
if reason == 'bot_challenge' or reason == 'rate_limited' then
  rest = challenge_rest
end
local proposed = now + rest
local current_until = tonumber(redis.call('HGET', key, 'open_until') or '0')
local probe_gen = tonumber(redis.call('HGET', key, 'probe_generation') or '0')
if current_until > probe_gen and current_until >= proposed then
  return {'kept', redis.call('HGET', key, 'reason') or ''}
end
redis.call('HSET', key, 'open_until', tostring(proposed), 'reason', reason)
return {'reopened', reason}
"""

SNAPSHOT_SCRIPT = """
local key = KEYS[1]
return {
  redis.call('HGET', key, 'open_until') or '0',
  redis.call('HGET', key, 'reason') or '',
  redis.call('HGET', key, 'probe_pass') or '',
  redis.call('HGET', key, 'probe_until') or '0',
  redis.call('HGET', key, 'admitted_pass') or '',
}
"""


def _decoded(value: Any) -> Any:
    """Redis renvoie des octets : la décision se lit sur des chaînes."""
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, list):
        return [_decoded(item) for item in value]
    return value


class CalSuspendedError(Exception):
    """Le régulateur refuse l'appel : ce n'est pas un échec réseau."""

    def __init__(self, reason: str, retry_at: datetime | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_at = retry_at


@dataclass
class GovernorSnapshot:
    open_until: float = 0.0
    reason: str = ""
    probe_pass: str = ""
    probe_until: float = 0.0
    admitted_pass: str = ""


@dataclass
class GovernorStats:
    emitted: int = 0
    denied: int = 0
    throttled: int = 0
    deferred: int = 0
    storage_unavailable: bool = False


class CalGovernor:
    def __init__(
        self,
        *,
        max_concurrent: int = CAL_MAX_CONCURRENT,
        min_interval_seconds: float = CAL_MIN_INTERVAL_SECONDS,
        max_wait_seconds: float = CAL_MAX_WAIT_SECONDS,
        max_requests_per_collection: int = CAL_MAX_REQUESTS_PER_COLLECTION,
        max_books_per_pass: int = CAL_MAX_BOOKS_PER_PASS,
        window_seconds: float = CAL_WINDOW_SECONDS,
        max_requests_per_window: int = CAL_MAX_REQUESTS_PER_WINDOW,
        pass_window_seconds: float = CAL_PASS_WINDOW_SECONDS,
        probe_ttl_seconds: float = CAL_PROBE_TTL_SECONDS,
        lease_ttl_seconds: float = CAL_LEASE_TTL_SECONDS,
        store: Any = None,
        clock: Any = None,
        monotonic_clock: Any = None,
        sleep: Any = asyncio.sleep,
        namespace: str = "",
    ) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.max_wait_seconds = max(0.0, max_wait_seconds)
        self.max_requests_per_collection = max(1, max_requests_per_collection)
        self.max_books_per_pass = max(1, max_books_per_pass)
        self.window_seconds = max(1.0, window_seconds)
        self.max_requests_per_window = max(1, max_requests_per_window)
        self.pass_window_seconds = max(1.0, pass_window_seconds)
        self.probe_ttl_seconds = max(1.0, probe_ttl_seconds)
        self.lease_ttl_seconds = max(1.0, lease_ttl_seconds)
        self.store = store
        self.clock = clock or time.time
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.sleep = sleep
        self.namespace = namespace
        self.emitted = 0
        self.denied = 0
        self.throttled = 0
        self.deferred = 0
        self.storage_unavailable = False
        self.storage_unavailable_since: float | None = None
        self.storage_incident = 0
        self._local = GovernorSnapshot()

    @property
    def key(self) -> str:
        return f"{GOVERNOR_KEY}:{self.namespace}" if self.namespace else GOVERNOR_KEY

    @property
    def lease_key(self) -> str:
        return self.key + ":lease"

    @property
    def admit_key(self) -> str:
        return self.key + ":admit"

    @property
    def window_key(self) -> str:
        return self.key + ":window"

    def wall_now(self) -> float:
        return float(self.clock())

    async def _redis(self) -> Any:
        if self.store is False:
            return None
        if self.store is not None:
            return self.store
        try:
            return get_redis_client()
        except Exception:
            return None

    async def _run(self, script: str, *args: Any, keys: list[str] | None = None) -> Any:
        client = await self._redis()
        if client is None:
            self._storage_failed("client_absent")
            return None
        resolved = keys or [self.key]
        try:
            raw = await asyncio.wait_for(
                client.eval(script, len(resolved), *resolved, *args), timeout=2,
            )
        except Exception as exc:
            self._storage_failed(type(exc).__name__)
            return None
        if self.storage_unavailable:
            logger.info("Stockage du régulateur CAL rétabli")
        self.storage_unavailable = False
        self.storage_unavailable_since = None
        return _decoded(raw)

    def _storage_failed(self, error_type: str) -> None:
        if not self.storage_unavailable:
            self.storage_unavailable_since = float(self.monotonic_clock())
            self.storage_incident += 1
            logger.warning("Stockage du régulateur CAL indisponible (%s)", error_type)
        self.storage_unavailable = True

    async def snapshot(self) -> GovernorSnapshot:
        raw = await self._run(SNAPSHOT_SCRIPT)
        if not isinstance(raw, list) or len(raw) < 5:
            return self._local
        self._local = GovernorSnapshot(
            open_until=float(raw[0] or 0),
            reason=str(raw[1] or ""),
            probe_pass=str(raw[2] or ""),
            probe_until=float(raw[3] or 0),
            admitted_pass=str(raw[4] or ""),
        )
        return self._local

    def phase(self) -> str:
        now = self.wall_now()
        if now < self._local.open_until:
            return OPEN
        if self._local.probe_pass and now < self._local.probe_until:
            return PROBING
        if self._local.open_until > 0:
            return OPEN
        return CLOSED

    def is_open(self) -> bool:
        return self.wall_now() < self._local.open_until

    def retry_at(self) -> datetime | None:
        if not self.is_open():
            return None
        return datetime.now(UTC) + timedelta(seconds=self._local.open_until - self.wall_now())

    async def free_places(self) -> int:
        """Places de livre encore libres dans la fenêtre de passe en cours.

        C'est la seule capacité qu'une passe a le droit de demander : le plafond
        est tenu ici, pas dans une constante recopiée ailleurs. Demander huit
        livres quand il n'y a deux places ne fait pas travailler la file, cela
        fait refuser six collectes avant leur première requête.

        Une source au repos rend zéro : rien n'est dû à un catalogue fermé. Un
        stockage muet, lui, n'invente pas une capacité nulle — il rend la capacité
        nominale, que l'admission bornera de toute façon.
        """
        raw = await self._run(
            FREE_PLACES_SCRIPT,
            str(self.wall_now()), str(self.pass_window_seconds), str(self.max_books_per_pass),
            keys=[self.key, self.admit_key],
        )
        if not isinstance(raw, list) or not raw:
            return self.max_books_per_pass
        try:
            return max(0, int(float(raw[0])))
        except (TypeError, ValueError):
            return self.max_books_per_pass

    async def suspend(self, reason: str, seconds: float) -> None:
        until = self.wall_now() + max(1.0, seconds)
        result = await self._run(SUSPEND_SCRIPT, str(until), reason)
        if result is None:
            if until > self._local.open_until:
                self._local.open_until = until
                self._local.reason = reason
            logger.warning("Régulateur CAL ouvert (local)", extra={"reason": reason})
            return
        await self.snapshot()
        logger.warning("Régulateur CAL ouvert", extra={"reason": reason})

    async def admit_book(
        self, occurrence: str | None, *, counts_as_book: bool = True,
    ) -> str | None:
        """Admet une collecte et renvoie le jeton d'essai, ou None hors essai.

        Seule une collecte de livre consomme une place du plafond ; l'entretien et
        les scripts sont bornés par la concurrence et la fenêtre de requêtes.
        """
        current_pass = occurrence or OUTSIDE_PASS
        raw = await self._run(
            ADMIT_SCRIPT,
            str(self.wall_now()), current_pass, str(self.max_books_per_pass),
            str(self.probe_ttl_seconds), str(self.pass_window_seconds),
            uuid.uuid4().hex, uuid.uuid4().hex, "1" if counts_as_book else "0",
            keys=[self.key, self.admit_key],
        )
        if not isinstance(raw, list):
            self.deferred += 1
            raise CalSuspendedError("storage_unavailable", None)
        await self.snapshot()
        if raw[0] == "admitted":
            token = raw[2] if len(raw) > 2 and raw[2] else None
            return str(token) if token else None
        self.deferred += 1
        retry = None
        if len(raw) > 2 and raw[2]:
            retry = datetime.now(UTC) + timedelta(
                seconds=max(0.0, float(raw[2]) - self.wall_now()),
            )
        raise CalSuspendedError(str(raw[1] or "rest"), retry)

    async def conclude_probe(
        self, token: str | None, *, succeeded: bool, reason: str | None = None,
    ) -> str:
        """Clôt l'essai identifié par `token` ; sans jeton, rien n'est clôturé."""
        if not token:
            return "no_probe"
        raw = await self._run(
            CONCLUDE_SCRIPT,
            str(self.wall_now()), token, "1" if succeeded else "0", reason or "",
            str(CAL_CHALLENGE_REST_SECONDS), str(CAL_PROBE_REST_SECONDS),
        )
        await self.snapshot()
        if not isinstance(raw, list):
            return "storage_unavailable"
        return str(raw[0])

    async def _acquire_slot(self, collection: "CalCollectionBudget") -> str:
        if not collection._reserve(self.max_requests_per_collection):
            self.denied += 1
            raise CalSuspendedError("collection_budget", None)
        reservation_pending = True
        try:
            deadline = self.wall_now() + self.max_wait_seconds
            token = uuid.uuid4().hex
            while True:
                if collection.used >= self.max_requests_per_collection:
                    self.denied += 1
                    raise CalSuspendedError("collection_budget", None)
                raw = await self._run(
                    ACQUIRE_SCRIPT,
                    str(self.wall_now()), token, str(self.max_concurrent),
                    str(self.min_interval_seconds), str(self.max_requests_per_window),
                    str(self.window_seconds), str(self.lease_ttl_seconds),
                    "1" if collection.opening_request else "0",
                    collection.probe_token or "",
                    keys=[self.key, self.lease_key, self.window_key],
                )
                if not isinstance(raw, list):
                    self.denied += 1
                    self.storage_unavailable = True
                    raise CalSuspendedError("storage_unavailable", None)
                if raw[0] == "admitted":
                    collection._finish_reservation(emitted=True)
                    reservation_pending = False
                    self.emitted += 1
                    return token
                if raw[0] == "denied":
                    self.denied += 1
                    raise CalSuspendedError(str(raw[1] or "rest"), None)
                wait_for = 0.1
                if len(raw) > 2 and raw[2]:
                    wait_for = max(0.05, min(1.0, float(raw[2]) - self.wall_now()))
                if self.wall_now() + wait_for > deadline:
                    self.denied += 1
                    raise CalSuspendedError(str(raw[1] or "spacing"), None)
                self.throttled += 1
                await self.sleep(wait_for)
        finally:
            if reservation_pending:
                collection._finish_reservation(emitted=False)

    async def _release(self, token: str) -> None:
        await self._run(
            RELEASE_SCRIPT, token, keys=[self.key, self.lease_key, self.window_key],
        )

    async def request(
        self, client: "CalHttpClient", collection: "CalCollectionBudget",
        *args: Any, _method: str = "get", **kwargs: Any,
    ) -> httpx.Response:
        follow_redirects = kwargs.pop(
            "follow_redirects", getattr(client, "follow_redirects", False),
        )
        max_redirects = getattr(client, "max_redirects", 20)
        kwargs["follow_redirects"] = False
        history: list[httpx.Response] = []
        while True:
            if len(history) > max_redirects:
                raise httpx.TooManyRedirects(
                    "Trop de redirections CAL", request=history[-1].request,
                )
            token = await self._acquire_slot(collection)
            try:
                response = cast(httpx.Response, await getattr(client, _method)(*args, **kwargs))
                await self.observe(response)
            finally:
                await self._release(token)
            response.history = list(history)
            if (not follow_redirects or response.next_request is None
                    or response.status_code == 429 or challenged(response)):
                return response
            history.append(response)
            args = (response.next_request,)
            kwargs = {"follow_redirects": False, "auth": None}
            _method = "send"

    async def observe(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            delay = retry_after_seconds(response)
            await self.suspend(
                "rate_limited",
                delay if delay is not None else CAL_429_DEFAULT_REST_SECONDS,
            )
        elif challenged(response):
            await self.suspend("bot_challenge", CAL_CHALLENGE_REST_SECONDS)


class CalHttpClient(Protocol):
    async def get(self, *args: Any, **kwargs: Any) -> httpx.Response: ...

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response: ...

    async def send(self, *args: Any, **kwargs: Any) -> httpx.Response: ...


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


class CalCollectionBudget:
    def __init__(self, governor: CalGovernor, limit: int | None = None) -> None:
        self.governor = governor
        self.limit = limit or governor.max_requests_per_collection
        self.used = 0
        self._reserved = 0
        self._reservation_lock = Lock()
        self.probe_token: str | None = None
        self.suspension: CalSuspendedError | None = None

    def _reserve(self, limit: int) -> bool:
        with self._reservation_lock:
            if self.used + self._reserved >= min(self.limit, limit):
                return False
            self._reserved += 1
            return True

    def _finish_reservation(self, *, emitted: bool) -> None:
        with self._reservation_lock:
            self._reserved -= 1
            if emitted:
                self.used += 1

    @property
    def opening_request(self) -> bool:
        """Vrai tant qu'aucune requête de cette collecte n'a été émise."""
        return self.used == 0

    async def get(self, client: "CalHttpClient", *args: Any, **kwargs: Any) -> httpx.Response:
        try:
            return await self.governor.request(client, self, *args, **kwargs)
        except CalSuspendedError as exc:
            self.suspension = exc
            raise

    async def post(self, client: "CalHttpClient", *args: Any, **kwargs: Any) -> httpx.Response:
        try:
            return await self.governor.request(client, self, *args, _method="post", **kwargs)
        except CalSuspendedError as exc:
            self.suspension = exc
            raise


class GovernedClient:
    def __init__(
        self, client: "CalHttpClient", budget: CalCollectionBudget, *,
        follow_redirects: bool | None = None,
    ) -> None:
        while isinstance(client, GovernedClient):
            client = client._client
        self._client: CalHttpClient = client
        self._budget = budget
        self._follow_redirects = follow_redirects

    async def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
        if self._follow_redirects is not None:
            kwargs["follow_redirects"] = self._follow_redirects
        return await self._budget.get(self._client, *args, **kwargs)

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        if self._follow_redirects is not None:
            kwargs["follow_redirects"] = self._follow_redirects
        return await self._budget.post(self._client, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class UnregulatedGovernor(CalGovernor):
    """Régulateur inerte réservé aux tests qui n'examinent pas la régulation.

    Rien n'est compté ni persisté : ces tests vérifient un autre comportement et
    ne doivent pas dépendre d'un stockage partagé. La production utilise
    toujours `cal_governor`, adossé à Redis.
    """

    def __init__(self) -> None:
        super().__init__(store=False, min_interval_seconds=0.0)

    async def admit_book(
        self,
        _occurrence: str | None,
        *,
        counts_as_book: bool = True,  # noqa: ARG002
    ) -> str | None:
        return None

    async def _acquire_slot(self, collection: "CalCollectionBudget") -> str:
        collection.used += 1
        self.emitted += 1
        return "unregulated"

    async def _release(self, _token: str) -> None:
        return None

    async def observe(self, _response: httpx.Response) -> None:
        return None

    async def suspend(self, _reason: str, _seconds: float) -> None:
        return None


cal_governor = CalGovernor()

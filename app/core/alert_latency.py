"""Horloges et diagnostics de la seule chaîne d'alerte, sans I/O ajouté."""

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from time import monotonic
from typing import Any

logger = logging.getLogger("radar.alert_latency")


@dataclass
class AlertTiming:
    ingestion_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None
    listing_id: uuid.UUID | None = None
    alert_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    book_id: uuid.UUID | None = None
    discovered_at: float | None = None
    entered_at: float | None = None
    acquired_at: float | None = None
    recipients_ready_at: float | None = None
    first_call_at: float | None = None
    first_call_seen: bool = False
    ack_at: float | None = None
    attempts: int = 0


_current: ContextVar[AlertTiming | None] = ContextVar("alert_timing", default=None)
_discovery: ContextVar[tuple[float | None, uuid.UUID | None]] = ContextVar(
    "alert_discovery", default=(None, None)
)


def clock() -> float | None:
    """Une panne de diagnostic ne doit pas changer le travail observé."""
    try:
        return monotonic()
    except Exception:
        return None


def elapsed(start: float | None, end: float | None) -> float | None:
    return (end - start) * 1000 if start is not None and end is not None and end >= start else None


def current() -> AlertTiming | None:
    return _current.get()


def discovery() -> tuple[float | None, uuid.UUID | None]:
    return _discovery.get()


@contextmanager
def discovery_context(at: float | None, collection_id: uuid.UUID | None) -> Iterator[None]:
    token = _discovery.set((at, collection_id))
    try:
        yield
    finally:
        _discovery.reset(token)


@contextmanager
def timing_context(timing: AlertTiming | None) -> Iterator[None]:
    token = _current.set(timing)
    try:
        yield
    finally:
        _current.reset(token)


def safe_log(log: logging.Logger, level: int, message: str, fields: dict[str, Any]) -> None:
    # Pas de log récursif : le handler/formatter peut être la cause de la panne.
    with suppress(Exception):
        log.log(level, message, extra=fields)


def emit(
    timing: AlertTiming,
    phase: str,
    start: float | None,
    end: float | None,
    *,
    result: str,
    **details: Any,
) -> None:
    safe_log(
        logger,
        logging.INFO,
        "Frontière de latence de la chaîne d'alerte.",
        {
            "event": "alert_latency",
            "phase": phase,
            "duration_ms": elapsed(start, end),
            "duration_unit": "ms",
            "result": result,
            **{
                name: str(value) if value is not None else None
                for name, value in (
                    ("ingestion_id", timing.ingestion_id),
                    ("collection_id", timing.collection_id),
                    ("listing_id", timing.listing_id),
                    ("alert_id", timing.alert_id),
                    ("attempt_id", timing.attempt_id),
                    ("book_id", timing.book_id),
                )
            },
            **details,
        },
    )


def observe_background[**P, R](
    kind: str,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """Observe les travaux issus d'une alerte ; aucun registre ni changement de drainage."""

    def decorate(
        function: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        @wraps(function)
        def scheduled(*args: P.args, **kwargs: P.kwargs) -> Coroutine[Any, Any, R]:
            parent = current()
            if parent is None:
                return function(*args, **kwargs)
            snapshot = replace(parent)
            scheduled_at = clock()

            async def run() -> R:
                started_at = clock()
                outcome = "returned"
                error_type = None
                emit(
                    snapshot,
                    "background_start",
                    scheduled_at,
                    started_at,
                    result="started",
                    work_kind=kind,
                )
                with timing_context(snapshot):
                    try:
                        # Coroutine créée uniquement ici : fermer une tâche jamais
                        # démarrée ne laisse pas de coroutine orpheline.
                        return await function(*args, **kwargs)
                    except BaseException as exc:
                        outcome = (
                            "cancelled" if isinstance(exc, asyncio.CancelledError) else "raised"
                        )
                        error_type = type(exc).__name__
                        raise
                    finally:
                        ended_at = clock()
                        emit(
                            snapshot,
                            "background_run",
                            started_at,
                            ended_at,
                            result=outcome,
                            work_kind=kind,
                            error_type=error_type,
                            scheduled_to_end_ms=elapsed(scheduled_at, ended_at),
                        )

            return run()

        return scheduled

    return decorate

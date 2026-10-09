"""Gestionnaire sécurisé de tâches d'arrière-plan avec prévention de Garbage Collection."""

import asyncio
import logging
from collections.abc import Coroutine
from time import monotonic
from typing import Any

logger = logging.getLogger("radar.tasks")

# Ensemble retenant les références fortes vers les tâches en cours d'exécution
_background_tasks: set[asyncio.Task[Any]] = set()
# Réveiller uniquement les drainages actifs si une nouvelle tâche est enregistrée.
_drain_events: set[asyncio.Event] = set()


def safe_create_task[T](
    coro: Coroutine[Any, Any, T],
    name: str | None = None,
) -> asyncio.Task[T]:
    """Crée une tâche asyncio d'arrière-plan protégée contre la collecte intempestive du GC.

    Attache un callback automatique qui journalise toute exception non gérée
    et retire la référence forte une fois la tâche terminée.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _on_task_done(t: asyncio.Task[Any]) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc:
                logger.error(
                    "Tâche d'arrière-plan '%s' a échoué avec une exception non gérée :",
                    t.get_name(),
                    exc_info=exc,
                )

    task.add_done_callback(_on_task_done)
    for event in _drain_events:
        event.set()
    return task


async def wait_all_background_tasks(timeout: float = 5.0) -> None:
    """Draine les générations jusqu'à une échéance, puis attend les nettoyages.

    L'annulation reste coopérative : un finally récalcitrant peut prolonger l'arrêt.
    Les producteurs doivent être arrêtés et leurs clients laissés ouverts par l'appelant.
    Une annulation de l'appelant est propagée après nettoyage, jamais convertie en succès.
    """
    deadline = monotonic() + max(0.0, timeout)
    current = asyncio.current_task()
    changed = asyncio.Event()
    cancelled: set[asyncio.Task[Any]] = set()
    cancelling = False
    caller_cancellation: asyncio.CancelledError | None = None
    logger.info("Attente de l'achèvement des tâches d'arrière-plan (échéance commune).")
    _drain_events.add(changed)
    try:
        while True:
            changed.clear()
            pending = {t for t in _background_tasks if t is not current and not t.done()}
            if not pending:
                if caller_cancellation is not None:
                    raise caller_cancellation
                return

            if cancelling:
                newly_cancelled = pending - cancelled
                for task in newly_cancelled:
                    # Ne pas interrompre un rollback qui traite déjà une annulation.
                    if not task.cancelling():
                        task.cancel()
                cancelled.update(pending)
                if newly_cancelled:
                    logger.warning(
                        "Drainage non terminé : annulation demandée, nettoyage attendu pour %s.",
                        sorted(t.get_name() for t in newly_cancelled),
                    )
                remaining = None
            else:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    cancelling = True
                    logger.warning("Échéance globale du drainage atteinte : annulation des tâches.")
                    continue

            # Attente locale, jamais détachée : elle est annulée et attendue à chaque tour.
            wakeup = asyncio.create_task(changed.wait())
            try:
                await asyncio.wait(
                    pending | {wakeup}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError as exc:
                caller_cancellation = exc
                cancelling = True
            finally:
                wakeup.cancel()
                try:
                    await asyncio.gather(wakeup, return_exceptions=True)
                except asyncio.CancelledError as exc:
                    caller_cancellation = exc
                    cancelling = True
    finally:
        _drain_events.discard(changed)

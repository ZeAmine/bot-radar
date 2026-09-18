"""Gestionnaire sécurisé de tâches d'arrière-plan avec prévention de Garbage Collection."""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger("radar.tasks")

# Ensemble retenant les références fortes vers les tâches en cours d'exécution
_background_tasks: set[asyncio.Task[Any]] = set()


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
    return task


async def wait_all_background_tasks(timeout: float = 5.0) -> None:
    """Attend la terminaison propre de toutes les tâches d'arrière-plan (pour shutdown propre)."""
    if not _background_tasks:
        return
    logger.info("Attente de l'achèvement de %d tâche(s) d'arrière-plan...", len(_background_tasks))
    tasks = list(_background_tasks)
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("Délai d'attente dépassé pour les tâches d'arrière-plan, annulation forcée.")
        for t in tasks:
            if not t.done():
                t.cancel()

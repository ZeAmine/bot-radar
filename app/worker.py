"""Worker autonome pour l'exécution des tâches planifiées d'arrière-plan (APScheduler)."""

import asyncio
import contextlib
import functools
import logging
import signal

from app.core.config import settings
from app.core.logging import setup_logging
from app.tasks.scheduler import scheduler
from app.tasks.vinted_poller import run_vinted_poller

logger = logging.getLogger("radar.worker")


async def run_worker() -> None:
    """Initialise et maintient le planificateur de tâches et le poller Vinted en continu."""
    setup_logging(settings.LOG_LEVEL)
    logger.info("Démarrage du worker autonome Radar Bot...")

    scheduler.start()
    logger.info("Planificateur APScheduler démarré avec succès.")

    stop_event = asyncio.Event()

    # Lancement du poller Vinted haute fréquence en tâche d'arrière-plan
    poller_task = asyncio.create_task(run_vinted_poller(stop_event))

    def handle_shutdown(sig: int) -> None:
        logger.info("Signal d'arrêt reçu (%s), arrêt du worker...", sig)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, functools.partial(handle_shutdown, sig))

    try:
        await stop_event.wait()
    finally:
        stop_event.set()
        logger.info("Arrêt du planificateur APScheduler...")
        scheduler.shutdown(wait=False)
        poller_task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await poller_task
        logger.info("Worker autonome Radar Bot arrêté proprement.")


def main() -> None:
    """Point d'entrée exécutable du worker."""
    try:
        asyncio.run(run_worker())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Arrêt manuel du worker.")


if __name__ == "__main__":
    main()

"""Point d'entrée principal de l'application FastAPI."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN, SchedulerEvent
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.middlewares.security import (
    PayloadSizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.routes import books, health, ingest, internal, telegram
from app.core import pilot_mode
from app.core.config import settings, validate_production_secrets
from app.core.logging import setup_logging


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Gestion du cycle de vie de l'application (démarrage et extinction)."""
    setup_logging(settings.LOG_LEVEL)
    validate_production_secrets()

    from app.bot.bot_instance import bot, setup_bot_commands
    if not pilot_mode.active():
        await setup_bot_commands(bot)

    from app.core.tasks import safe_create_task
    from app.tasks.scheduler import scheduler
    from app.tasks.vinted_poller import run_vinted_poller

    stop_event = asyncio.Event()
    poller_task = None
    scheduler.start()

    if settings.VINTED_POLLER_ENABLED and not pilot_mode.active():
        poller_task = safe_create_task(run_vinted_poller(stop_event))

    async def shutdown_services() -> None:
        stop_event.set()
        # Shutdown APScheduler différé : son exécuteur 3.x efface _pending_futures,
        # d'où la capture des producteurs avant l'arrêt.
        stopped = asyncio.Event()

        def scheduler_stopped(_event: SchedulerEvent) -> None:
            stopped.set()

        scheduler.add_listener(scheduler_stopped, EVENT_SCHEDULER_SHUTDOWN)
        try:
            scheduler.pause()
            executor = scheduler._executors.get("default")
            pending_jobs = tuple(getattr(executor, "_pending_futures", ()))
            if poller_task:
                poller_task.cancel()
            scheduler.shutdown(wait=False)
            await stopped.wait()
        finally:
            scheduler.remove_listener(scheduler_stopped)

        producers = {task for task in pending_jobs if not task.done()}
        if poller_task is not None and not poller_task.done():
            producers.add(poller_task)
        if producers:
            logging.getLogger("radar.tasks").warning(
                "Arrêt non terminé : attente du nettoyage de %d producteur(s).", len(producers)
            )
            await asyncio.wait(producers)

        from app.core.http_client import close_shared_http_client
        from app.core.redis import close_redis
        from app.core.tasks import wait_all_background_tasks

        await wait_all_background_tasks(timeout=5.0)
        await close_shared_http_client()
        await close_redis()

    try:
        yield
    finally:
        # Nettoyage local, retenu par une référence forte : jamais détaché.
        cleanup = asyncio.create_task(shutdown_services(), name="shutdown-services")
        caller_cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                if cleanup.cancelled():
                    raise
                caller_cancellation = exc
                logging.getLogger("radar.tasks").warning(
                    "Arrêt non terminé : appelant annulé, nettoyage des services encore attendu."
                )
            except Exception as exc:
                if caller_cancellation is not None:
                    raise caller_cancellation from exc
                raise
        if caller_cancellation is not None:
            raise caller_cancellation


class PilotMaintenanceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if pilot_mode.active() and not (
            request.method == "GET" and (request.url.path == "/health"
                                        or request.url.path.startswith("/api/v1/internal/"))
        ):
            return Response(status_code=503, headers={"Retry-After": "3600"})
        return await call_next(request)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Injecte un request_id unique dans chaque requête pour l'observabilité."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def create_app() -> FastAPI:
    """Fabrique de l'application FastAPI."""
    # Production : ni documentation interactive ni schéma OpenAPI exposés.
    docs_url = None if settings.is_production else "/docs"
    redoc_url = None if settings.is_production else "/redoc"
    openapi_url = None if settings.is_production else "/openapi.json"

    application = FastAPI(
        title=f"{settings.PROJECT_NAME} API",
        description="API d'ingestion et bot Telegram privé de détection des bonnes affaires.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )

    # Middlewares (ordonnés : SecurityHeadersMiddleware le plus externe)
    application.add_middleware(RequestIdMiddleware)
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(PayloadSizeLimitMiddleware)
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(PilotMaintenanceMiddleware)

    application.include_router(health.router)
    application.include_router(ingest.router)
    application.include_router(internal.router)
    application.include_router(books.router)
    application.include_router(telegram.router)

    return application


app = create_app()

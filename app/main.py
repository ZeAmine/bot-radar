"""Point d'entrée principal de l'application FastAPI."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.middlewares.security import (
    PayloadSizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.routes import health, ingest, internal, telegram
from app.core.config import settings, validate_production_secrets
from app.core.logging import setup_logging


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Gestion du cycle de vie de l'application (démarrage et extinction)."""
    # Initialisation du logging structuré
    setup_logging(settings.LOG_LEVEL)

    # Configuration des commandes du menu Telegram
    from app.bot.bot_instance import bot, setup_bot_commands
    await setup_bot_commands(bot)

    # Démarrage du planificateur APScheduler et du poller Vinted unifiés
    from app.core.tasks import safe_create_task
    from app.tasks.scheduler import scheduler
    from app.tasks.vinted_poller import run_vinted_poller

    validate_production_secrets()

    stop_event = asyncio.Event()
    poller_task = None
    scheduler.start()

    if settings.VINTED_POLLER_ENABLED:
        poller_task = safe_create_task(run_vinted_poller(stop_event))

    yield

    # Nettoyage à la fermeture
    stop_event.set()
    if poller_task:
        poller_task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await poller_task

    with contextlib.suppress(Exception):
        scheduler.shutdown(wait=False)

    from app.core.http_client import close_shared_http_client
    from app.core.redis import close_redis
    from app.core.tasks import wait_all_background_tasks

    await wait_all_background_tasks(timeout=5.0)
    await close_shared_http_client()
    await close_redis()


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
    # Désactivation de la documentation interactive et du schéma OpenAPI en production
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

    # Inclusion des routes
    application.include_router(health.router)
    application.include_router(ingest.router)
    application.include_router(internal.router)
    application.include_router(telegram.router)

    return application


app = create_app()

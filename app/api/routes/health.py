"""Route de vérification d'état (Healthcheck)."""

from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Endpoint public de santé du service."""
    return {
        "status": "ok",
        "environment": settings.APP_ENV,
    }

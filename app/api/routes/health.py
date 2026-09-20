from typing import Any

from fastapi import APIRouter

from app.core.config import settings
from app.services.ai.client import deepseek_client

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Endpoint public de santé du service."""
    ai_status = await deepseek_client.check_health()
    return {
        "status": "ok",
        "environment": settings.APP_ENV,
        "ai": ai_status,
    }

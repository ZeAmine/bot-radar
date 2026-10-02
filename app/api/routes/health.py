from typing import Any

from fastapi import APIRouter

from app.core.config import settings
from app.providers.llm.registry import llm_client

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Endpoint public de santé du service."""
    ai_status = (await llm_client.check_health()).as_dict()
    return {
        "status": "ok",
        "environment": settings.APP_ENV,
        "ai": ai_status,
    }

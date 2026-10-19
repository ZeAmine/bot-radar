from typing import Any

from fastapi import APIRouter

from app.core.config import settings
from app.providers.llm.registry import llm_client

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Vivacité HTTP et sonde IA existante ; ne valide pas la capacité métier du bot."""
    ai_status = (await llm_client.check_health()).as_dict()
    return {
        "status": "ok",
        "scope": "http_liveness",
        "unchecked": ["database", "redis", "scheduler", "poller"],
        "environment": settings.APP_ENV,
        "ai": ai_status,
    }

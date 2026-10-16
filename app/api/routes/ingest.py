"""Route d'ingestion sécurisée d'annonces JSON."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_ingest_token
from app.schemas.listing import ListingIngestPayload, ListingIngestResponse
from app.services.telegram_service import send_listing_alert
from app.use_cases.ingest_listing import ProcessListingUseCase
from app.use_cases.listing_ingestion_gate import IngestionBusyError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ingest", tags=["Ingestion"])

__all__ = ["ingest_listing", "router"]


@router.post(
    "/listings",
    response_model=ListingIngestResponse,
    dependencies=[Depends(require_ingest_token)],
)
async def ingest_listing(
    payload: ListingIngestPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ListingIngestResponse:
    """Ingère une nouvelle annonce, calcule le total, matche les alertes et notifie."""
    raw_payload: dict[str, Any] = await request.json()
    use_case = ProcessListingUseCase(db, sender=send_listing_alert)
    try:
        return await use_case.execute(payload, raw_payload=raw_payload)
    except IngestionBusyError as exc:
        logger.warning(
            "Admission d'ingestion refusée temporairement : %s",
            exc.reason,
            extra={"event": "ingestion_busy", "reason": exc.reason},
        )
        raise HTTPException(
            status_code=503, detail=exc.message, headers={"Retry-After": "1"}
        ) from exc

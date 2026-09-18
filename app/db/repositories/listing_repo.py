"""Dépôt d'accès aux données pour les annonces et les matches."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.schemas.listing import ListingIngestPayload
from app.services.matcher_service import normalize_text
from app.services.price_calculator import calculate_estimated_total


class ListingRepository:
    """Gestion des opérations en base de données pour les annonces et correspondances."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_source_and_external_id(
        self,
        source_name: str,
        external_id: str,
    ) -> Listing | None:
        """Recherche une annonce existante par sa source et son identifiant externe."""
        stmt = select(Listing).where(
            Listing.source_name == source_name,
            Listing.external_id == external_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, listing_id: uuid.UUID) -> Listing | None:
        """Récupère une annonce par son UUID."""
        stmt = select(Listing).where(Listing.id == listing_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_from_payload(
        self,
        payload: ListingIngestPayload,
        raw_dict: dict[str, Any],
    ) -> Listing:
        """Crée et persiste une nouvelle annonce à partir du payload validé."""
        estimated_total = calculate_estimated_total(
            item_price_cents=payload.item_price_cents,
            estimated_protection_cents=payload.estimated_protection_cents,
            estimated_shipping_cents=payload.estimated_shipping_cents,
            fallback_defaults=True,
        )

        listing = Listing(
            source_name=payload.source_name,
            external_id=payload.external_id,
            url=payload.url,
            title=payload.title,
            normalized_title=normalize_text(payload.title),
            description=payload.description,
            category_slug=payload.category_slug,
            item_price_cents=payload.item_price_cents,
            estimated_protection_cents=payload.estimated_protection_cents,
            estimated_shipping_cents=payload.estimated_shipping_cents,
            estimated_total_cents=estimated_total,
            condition=payload.condition,
            seller_name=payload.seller_name,
            seller_rating=payload.seller_rating,
            photos=payload.photos,
            isbn13=payload.isbn13,
            published_at=payload.published_at,
            raw_payload=raw_dict,
        )
        self.session.add(listing)
        await self.session.flush()
        return listing

    async def get_active_alerts_by_category(
        self,
        category_slug: str,
    ) -> list[Alert]:
        """Récupère toutes les alertes actives pour une catégorie donnée."""
        stmt = (
            select(Alert)
            .join(Category, Alert.category_id == Category.id)
            .options(selectinload(Alert.category), selectinload(Alert.user))
            .where(
                Category.slug == category_slug,
                Alert.status == AlertStatus.ACTIVE,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def is_already_matched(
        self,
        alert_id: uuid.UUID,
        listing_id: uuid.UUID,
    ) -> bool:
        """Vérifie si une annonce a déjà été associée/notifiée à cette alerte."""
        stmt = select(ListingMatch).where(
            ListingMatch.alert_id == alert_id,
            ListingMatch.listing_id == listing_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def create_match(
        self,
        alert_id: uuid.UUID,
        listing_id: uuid.UUID,
        keyword_match_score: int = 1,
        condition_matches: bool = True,
        price_matches: bool = True,
        notified: bool = False,
    ) -> ListingMatch:
        """Enregistre une correspondance alerte-annonce."""
        now = datetime.now(UTC)
        match = ListingMatch(
            alert_id=alert_id,
            listing_id=listing_id,
            keyword_match_score=keyword_match_score,
            condition_matches=condition_matches,
            price_matches=price_matches,
            notified_at=now if notified else None,
        )
        self.session.add(match)
        await self.session.flush()
        return match

    async def get_alert_for_listing(self, listing_id: uuid.UUID) -> Alert | None:
        """Récupère l'alerte associée à cette annonce via la table listing_matches."""
        stmt = (
            select(Alert)
            .join(ListingMatch, Alert.id == ListingMatch.alert_id)
            .where(ListingMatch.listing_id == listing_id)
            .order_by(ListingMatch.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

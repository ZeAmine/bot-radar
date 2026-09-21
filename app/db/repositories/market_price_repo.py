"""Dépôt d'accès aux données pour les snapshots et le cache de prix de marché."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.providers.base import MarketOffer


class MarketPriceRepository:
    """Gestion des opérations de stockage des snapshots et du cache de marché."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save_snapshot(
        self,
        book_id: uuid.UUID,
        offer: MarketOffer,
    ) -> MarketPriceSnapshot:
        """Enregistre un snapshot d'offre de marché."""
        snapshot = MarketPriceSnapshot(
            book_id=book_id,
            provider_name=offer.provider_name,
            item_price_cents=offer.item_price_cents,
            shipping_price_cents=offer.shipping_price_cents,
            delivered_price_cents=offer.delivered_price_cents,
            currency=offer.currency,
            item_condition=offer.item_condition,
            source_url=offer.source_url,
            checked_at=datetime.now(UTC),
            is_comparable=offer.is_comparable,
            confidence=offer.confidence,
            raw_payload=offer.raw_payload,
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def get_cache_by_book_id(self, book_id: uuid.UUID) -> BookMarketCache | None:
        """Récupère le cache de prix consolidé pour un livre."""
        stmt = select(BookMarketCache).where(BookMarketCache.book_id == book_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_historical_low_1y(self, book_id: uuid.UUID) -> int | None:
        """Calcule le prix le plus bas constaté sur les 365 derniers jours
        à partir des données certifiées stockées dans les snapshots (raw_payload).
        """
        one_year_ago = datetime.now(UTC) - timedelta(days=365)
        stmt = select(MarketPriceSnapshot.raw_payload).where(
            MarketPriceSnapshot.book_id == book_id,
            MarketPriceSnapshot.checked_at >= one_year_ago,
            MarketPriceSnapshot.is_comparable.is_(True),
        )
        result = await self.session.execute(stmt)
        payloads = result.scalars().all()
        hist_lows = [
            p.get("historical_low_1y_cents")
            for p in payloads
            if p and isinstance(p, dict) and p.get("historical_low_1y_cents") is not None
        ]
        return min(hist_lows) if hist_lows else None

    async def update_or_create_cache(
        self,
        book_id: uuid.UUID,
        lowest_delivered_price_cents: int | None,
        best_provider_name: str | None,
        best_source_url: str | None,
        confidence: MatchConfidence | None,
        comparison_status: ComparisonStatus,
        cache_duration_hours: int = 24,
        historical_low_1y_cents: int | None = None,
    ) -> BookMarketCache:
        """Crée ou met à jour le cache consolidé du prix d'occasion d'un livre."""
        now = datetime.now(UTC)
        expires_at = (
            now + timedelta(hours=cache_duration_hours)
            if comparison_status == ComparisonStatus.AVAILABLE
            else None
        )

        cache = await self.get_cache_by_book_id(book_id)

        # Calcul ou conservation du plus bas historique certifié sur 1 an
        computed_hist_low: int | None = historical_low_1y_cents
        if computed_hist_low is None:
            # Si le fournisseur courant n'a pas renvoyé d'historique, préserver le cache ou chercher dans les snapshots certifiés
            if cache and cache.historical_low_1y_cents is not None:
                computed_hist_low = cache.historical_low_1y_cents
            else:
                computed_hist_low = await self.get_historical_low_1y(book_id)
        elif cache and cache.historical_low_1y_cents is not None:
            # Si le fournisseur et le cache ont tous deux un historique certifié, conserver le plus bas
            computed_hist_low = min(cache.historical_low_1y_cents, computed_hist_low)

        # Ne JAMAIS inventer un historique bas à partir du prix du jour (lowest_delivered_price_cents)

        if cache:
            cache.lowest_delivered_price_cents = lowest_delivered_price_cents
            cache.best_provider_name = best_provider_name
            cache.best_source_url = best_source_url
            cache.confidence = confidence
            cache.checked_at = now
            cache.expires_at = expires_at
            cache.comparison_status = comparison_status
            cache.historical_low_1y_cents = computed_hist_low
            cache.historical_low_at = now if computed_hist_low is not None else None
            await self.session.flush()
            return cache

        cache = BookMarketCache(
            book_id=book_id,
            lowest_delivered_price_cents=lowest_delivered_price_cents,
            best_provider_name=best_provider_name,
            best_source_url=best_source_url,
            confidence=confidence,
            checked_at=now,
            expires_at=expires_at,
            comparison_status=comparison_status,
            historical_low_1y_cents=computed_hist_low,
            historical_low_at=now if computed_hist_low is not None else None,
        )
        self.session.add(cache)
        await self.session.flush()
        return cache

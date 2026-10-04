"""Dépôt d'accès aux données pour les snapshots et le cache de prix de marché."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AlertStatus, CategorySlug, ComparisonStatus, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.providers.base import MarketOffer

# Présence de la cote certifiée dans le `raw_payload` d'un relevé : c'est cette clé
# qui distingue une observation d'un simple prix du jour. L'opérateur `->>` est
# nommé explicitement car la colonne est déclarée en `JSON` générique — `.astext`
# n'existe que sur le comparateur JSONB, et échouerait dès l'import.
_CERTIFIED_LOW_PRESENT = MarketPriceSnapshot.raw_payload.op("->>")("historical_low_1y_cents")


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

    async def get_latest_snapshot(self, book_id: uuid.UUID) -> MarketPriceSnapshot | None:
        """Récupère le snapshot le plus récent enregistré pour un livre."""
        stmt = (
            select(MarketPriceSnapshot)
            .where(MarketPriceSnapshot.book_id == book_id)
            .order_by(MarketPriceSnapshot.checked_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

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
        payloads: list[Any] = list(result.scalars().all())
        hist_lows: list[int] = [
            int(p["historical_low_1y_cents"])
            for p in payloads
            if isinstance(p, dict) and p.get("historical_low_1y_cents") is not None
        ]
        return min(hist_lows) if hist_lows else None

    async def get_observed_low_cents(self, book_id: uuid.UUID) -> int | None:
        """Plus bas prix livré que nos propres relevés attestent pour un livre.

        Les relevés comparables conservés (90 jours, cf. `cleanup`) font autorité
        sous une cotation d'agrégateur : une cote supérieure à un prix livré que
        nous avons constaté pour ce même livre est une cote fausse, pas une cote
        haute. L'offre vide émise quand aucune occasion n'est en stock
        (`delivered_price_cents = 0`, « Occasion indisponible ») n'est pas un prix
        et n'entre donc pas dans le minimum.
        """
        stmt = select(func.min(MarketPriceSnapshot.delivered_price_cents)).where(
            MarketPriceSnapshot.book_id == book_id,
            MarketPriceSnapshot.is_comparable.is_(True),
            MarketPriceSnapshot.delivered_price_cents > 0,
        )
        result = await self.session.execute(stmt)
        observed: int | None = result.scalar_one_or_none()
        return observed if observed and observed > 0 else None

    async def clamp_historical_low_by_observations(
        self,
        book_id: uuid.UUID,
        low_cents: int,
    ) -> int:
        """Abaisse une cote sous le plus bas prix livré que nos relevés attestent.

        Cette méthode ne crée jamais de référence : sans cote préexistante à
        corriger, la référence reste le prix du marché du jour. Un plancher
        appliqué dans ce sens ferait basculer des dizaines de livres en « prix
        bas constaté » pour leur seul prix du jour.
        """
        observed = await self.get_observed_low_cents(book_id)
        if observed is None:
            return low_cents
        return min(low_cents, observed)

    async def find_stale_certified_lows(
        self,
        *,
        stale_after: datetime,
        limit: int = 8,
    ) -> list[tuple[str, str | None, int, datetime | None]]:
        """Livres dont la cote 1 an affichée n'est plus nourrie par aucun relevé.

        Une cote affichée sans nouvelle observation reste un chiffre, et le
        `historical_low_at` est réécrit à chaque passe : rien ne dit qu'elle a vieilli.
        Cette requête regarde ce qui a réellement été constaté, et non ce qui est
        affiché. Ne remontent que les livres d'une alerte BD active — une cote Nobody
        ne regarde n'a pas besoin d'alerte.

        Returns:
            Liste de (titre, ISBN, cote en centimes, dernière observation certifiée).
            `None` comme dernière observation signifie « jamais observé dans les
            relevés conservés », ce qui est plus grave, pas plus rassurant.
        """
        stmt = (
            select(
                Book.title,
                Book.isbn13,
                BookMarketCache.historical_low_1y_cents,
                func.max(
                    MarketPriceSnapshot.checked_at
                ).filter(_CERTIFIED_LOW_PRESENT.is_not(None)),
            )
            .join(BookMarketCache, BookMarketCache.book_id == Book.id)
            .join(AlertBook, AlertBook.book_id == Book.id)
            .join(Alert, Alert.id == AlertBook.alert_id)
            .join(Category, Category.id == Alert.category_id)
            .outerjoin(MarketPriceSnapshot, MarketPriceSnapshot.book_id == Book.id)
            .where(
                Category.slug == CategorySlug.BD_COMICS.value,
                Alert.status == AlertStatus.ACTIVE,
                BookMarketCache.historical_low_1y_cents.is_not(None),
            )
            .group_by(Book.id, Book.title, Book.isbn13, BookMarketCache.historical_low_1y_cents)
            .having(
                func.max(MarketPriceSnapshot.checked_at).filter(
                    _CERTIFIED_LOW_PRESENT.is_not(None)
                )
                < stale_after,
                # Un livre jamais observé est plus inquiétant qu'un livre ancien.
                func.max(MarketPriceSnapshot.checked_at)
                .filter(_CERTIFIED_LOW_PRESENT.is_not(None))
                .is_(None),
            )
            .order_by(Book.title)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        # Titre et ISBN viennent du modèle ; l'agrégat filtré n'a pas de type :
        # SQLAlchemy ne le propage pas au Tuple.
        found: list[tuple[str, str | None, int, datetime | None]] = []
        for title, isbn, low, last_seen in result.all():
            # `historical_low_1y_cents` est filtré non nul par le WHERE ci-dessus.
            found.append(
                (title, isbn, cast(int, low), cast("datetime | None", last_seen))
            )
        return found

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
            # Si le fournisseur courant n'a pas renvoyé d'historique, préserver le cache
            # ou chercher dans les snapshots certifiés
            if cache and cache.historical_low_1y_cents is not None:
                computed_hist_low = cache.historical_low_1y_cents
            else:
                computed_hist_low = await self.get_historical_low_1y(book_id)
        elif cache and cache.historical_low_1y_cents is not None:
            # Si le fournisseur et le cache ont tous deux un historique certifié,
            # conserver le plus bas
            computed_hist_low = min(cache.historical_low_1y_cents, computed_hist_low)

        # Plancher sur nos relevés : une cote 1 an plus haute qu'un prix livré
        # que nous avons vu pour ce livre est une cote fausse.
        if computed_hist_low is not None:
            computed_hist_low = await self.clamp_historical_low_by_observations(
                book_id, computed_hist_low
            )

        # Ne JAMAIS inventer un historique bas à partir du prix du jour
        # (lowest_delivered_price_cents)

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

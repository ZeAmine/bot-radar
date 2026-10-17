"""Dépôt d'accès aux données pour les snapshots et le cache de prix de marché."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.constants import AlertStatus, CategorySlug, ComparisonStatus, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.domain.rules.market_offer import is_comparable_secondhand_offer, is_merchant_excluded
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
        if offer.observation_id is None:
            offer.observation_id = uuid.uuid4()
        previous = await self.session.get(MarketPriceSnapshot, offer.observation_id)
        if previous is not None:
            fields = ("provider_name", "item_price_cents", "shipping_price_cents",
                      "delivered_price_cents", "currency", "item_condition", "source_url",
                      "is_comparable", "is_historical")
            old_date = _utc(previous.observed_at) if previous.observed_at is not None else None
            new_date = _utc(offer.observed_at) if offer.observed_at is not None else None
            merchant = (offer.raw_payload or {}).get("best_merchant")
            new_merchant = merchant if isinstance(merchant, str) else None
            if (previous.book_id != book_id or old_date != new_date
                    or previous.observed_merchant != new_merchant
                    or any(getattr(previous, name) != getattr(offer, name) for name in fields)):
                raise ValueError("Identité d'observation réutilisée pour un autre relevé")
            return previous
        observed = _utc(offer.observed_at) if offer.observed_at is not None else None
        now = datetime.now(UTC)
        merchant = (offer.raw_payload or {}).get("best_merchant")
        snapshot = MarketPriceSnapshot(
            id=offer.observation_id,
            observed_at=observed,
            annual_eligible=(
                observed is not None and _utc(observed) <= now and _offer_eligible(offer)
            ),
            observed_merchant=merchant if isinstance(merchant, str) else None,
            book_id=book_id,
            provider_name=offer.provider_name,
            item_price_cents=offer.item_price_cents,
            shipping_price_cents=offer.shipping_price_cents,
            delivered_price_cents=offer.delivered_price_cents,
            currency=offer.currency,
            item_condition=offer.item_condition,
            source_url=offer.source_url,
            checked_at=(observed if not offer.is_historical and observed is not None
                        and observed <= now else now),
            is_comparable=offer.is_comparable,
            is_historical=offer.is_historical,
            confidence=offer.confidence,
            raw_payload=offer.raw_payload,
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def qualify_legacy_observations(
        self,
        book_ids: list[uuid.UUID] | None = None,
        *,
        persist: bool = True,
    ) -> list[uuid.UUID]:
        """Classe les anciens relevés sur leurs faits, sans modifier leurs dates."""
        stmt = select(MarketPriceSnapshot).where(MarketPriceSnapshot.annual_eligible.is_(None))
        if book_ids is not None:
            stmt = stmt.where(MarketPriceSnapshot.book_id.in_(book_ids))
        rows = (await self.session.scalars(stmt)).all()
        now = datetime.now(UTC)
        eligible_ids = []
        for row in rows:
            merchant = (row.raw_payload or {}).get("best_merchant")
            offer = MarketOffer(
                provider_name=row.provider_name,
                item_price_cents=row.item_price_cents,
                shipping_price_cents=row.shipping_price_cents,
                delivered_price_cents=row.delivered_price_cents,
                source_url=row.source_url,
                currency=row.currency,
                item_condition=row.item_condition,
                is_comparable=row.is_comparable,
                raw_payload=row.raw_payload,
                price_includes_shipping=(
                    (row.raw_payload or {}).get("price_includes_shipping") is True
                ),
            )
            eligible = _utc(row.checked_at) <= now and _offer_eligible(offer)
            if eligible:
                eligible_ids.append(row.id)
            if persist:
                row.observed_merchant = merchant if isinstance(merchant, str) else None
                row.annual_eligible = eligible
        if persist:
            await self.session.flush()
        return eligible_ids

    async def refresh_annual_references(
        self,
        book_ids: list[uuid.UUID],
        *,
        now: datetime | None = None,
        force: bool = False,
        persist: bool = True,
    ) -> None:
        """Recalcule en une requête groupée les projections échues, sans collecte."""
        if not book_ids:
            return
        now = _utc(now or datetime.now(UTC))
        caches = list(
            (
                await self.session.scalars(
                    select(BookMarketCache).where(BookMarketCache.book_id.in_(book_ids))
                )
            ).all()
        )
        due = [
            cache
            for cache in caches
            if force
            or cache.annual_computed_at is None
            or cache.annual_expires_at is None
            or _utc(cache.annual_expires_at) <= now
            or _utc(cache.annual_computed_at) > now
        ]
        if not due:
            return
        ids = [cache.book_id for cache in due]
        legacy_ids = await self.qualify_legacy_observations(ids, persist=persist)
        at = func.coalesce(MarketPriceSnapshot.observed_at, MarketPriceSnapshot.checked_at)
        ranked = (
            select(
                MarketPriceSnapshot.book_id.label("book_id"),
                MarketPriceSnapshot.id.label("snapshot_id"),
                MarketPriceSnapshot.delivered_price_cents.label("price"),
                MarketPriceSnapshot.provider_name.label("provider"),
                at.label("at"),
                func.count().over(partition_by=MarketPriceSnapshot.book_id).label("count"),
                func.min(at).over(partition_by=MarketPriceSnapshot.book_id).label("first"),
                func.max(at).over(partition_by=MarketPriceSnapshot.book_id).label("last"),
                func.row_number()
                .over(
                    partition_by=MarketPriceSnapshot.book_id,
                    order_by=[
                        MarketPriceSnapshot.delivered_price_cents.asc(),
                        at.desc(),
                        MarketPriceSnapshot.id.asc(),
                    ],
                )
                .label("rank"),
            )
            .where(
                MarketPriceSnapshot.book_id.in_(ids),
                or_(
                    MarketPriceSnapshot.annual_eligible.is_(True),
                    MarketPriceSnapshot.id.in_(legacy_ids),
                ),
                at > now - timedelta(days=365),
                at <= now,
            )
            .subquery()
        )
        rows = (await self.session.execute(select(ranked).where(ranked.c.rank == 1))).mappings()
        by_book = {row["book_id"]: row for row in rows}
        for cache in due:
            row = by_book.get(cache.book_id)
            values = {
                "annual_low_cents": row["price"] if row else None,
                "annual_low_snapshot_id": row["snapshot_id"] if row else None,
                "annual_low_provider": row["provider"] if row else None,
                "annual_low_observed_at": row["at"] if row else None,
                "annual_first_at": row["first"] if row else None,
                "annual_last_at": row["last"] if row else None,
                "annual_count": row["count"] if row else 0,
                "annual_computed_at": now,
                "annual_expires_at": (
                    _utc(row["first"]) + timedelta(days=365) if row else now + timedelta(days=1)
                ),
            }
            for name, value in values.items():
                if persist:
                    setattr(cache, name, value)
                else:
                    set_committed_value(cache, name, value)
        if persist:
            await self.session.flush()

    async def get_latest_snapshot(self, book_id: uuid.UUID) -> MarketPriceSnapshot | None:
        """Récupère le snapshot le plus récent enregistré pour un livre."""
        stmt = (
            select(MarketPriceSnapshot)
            .where(MarketPriceSnapshot.book_id == book_id,
                   MarketPriceSnapshot.is_historical.is_(False))
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

    async def get_alert_ids_linked_to_book(self, book_id: uuid.UUID) -> list[uuid.UUID]:
        """Alertes qui portent un livre venu d'être coté — pour leur total de série."""
        stmt = select(AlertBook.alert_id).where(AlertBook.book_id == book_id).distinct()
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def set_series_total(self, alert_id: uuid.UUID, total_cents: int | None) -> None:
        """Écrit le total de série persisté d'une alerte, sans commit.

        ``NULL`` dit « aucune cote exploitable dans la série », jamais « total zéro » :
        un zéro se lirait comme une série dont les tomes ne valent rien.
        """
        alert = await self.session.get(Alert, alert_id)
        if alert is None:
            return
        alert.series_total_cents = total_cents
        await self.session.flush()

    async def lock_series(self, alert_id: uuid.UUID) -> None:
        """Sérialise le calcul du total avant la relecture des caches des tomes."""
        await self.session.execute(select(Alert.id).where(Alert.id == alert_id).with_for_update())

    async def find_stale_certified_lows(
        self,
        *,
        stale_after: datetime,
        limit: int = 8,
    ) -> tuple[list[tuple[str, str | None, int, datetime | None]], int]:
        """Collecte historique absente depuis le seuil, indépendamment du minimum.

        Retourne les ouvrages surveillés (actifs ou en pause), un montant brut
        conservé pour compatibilité des rapports, la dernière collecte et le total.
        Le montant brut ne détermine jamais l'admission ni la fraîcheur.
        """
        # Dernière observation certifiée par livre, réutilisée telle quelle dans le
        # HAVING : même agrégat, pas une approximation recalculée.
        last_certified = BookMarketCache.historical_checked_at
        # Fonction de fenêtre évaluée après le HAVING et avant le LIMIT : le total
        # porte sur les livres déjà filtrés, liste tronquée comprise.
        total_matching = func.count().over().label("total_matching")

        stmt = (
            select(
                Book.title,
                Book.isbn13,
                func.coalesce(BookMarketCache.historical_low_1y_cents, 0),
                last_certified,
                total_matching,
            )
            .outerjoin(BookMarketCache, BookMarketCache.book_id == Book.id)
            .join(AlertBook, AlertBook.book_id == Book.id)
            .join(Alert, Alert.id == AlertBook.alert_id)
            .join(Category, Category.id == Alert.category_id)
            .where(
                Category.slug == CategorySlug.BD_COMICS.value,
                Alert.status.in_([AlertStatus.ACTIVE, AlertStatus.PAUSED]),
                Alert.deleted_at.is_(None),
            )
            .group_by(Book.id, Book.title, Book.isbn13, BookMarketCache.historical_low_1y_cents,
                      BookMarketCache.historical_checked_at, Book.created_at)
            # Ancien ou jamais observé, les deux cas comptent ; un `AND` les
            # rendrait exclusifs, or jamais observé est au moins aussi inquiétant.
            .having(or_(last_certified <= stale_after,
                        last_certified.is_(None) & (Book.created_at <= stale_after)))
            .order_by(Book.title)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        # Titre et ISBN viennent du modèle ; les agrégats filtrés n'ont pas de type
        # propagé au Tuple par SQLAlchemy.
        found: list[tuple[str, str | None, int, datetime | None]] = []
        total = 0
        for title, isbn, low, last_seen, matching in result.all():
            found.append((title, isbn, low, last_seen))
            # La fenêtre porte le même total sur chaque ligne : une seule suffit.
            total = matching
        return found, total

    async def collection_health_counts(self, *, stale_after: datetime) -> tuple[int, int, int]:
        watched = select(AlertBook.book_id).join(Alert).join(Category).where(
            Category.slug == "bd_comics", Alert.deleted_at.is_(None),
            Alert.status.in_([AlertStatus.ACTIVE, AlertStatus.PAUSED]),
        )
        last = BookMarketCache.last_successful_collection_at
        statement = select(
            func.count().filter(last.is_(None)),
            func.count().filter(last <= stale_after),
            func.count().filter(or_(BookMarketCache.historical_checked_at <= stale_after,
                                   BookMarketCache.historical_checked_at.is_(None))),
        ).select_from(Book).outerjoin(
            BookMarketCache, BookMarketCache.book_id == Book.id,
        ).where(Book.id.in_(watched))
        row = (await self.session.execute(statement)).one()
        return int(row[0]), int(row[1]), int(row[2])

    async def find_stale_current_collections(
        self, *, stale_after: datetime, limit: int = 8,
    ) -> tuple[list[tuple[str, str | None, int, datetime | None]], int]:
        """Offres dont aucune collecte réussie n'a renouvelé la connaissance sous sept jours."""
        last = BookMarketCache.last_successful_collection_at
        watched = select(AlertBook.book_id).join(Alert).join(Category).where(
            Category.slug == "bd_comics",
            Alert.status.in_([AlertStatus.ACTIVE, AlertStatus.PAUSED]),
            Alert.deleted_at.is_(None),
        )
        stmt = select(Book.title, Book.isbn13,
                      func.coalesce(BookMarketCache.lowest_delivered_price_cents, 0),
                      last, func.count().over()).outerjoin(
            BookMarketCache, BookMarketCache.book_id == Book.id,
        ).where(Book.id.in_(watched), or_(last <= stale_after,
                last.is_(None) & (Book.created_at <= stale_after)))
        rows = (await self.session.execute(stmt.order_by(Book.title).limit(limit))).all()
        return ([(title, isbn, low, date) for title, isbn, low, date, _ in rows],
                rows[0][4] if rows else 0)

    async def find_silent_providers(
        self,
        *,
        silent_after: datetime,
        recent_after: datetime,
        min_recent_observations: int = 3,
    ) -> list[tuple[str, datetime | None]]:
        """Fournisseurs récemment sollicités qui ne répondent plus depuis `silent_after`.

        Un fournisseur ne se signale pas quand il tombe en panne : ses relevés
        s'arrêtent et le cache continue d'afficher son dernier prix. Ce regroupement
        ne remonte que ceux qui étaient **encore actifs récemment** (au moins
        `min_recent_observations` relevés entre `recent_after` et `silent_after`)
        puis se sont tus : un fournisseur retiré du pipeline depuis longtemps
        cesse donc d'alerter de lui-même.

        Returns:
            (nom, dernier relevé) par fournisseur silencieux, du plus ancien au
            plus récent.
        """
        recent_observations = func.count().filter(
            MarketPriceSnapshot.checked_at >= recent_after,
            MarketPriceSnapshot.checked_at < silent_after,
        )
        stmt = (
            select(
                MarketPriceSnapshot.provider_name,
                func.max(MarketPriceSnapshot.checked_at).label("last_seen"),
            )
            .group_by(MarketPriceSnapshot.provider_name)
            .having(
                func.max(MarketPriceSnapshot.checked_at) < silent_after,
                recent_observations >= min_recent_observations,
            )
            .order_by(func.max(MarketPriceSnapshot.checked_at))
        )
        result = await self.session.execute(stmt)
        return [(name, cast("datetime | None", last_seen)) for name, last_seen in result.all()]

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
        observed_at: datetime | None = None,
    ) -> BookMarketCache:
        """Crée ou met à jour le cache consolidé du prix d'occasion d'un livre."""
        observed_at = _utc(observed_at) if observed_at is not None else None
        if observed_at is not None and observed_at > datetime.now(UTC):
            observed_at = None
        now = observed_at or datetime.now(UTC)
        expires_at = (
            now + timedelta(hours=cache_duration_hours)
            if comparison_status == ComparisonStatus.AVAILABLE and observed_at is not None
            else None
        )

        cache = await self.get_cache_by_book_id(book_id)

        # Conserve l'agrégat brut ; seules les observations datées servent à la référence.
        computed_hist_low = (
            cache.historical_low_1y_cents
            if cache is not None and cache.historical_low_1y_cents is not None
            else historical_low_1y_cents
        )

        if cache:
            cache.lowest_delivered_price_cents = lowest_delivered_price_cents
            cache.best_provider_name = best_provider_name
            cache.best_source_url = best_source_url
            cache.confidence = confidence
            cache.checked_at = observed_at
            cache.expires_at = expires_at
            cache.comparison_status = comparison_status
            cache.historical_low_1y_cents = computed_hist_low
            await self.session.flush()
            return cache

        cache = BookMarketCache(
            book_id=book_id,
            lowest_delivered_price_cents=lowest_delivered_price_cents,
            best_provider_name=best_provider_name,
            best_source_url=best_source_url,
            confidence=confidence,
            checked_at=observed_at,
            expires_at=expires_at,
            comparison_status=comparison_status,
            historical_low_1y_cents=computed_hist_low,
            historical_low_at=None,
        )
        self.session.add(cache)
        await self.session.flush()
        return cache


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


def _offer_eligible(offer: MarketOffer) -> bool:
    merchant = (offer.raw_payload or {}).get("best_merchant")
    return (
        offer.currency == "EUR"
        and offer.delivered_price_cents > 0
        and (offer.is_available or offer.is_historical)
        and (offer.shipping_price_cents is not None or offer.price_includes_shipping)
        and is_comparable_secondhand_offer(offer)
        and not is_merchant_excluded(merchant if isinstance(merchant, str) else None)
    )

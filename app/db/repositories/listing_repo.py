"""Dépôt d'accès aux données pour les annonces et les matches."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.constants import AlertStatus, CategorySlug
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.domain.enums import NotificationState
from app.schemas.listing import ListingIngestPayload
from app.services.price_calculator import calculate_estimated_total
from app.utils.text import normalize_text


class ListingRepository:
    """Gestion des opérations en base de données pour les annonces et correspondances."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_initial_seen_external_ids(self, source_name: str) -> list[str]:
        """Lit les annonces récentes sans notification éligible encore en attente.

        Seuls ready/deferred sans marqueur participent à la recollecte : unknown
        et sending ne prouvent pas l'absence d'envoi. Le destinataire doit rester
        autorisé, avec un identifiant Telegram non nul. L'admission relit ensuite
        échéance, âge et budget sous le gate commun.

        La fenêtre reste celle des 3 000 annonces récentes, avant exclusion des
        attentes : les annonces plus anciennes ne deviennent pas déjà vues.
        """
        recent = (
            select(Listing.id, Listing.external_id, Listing.received_at)
            .where(Listing.source_name == source_name)
            .order_by(Listing.received_at.desc())
            .limit(3000)
            .subquery()
        )
        pending = (
            select(ListingMatch.id)
            .join(Alert, Alert.id == ListingMatch.alert_id)
            .join(User, User.id == Alert.user_id)
            .where(
                ListingMatch.listing_id == recent.c.id,
                ListingMatch.notified_at.is_(None),
                ListingMatch.notification_state.in_([
                    NotificationState.READY, NotificationState.DEFERRED,
                ]),
                Alert.status == AlertStatus.ACTIVE,
                User.is_allowed.is_(True),
                User.telegram_user_id.in_(settings.TELEGRAM_ALLOWED_USER_IDS),
                User.telegram_user_id != 0,
            )
            .exists()
        )
        stmt = (
            select(recent.c.external_id)
            .where(~pending)
            .order_by(recent.c.received_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_source_and_external_id(
        self,
        source_name: str,
        external_id: str,
    ) -> Listing | None:
        """Recherche une annonce existante par sa source et son identifiant externe."""
        stmt = (
            select(Listing)
            .where(
                Listing.source_name == source_name,
                Listing.external_id == external_id,
            )
            .execution_options(populate_existing=True)
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
        load_options = [selectinload(Alert.category), selectinload(Alert.user)]
        if category_slug == CategorySlug.BD_COMICS.value:
            load_options.append(
                selectinload(Alert.alert_books)
                .selectinload(AlertBook.book)
                .selectinload(Book.market_cache)
            )
        stmt = (
            select(Alert)
            .join(Category, Alert.category_id == Category.id)
            .options(*load_options)
            .execution_options(populate_existing=True)
            .where(
                Category.slug == category_slug,
                Alert.status == AlertStatus.ACTIVE,
            )
        )
        result = await self.session.execute(stmt)
        alerts = list(result.scalars().all())
        if category_slug == CategorySlug.BD_COMICS.value:
            from app.db.repositories.market_price_repo import MarketPriceRepository

            ids = list({link.book_id for alert in alerts for link in alert.alert_books})
            await MarketPriceRepository(self.session).refresh_annual_references(ids, persist=False)
        return alerts

    async def match_notification_state(
        self,
        listing_id: uuid.UUID,
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        """Sépare les matches déjà notifiés de ceux encore en attente d'envoi."""
        stmt = select(ListingMatch.alert_id, ListingMatch.notified_at).where(
            ListingMatch.listing_id == listing_id
        )
        result = await self.session.execute(stmt)
        notified: set[uuid.UUID] = set()
        pending: set[uuid.UUID] = set()
        for alert_id, notified_at in result.all():
            if notified_at is not None:
                notified.add(alert_id)
            else:
                pending.add(alert_id)
        return notified, pending

    async def refresh_notification_context(
        self,
        listing: Listing,
        alert_ids: list[uuid.UUID],
        book_ids: list[uuid.UUID],
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        """Relit en groupe les objets expirés par le rollback, sans écriture ni réseau."""
        await self.session.refresh(listing)
        alerts = (
            (
                await self.session.execute(
                    select(Alert)
                    .where(Alert.id.in_(alert_ids))
                    .options(
                        selectinload(Alert.category),
                        selectinload(Alert.user),
                        selectinload(Alert.alert_books)
                        .selectinload(AlertBook.book)
                        .selectinload(Book.market_cache),
                    )
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        books = (
            (
                await self.session.execute(
                    select(Book)
                    .where(Book.id.in_(book_ids))
                    .options(selectinload(Book.market_cache))
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
            if book_ids
            else []
        )
        return {alert.id for alert in alerts}, {book.id for book in books}

    async def mark_notified(self, pairs: list[tuple[uuid.UUID, uuid.UUID]]) -> bool:
        """Horodate un pending sans réécrire un accusé déjà durable ; absence rend False."""
        now = datetime.now(UTC)
        complete = True
        for alert_id, listing_id in pairs:
            stmt = (
                update(ListingMatch)
                .where(
                    ListingMatch.alert_id == alert_id,
                    ListingMatch.listing_id == listing_id,
                    ListingMatch.notified_at.is_(None),
                )
                .values(notified_at=now, notification_state=NotificationState.NOTIFIED,
                        next_attempt_at=None)
                .returning(ListingMatch.id)
            )
            result = await self.session.execute(stmt)
            marked = result.scalar_one_or_none() is not None
            if not marked:
                # Colonnes scalaires, jamais de valeur ORM périmée : READ COMMITTED
                # voit le marqueur posé par la transaction concurrente.
                persisted_at = await self.session.scalar(
                    select(ListingMatch.notified_at).where(
                        ListingMatch.alert_id == alert_id,
                        ListingMatch.listing_id == listing_id,
                    )
                )
                marked = persisted_at is not None
            complete = marked and complete
        return complete

    async def create_match(
        self,
        alert_id: uuid.UUID,
        listing_id: uuid.UUID,
        keyword_match_score: int = 1,
        condition_matches: bool = True,
        price_matches: bool = True,
        notified: bool = False,
        known_unsent: bool = True,
    ) -> ListingMatch:
        """Enregistre une correspondance alerte-annonce."""
        now = datetime.now(UTC)
        values = {
            "id": uuid.uuid4(),
            "alert_id": alert_id,
            "listing_id": listing_id,
            "keyword_match_score": keyword_match_score,
            "condition_matches": condition_matches,
            "price_matches": price_matches,
            "notified_at": now if notified else None,
            "notification_state": (
                NotificationState.NOTIFIED if notified else
                NotificationState.READY if known_unsent else NotificationState.UNKNOWN
            ),
            "attempt_count": 0 if known_unsent else None,
            "notification_reason": None if known_unsent else "missing_match_evidence",
            "created_at": now,
        }
        bind = self.session.get_bind()
        dialect_insert = sqlite_insert if bind.dialect.name == "sqlite" else pg_insert
        stmt = (
            dialect_insert(ListingMatch)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["alert_id", "listing_id"])
        )
        await self.session.execute(stmt)
        await self.session.flush()
        found = await self.session.execute(
            select(ListingMatch).where(
                ListingMatch.alert_id == alert_id,
                ListingMatch.listing_id == listing_id,
            )
        )
        return found.scalar_one()

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

    async def get_listing_matched_to_user(
        self,
        listing_id: uuid.UUID,
        telegram_user_id: int,
    ) -> Listing | None:
        """Annonce réellement associée à une alerte de cet utilisateur.

        Une action d'alerte ne doit jamais pouvoir agir sur l'annonce d'une
        association absente ou d'un autre utilisateur : l'annonce n'est rendue
        que si un match la relie à une alerte dont l'utilisateur Telegram est
        celui qui agit.

        Les alertes **actives et en pause** comptent : une pause suspend les
        notifications, pas la propriété de l'annonce — l'utilisateur qui clique
        sur un ancien message doit pouvoir finaliser l'achat qu'il visait. Seules
        les alertes supprimées sont écartées.
        """
        stmt = (
            select(Listing)
            .join(ListingMatch, Listing.id == ListingMatch.listing_id)
            .join(Alert, Alert.id == ListingMatch.alert_id)
            .join(User, User.id == Alert.user_id)
            .where(
                Listing.id == listing_id,
                User.telegram_user_id == telegram_user_id,
                Alert.status != AlertStatus.DELETED,
            )
            .limit(1)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

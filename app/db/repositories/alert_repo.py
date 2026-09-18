"""Dépôt d'accès aux données pour les alertes."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import or_ as sa_or
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.category import Category


class AlertRepository:
    """Gestion des opérations en base de données pour les alertes."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: uuid.UUID,
        category_id: uuid.UUID,
        name: str,
        include_keywords: list[str],
        exclude_keywords: list[str] | None = None,
        variants: list[str] | None = None,
        max_item_price_cents: int | None = None,
        accepted_conditions: list[str] | None = None,
        vinted_search_url: str | None = None,
        isbn: str | None = None,
    ) -> Alert:
        """Crée une nouvelle alerte pour un utilisateur."""
        alert = Alert(
            user_id=user_id,
            category_id=category_id,
            name=name,
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords or [],
            variants=variants or [],
            max_item_price_cents=max_item_price_cents,
            accepted_conditions=accepted_conditions,
            vinted_search_url=vinted_search_url,
            isbn=isbn,
            status=AlertStatus.ACTIVE,
        )
        self.session.add(alert)
        await self.session.flush()
        return alert

    async def get_by_id(
        self,
        alert_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> Alert | None:
        """Récupère une alerte par son UUID, optionnellement filtrée par utilisateur."""
        stmt = select(Alert).options(selectinload(Alert.category)).where(Alert.id == alert_id)
        if user_id:
            stmt = stmt.where(Alert.user_id == user_id)

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        status: AlertStatus | None = None,
        limit: int = 10,
        offset: int = 0,
        category_slug: str | None = None,
    ) -> list[Alert]:
        """Récupère la liste paginée des alertes d'un utilisateur, optionnellement par catégorie."""
        stmt = (
            select(Alert)
            .options(selectinload(Alert.category))
            .where(Alert.user_id == user_id)
            .order_by(Alert.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if status:
            stmt = stmt.where(Alert.status == status)
        else:
            # Exclure les alertes supprimées par défaut
            stmt = stmt.where(Alert.status != AlertStatus.DELETED)

        if category_slug and category_slug != "all":
            stmt = stmt.join(Alert.category).where(Category.slug == category_slug)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_user(
        self,
        user_id: uuid.UUID,
        status: AlertStatus | None = None,
        category_slug: str | None = None,
    ) -> int:
        """Compte le nombre total d'alertes d'un utilisateur, optionnellement par catégorie."""
        stmt = select(func.count(Alert.id)).where(Alert.user_id == user_id)
        if status:
            stmt = stmt.where(Alert.status == status)
        else:
            stmt = stmt.where(Alert.status != AlertStatus.DELETED)

        if category_slug and category_slug != "all":
            stmt = stmt.join(Alert.category).where(Category.slug == category_slug)

        result = await self.session.execute(stmt)
        return result.scalar_one() or 0

    async def count_by_user_grouped_by_category(
        self,
        user_id: uuid.UUID,
        status: AlertStatus | None = None,
    ) -> dict[str, int]:
        """Retourne le décompte des alertes par slug de catégorie pour un utilisateur."""
        stmt = (
            select(Category.slug, func.count(Alert.id))
            .join(Alert, Alert.category_id == Category.id)
            .where(Alert.user_id == user_id)
        )
        if status:
            stmt = stmt.where(Alert.status == status)
        else:
            stmt = stmt.where(Alert.status != AlertStatus.DELETED)
        stmt = stmt.group_by(Category.slug)
        result = await self.session.execute(stmt)
        return dict(result.all())

    async def set_status(
        self,
        alert: Alert,
        status: AlertStatus,
    ) -> Alert:
        """Modifie le statut d'une alerte (active / paused)."""
        alert.status = status
        await self.session.flush()
        return alert

    async def soft_delete(self, alert: Alert) -> Alert:
        """Effectue un soft delete de l'alerte."""
        alert.status = AlertStatus.DELETED
        alert.deleted_at = datetime.now(UTC)
        await self.session.flush()
        return alert

    async def delete_completely(self, alert: Alert) -> dict[str, Any]:
        """Supprime définitivement une alerte et purge toutes ses données associées.

        1. Récupère les IDs des livres associés dans alert_books.
        2. Supprime l'alerte (PostgreSQL cascade supprime automatiquement
           les enregistrements dans alert_books et listing_matches).
        3. Identifie les livres devenus orphelins (qui ne sont plus liés à aucune autre alerte)
           et les supprime (PostgreSQL cascade supprime automatiquement
           leur book_market_cache et leurs market_price_snapshots).
        """
        from app.db.models.alert_book import AlertBook
        from app.db.models.book import Book

        alert_id = alert.id

        # 1. Identifier les livres liés à cette alerte
        stmt_books = select(AlertBook.book_id).where(AlertBook.alert_id == alert_id)
        linked_book_ids = list((await self.session.execute(stmt_books)).scalars().all())

        # 2. Supprimer l'alerte
        await self.session.delete(alert)
        await self.session.flush()

        # 3. Supprimer les livres orphelins
        deleted_books_count = 0
        for book_id in linked_book_ids:
            stmt_other = (
                select(func.count(AlertBook.alert_id))
                .where(AlertBook.book_id == book_id)
            )
            count = (await self.session.execute(stmt_other)).scalar_one() or 0
            if count == 0:
                book = await self.session.get(Book, book_id)
                if book:
                    await self.session.delete(book)
                    deleted_books_count += 1

        await self.session.flush()
        return {
            "alert_id": alert_id,
            "deleted_books_count": deleted_books_count,
        }

    async def purge_orphans_and_deleted(self) -> dict[str, int]:
        """Purge toutes les alertes marquées DELETED ainsi que les livres orphelins."""
        from app.db.models.book import Book

        # Trouver toutes les alertes soft-deleted
        stmt_deleted = select(Alert).where(
            sa_or(
                Alert.status == AlertStatus.DELETED,
                Alert.deleted_at.is_not(None),
            )
        )
        deleted_alerts = list((await self.session.execute(stmt_deleted)).scalars().all())
        deleted_alerts_count = len(deleted_alerts)

        for a in deleted_alerts:
            await self.delete_completely(a)

        # Vérifier aussi s'il reste des livres orphelins sans aucun lien alert_books
        stmt_orphans = select(Book).where(~Book.alert_books.any())
        orphan_books = list((await self.session.execute(stmt_orphans)).scalars().all())
        deleted_orphans_count = len(orphan_books)
        for b in orphan_books:
            await self.session.delete(b)

        await self.session.flush()
        return {
            "deleted_alerts": deleted_alerts_count,
            "deleted_orphans": deleted_orphans_count,
        }


    async def search_by_user(
        self,
        user_id: uuid.UUID,
        query: str,
        limit: int = 10,
        category_slug: str | None = None,
    ) -> list[Alert]:
        """Recherche les alertes d'un utilisateur par nom, ISBN ou mot-clé."""
        clean_q = query.strip()
        if not clean_q:
            return await self.list_by_user(user_id, limit=limit, category_slug=category_slug)

        pattern = f"%{clean_q}%"
        stmt = (
            select(Alert)
            .options(selectinload(Alert.category))
            .where(
                Alert.user_id == user_id,
                Alert.status != AlertStatus.DELETED,
                sa_or(
                    Alert.name.ilike(pattern),
                    Alert.isbn.ilike(pattern),
                ),
            )
            .order_by(Alert.created_at.desc())
            .limit(limit)
        )
        if category_slug and category_slug != "all":
            stmt = stmt.join(Alert.category).where(Category.slug == category_slug)

        result = await self.session.execute(stmt)
        matched = list(result.scalars().all())
        if matched:
            return matched

        # Repli : vérifier si le mot-clé est contenu dans include_keywords
        all_alerts = await self.list_by_user(user_id, limit=50, category_slug=category_slug)
        norm_q = clean_q.lower()
        keyword_matches = [
            a for a in all_alerts if any(norm_q in kw.lower() for kw in a.include_keywords)
        ]
        return keyword_matches[:limit]

    async def find_existing(
        self,
        user_id: uuid.UUID,
        isbn: str | None = None,
        title: str | None = None,
    ) -> Alert | None:
        """Trouve une alerte existante active ou en pause pour un ISBN ou un titre donné."""
        conditions = []
        if isbn:
            conditions.append(Alert.isbn == isbn)
        if title and len(title.strip()) >= 3:
            conditions.append(Alert.name.ilike(title.strip()))

        if conditions:
            stmt = (
                select(Alert)
                .options(selectinload(Alert.category))
                .where(
                    Alert.user_id == user_id,
                    Alert.status != AlertStatus.DELETED,
                    Alert.deleted_at.is_(None),
                    sa_or(*conditions),
                )
                .limit(1)
            )
            res = await self.session.execute(stmt)
            existing = res.scalar_one_or_none()
            if existing:
                return existing

        # Recherche via alert_books si un livre correspond à l'ISBN
        if isbn:
            from app.db.models.alert_book import AlertBook
            from app.db.models.book import Book

            stmt_book = (
                select(Alert)
                .options(selectinload(Alert.category))
                .join(AlertBook, AlertBook.alert_id == Alert.id)
                .join(Book, Book.id == AlertBook.book_id)
                .where(
                    Alert.user_id == user_id,
                    Alert.status != AlertStatus.DELETED,
                    Alert.deleted_at.is_(None),
                    sa_or(Book.isbn13 == isbn, Book.isbn10 == isbn),
                )
                .limit(1)
            )
            res_book = await self.session.execute(stmt_book)
            return res_book.scalar_one_or_none()

        return None


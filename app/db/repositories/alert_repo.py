"""Dépôt d'accès aux données pour les alertes."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import or_ as sa_or
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import AlertStatus
from app.core.exceptions import EntityNotFoundError
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.listing_match import ListingMatch
from app.domain.entities.alert_preparation import preparation_request


@dataclass(frozen=True)
class BibliographicSnapshot:
    """Contexte optimiste d'un résultat : un ISBN revenu à sa valeur ne suffit pas."""

    name: str
    isbn: str | None
    status: AlertStatus
    revision: datetime
    preparation_token: str | None

    @classmethod
    def capture(cls, alert: Alert) -> "BibliographicSnapshot":
        request = preparation_request(alert)
        return cls(alert.name, alert.isbn, alert.status, alert.updated_at,
                   request.token if request else None)

    def matches(self, alert: Alert) -> bool:
        current = self.capture(alert)
        return (
            (self.name, self.isbn, self.status, self.preparation_token)
            == (current.name, current.isbn, current.status, current.preparation_token)
            and (self.preparation_token is not None
                 or self.revision.replace(tzinfo=None) == current.revision.replace(tzinfo=None))
        )


class AlertRepository:
    """Gestion des opérations en base de données pour les alertes."""

    _ALERT_EAGER_LOAD = (selectinload(Alert.category),)

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _apply_status_filter(stmt: Any, status: AlertStatus | None) -> Any:
        """Applique le filtre de statut (exclut DELETED par défaut)."""
        if status:
            return stmt.where(Alert.status == status)
        return stmt.where(Alert.status != AlertStatus.DELETED)

    @staticmethod
    def _apply_category_filter(stmt: Any, category_slug: str | None) -> Any:
        """Applique le filtre par catégorie si différent de 'all'."""
        if category_slug and category_slug != "all":
            return stmt.join(Alert.category).where(Category.slug == category_slug)
        return stmt

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
        *, refresh: bool = False,
    ) -> Alert | None:
        """Récupère une alerte par son UUID, optionnellement filtrée par utilisateur."""
        stmt = select(Alert).options(*self._ALERT_EAGER_LOAD).where(Alert.id == alert_id)
        stmt = stmt.execution_options(populate_existing=refresh)
        if user_id:
            stmt = stmt.where(Alert.user_id == user_id)

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def lock_for_edit(self, alert: Alert, *, include_deleted: bool = False) -> bool:
        """Relit l'alerte sous verrou court, commun aux gestes et aux résultats de préparation."""
        stmt = (
            select(Alert).options(*self._ALERT_EAGER_LOAD)
            .where(Alert.id == alert.id).with_for_update()
            .execution_options(populate_existing=True)
        )
        if not include_deleted:
            stmt = stmt.where(Alert.status != AlertStatus.DELETED, Alert.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def lock_preparation(self, alert_id: uuid.UUID, token: str) -> Alert | None:
        """N'admet les écritures que sur la version encore souhaitée ; aucun HTTP sous ce verrou."""
        stmt = (
            select(Alert).where(
                Alert.id == alert_id, Alert.status == AlertStatus.PAUSED,
                Alert.deleted_at.is_(None), Alert.preparation["token"].as_string() == token,
                Alert.preparation["state"].as_string() == "pending",
                Alert.preparation["activation_requested"].as_boolean().is_(True),
            ).with_for_update().execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def invalidate_preparation(self, alert: Alert, *, author_hint: str | None) -> None:
        """Seules les dépendances bibliographiques changent la version du travail."""
        request = preparation_request(alert)
        if request is not None:
            alert.decision_version += 1
            await self.reset_bibliographic_identity(alert)
            alert.preparation = request.restart(
                activate=request.activation_requested, author_hint=author_hint
            ).stored()

    async def reset_bibliographic_identity(self, alert: Alert) -> None:
        """Détache l'ancien catalogue, jamais les livres partagés ; sans commit."""
        from app.db.repositories.book_repo import BookRepository

        await BookRepository(self.session).clear_alert_books(alert.id)
        alert.book_search_exhausted_at = None
        alert.series_total_cents = None

    async def lock_bibliographic_result(
        self, alert_id: uuid.UUID, snapshot: BibliographicSnapshot, *,
        require_preparation: bool = False,
    ) -> Alert | None:
        """Relit sous verrou court avant liaison/verdict, y compris après un aller-retour d'ISBN."""
        current = await self.get_by_id(alert_id)
        if current is None:
            return None
        # Même verrou de ligne que lock_for_edit, sans expirer user/alert_books
        # déjà chargés par l'ingestion. Les champs de garde seuls sont relus.
        locked = await self.session.scalar(select(Alert.id).where(
            Alert.id == alert_id, Alert.deleted_at.is_(None), Alert.status != AlertStatus.DELETED,
        ).with_for_update())
        if locked is None:
            return None
        await self.session.refresh(current, [
            "name", "isbn", "status", "updated_at", "preparation",
            "include_keywords", "variants", "exclude_keywords",
        ])
        request = preparation_request(current)
        if require_preparation and (
            request is None or request.state != "pending" or not request.activation_requested
            or current.status != AlertStatus.PAUSED
        ):
            return None
        return current if snapshot.matches(current) else None

    async def pending_preparation_ids(self, excluded: set[uuid.UUID]) -> list[uuid.UUID]:
        """Quatre demandes éligibles, jamais tentées puis les moins récemment tentées."""
        stmt = select(Alert.id).where(
            Alert.status == AlertStatus.PAUSED, Alert.deleted_at.is_(None),
            Alert.preparation["state"].as_string() == "pending",
            Alert.preparation["activation_requested"].as_boolean().is_(True),
        )
        if excluded:
            stmt = stmt.where(Alert.id.not_in(excluded))
        stmt = stmt.order_by(
            Alert.preparation["last_attempt_at"].as_string().asc().nulls_first(),
            Alert.created_at, Alert.id,
        ).limit(4)
        return list((await self.session.execute(stmt)).scalars())

    async def list_not_deleted(self) -> list[Alert]:
        """Alertes encore présentes, plus récentes d'abord."""
        stmt = (
            select(Alert)
            .options(*self._ALERT_EAGER_LOAD)
            .where(Alert.status != AlertStatus.DELETED)
            .order_by(Alert.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

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
            .options(*self._ALERT_EAGER_LOAD)
            .where(Alert.user_id == user_id)
            .order_by(Alert.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        stmt = self._apply_status_filter(stmt, status)
        stmt = self._apply_category_filter(stmt, category_slug)

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
        stmt = self._apply_status_filter(stmt, status)
        stmt = self._apply_category_filter(stmt, category_slug)

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
        stmt = self._apply_status_filter(stmt, status)
        stmt = stmt.group_by(Category.slug)
        result = await self.session.execute(stmt)
        return dict(result.all())

    async def set_status(
        self,
        alert: Alert,
        status: AlertStatus,
        *,
        require_editable: bool = False,
    ) -> Alert:
        """Modifie le statut ; le mode strict distingue le refus du verrou d'une mutation."""
        if not await self.lock_for_edit(alert):
            if require_editable:
                raise EntityNotFoundError("Alerte introuvable.")
            return alert
        request = preparation_request(alert)
        if request is not None and status in (AlertStatus.PAUSED, AlertStatus.ACTIVE):
            if status == AlertStatus.PAUSED:
                alert.preparation = request.suspend().stored()
            elif not request.activation_requested or request.state == "blocked":
                alert.preparation = request.restart(
                    activate=status == AlertStatus.ACTIVE, author_hint=request.author_hint,
                    preserve_series_volumes=True,
                ).stored()
            alert.status = AlertStatus.PAUSED
        else:
            alert.status = status
        alert.decision_version += 1
        await self.session.flush()
        return alert

    async def set_book_search_exhausted(
        self,
        alert_id: uuid.UUID,
        exhausted_at: datetime | None,
    ) -> None:
        """Écrit le verdict « aucune source ne catalogue cet ouvrage », sans commit.

        La date est celle du relevé qui l'établit ; ``None`` l'efface — une notice
        arrivée depuis contredit le verdict, et l'alerte ne doit pas continuer de
        l'annoncer. Ce n'est ni une règle de matching ni une cote : seulement la
        mémoire d'une recherche, écrite par l'enrichissement de fond (ADR-041).
        """
        alert = await self.session.get(Alert, alert_id)
        if alert is None:
            return
        alert.book_search_exhausted_at = exhausted_at
        await self.session.flush()

    async def soft_delete(self, alert: Alert) -> Alert:
        """Effectue un soft delete de l'alerte."""
        if not await self.lock_for_edit(alert):
            return alert
        alert.status = AlertStatus.DELETED
        alert.decision_version += 1
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
        if not await self.lock_for_edit(alert, include_deleted=True):
            return {"alert_id": alert_id, "deleted_books_count": 0}

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
            ),
            ~Alert.matches.any(ListingMatch.notified_at.is_(None)),
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
            .options(*self._ALERT_EAGER_LOAD)
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
        stmt = self._apply_category_filter(stmt, category_slug)

        result = await self.session.execute(stmt)
        matched: list[Alert] = list(result.scalars().all())
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
        if isbn and isinstance(isbn, str):
            conditions.append(Alert.isbn == isbn)
        if isinstance(title, str) and len(title.strip()) >= 3:
            conditions.append(Alert.name.ilike(title.strip()))

        if conditions:
            stmt = (
                select(Alert)
                .options(*self._ALERT_EAGER_LOAD)
                .where(
                    Alert.user_id == user_id,
                    Alert.status != AlertStatus.DELETED,
                    Alert.deleted_at.is_(None),
                    sa_or(*conditions),
                )
                .limit(1)
            )
            import inspect

            res = await self.session.execute(stmt)
            existing = res.scalar_one_or_none()
            if existing is not None and not inspect.isawaitable(existing):
                return existing

        # Recherche via alert_books si un livre correspond à l'ISBN ou au titre
        if isbn or (title and len(title.strip()) >= 3):
            import inspect

            from app.db.models.alert_book import AlertBook
            from app.db.models.book import Book
            from app.utils.text import normalize_text

            book_conditions = []
            if isbn:
                book_conditions.extend([Book.isbn13 == isbn, Book.isbn10 == isbn])
            if title and len(title.strip()) >= 3:
                norm = normalize_text(title.strip())
                book_conditions.append(Book.title.ilike(title.strip()))
                if norm:
                    book_conditions.append(Book.normalized_title == norm)

            stmt_book = (
                select(Alert)
                .options(*self._ALERT_EAGER_LOAD)
                .join(AlertBook, AlertBook.alert_id == Alert.id)
                .join(Book, Book.id == AlertBook.book_id)
                .where(
                    Alert.user_id == user_id,
                    Alert.status != AlertStatus.DELETED,
                    Alert.deleted_at.is_(None),
                    sa_or(*book_conditions),
                )
                .limit(1)
            )
            res_book = await self.session.execute(stmt_book)
            existing_book = res_book.scalar_one_or_none()
            if existing_book is not None and not inspect.isawaitable(existing_book):
                return existing_book

        return None

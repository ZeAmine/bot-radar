"""Dépôt d'accès aux données pour les catégories."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.category import Category


class CategoryRepository:
    """Gestion des opérations en base de données pour les catégories."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all_active(self) -> list[Category]:
        """Récupère toutes les catégories actives ordonnées par libellé."""
        stmt = select(Category).where(Category.is_active.is_(True)).order_by(Category.label)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_slug(self, slug: str) -> Category | None:
        """Récupère une catégorie par son slug."""
        stmt = select(Category).where(Category.slug == slug)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, category_id: uuid.UUID) -> Category | None:
        """Récupère une catégorie par son UUID."""
        stmt = select(Category).where(Category.id == category_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

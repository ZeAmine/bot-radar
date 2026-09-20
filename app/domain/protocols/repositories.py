"""Contrats d'interface pour les dépôts d'accès aux données (Repositories)."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.domain.entities.alert import AlertEntity
from app.domain.entities.listing import ListingEntity
from app.domain.enums import AlertStatus, CategorySlug


class AlertRepositoryProtocol(Protocol):
    """Abstraction de l'accès aux données des alertes."""

    async def get_by_id(
        self,
        alert_id: UUID,
        user_id: UUID | None = None,
    ) -> AlertEntity | None:
        """Récupère une alerte par son UUID."""
        ...

    async def get_active_by_category(
        self,
        category_slug: CategorySlug,
    ) -> Sequence[AlertEntity]:
        """Récupère les alertes actives d'une catégorie pour le matching en mémoire."""
        ...

    async def list_by_user(
        self,
        user_id: UUID,
        status: AlertStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[AlertEntity]:
        """Liste paginée des alertes d'un utilisateur."""
        ...


class ListingRepositoryProtocol(Protocol):
    """Abstraction de l'accès aux données des annonces et correspondances."""

    async def exists_by_external_id(
        self,
        source_name: str,
        external_id: str,
    ) -> bool:
        """Vérifie rapidement si une annonce a déjà été ingérée."""
        ...

    async def save(
        self,
        listing: ListingEntity,
        matched_alert_ids: Sequence[UUID] | None = None,
    ) -> ListingEntity:
        """Persiste une nouvelle annonce et ses éventuelles correspondances."""
        ...

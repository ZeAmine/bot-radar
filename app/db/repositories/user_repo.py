"""Dépôt d'accès aux données pour les utilisateurs Telegram."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User


class UserRepository:
    """Gestion des opérations en base de données pour les utilisateurs."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        """Récupère un utilisateur par son identifiant Telegram."""
        stmt = select(User).where(User.telegram_user_id == telegram_user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        telegram_user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        is_allowed: bool = True,
    ) -> User:
        """Récupère ou crée un utilisateur Telegram."""
        user = await self.get_by_telegram_id(telegram_user_id)
        if user:
            updated = False
            if username and user.username != username:
                user.username = username
                updated = True
            if first_name and user.first_name != first_name:
                user.first_name = first_name
                updated = True
            if updated:
                await self.session.flush()
            return user

        user = User(
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            is_allowed=is_allowed,
        )
        self.session.add(user)
        await self.session.flush()
        return user

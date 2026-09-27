"""Handler pour la commande /categories."""

from typing import Any

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.constants import CATEGORY_EMOJIS, CATEGORY_LABELS, CategorySlug
from app.db.repositories.category_repo import CategoryRepository

router = Router(name="categories_router")

CATEGORIES_TEXT = "📂 Catégories disponibles :\n\n📚 Livres\n💻 Électronique\n🎮 Jeux Vidéo\n👕 Vêtements"


@router.message(Command("categories"))
async def handle_categories(message: Message, db_session: Any | None = None) -> None:
    """Affiche les catégories actives prises en charge par BotRadar."""
    if db_session:
        try:
            repo = CategoryRepository(db_session)
            active_cats = await repo.get_all_active()
            if active_cats:
                lines = [f"{c.icon or '📦'} {c.label}" for c in active_cats]
                text = "📂 Catégories disponibles :\n\n" + "\n".join(lines)
                await message.answer(text)
                return
        except Exception:
            pass

    await message.answer(CATEGORIES_TEXT)

"""Handler pour la commande /categories."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="categories_router")

CATEGORIES_TEXT = "📂 Catégories disponibles :\n\n📚 Livres\n💻 Électronique\n🎮 Jeux Vidéo\n👕 Vêtements"


@router.message(Command("categories"))
async def handle_categories(message: Message) -> None:
    """Affiche les catégories prises en charge par Radar Bot."""
    await message.answer(CATEGORIES_TEXT)

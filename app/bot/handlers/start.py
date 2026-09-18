"""Handler pour la commande /start."""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.bot.keyboards.start import get_start_keyboard

router = Router(name="start_router")

START_TEXT = (
    "👋 Bienvenue sur Radar Bot.\n\n"
    "Je centralise tes alertes de bonnes affaires.\n"
    "Je peux filtrer les annonces reçues, calculer leur total estimé et comparer "
    "les BD/Comics avec le meilleur prix occasion connu.\n\n"
    "Utilise /add pour créer ta première alerte."
)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Répond au message /start avec le texte d'accueil et le bouton d'action."""
    await message.answer(START_TEXT, reply_markup=get_start_keyboard())

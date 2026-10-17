"""Handler pour la commande /help."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.labels import TITLE_HELP

router = Router(name="help_router")

# Seule liste complète des commandes : le menu Telegram n'en affiche que quatre.
# Groupées par moment d'usage, libellés tutoyés et sans exclamation.
HELP_TEXT = (
    f"{TITLE_HELP}\n\n"
    "<b>Au menu</b>\n"
    "/add — Créer une alerte\n"
    "/show — Consulter mes alertes, carte par carte\n"
    "/cancel — Annuler l'opération en cours\n"
    "/help — Afficher cette aide\n\n"
    "<b>Depuis la carte d'une alerte</b>\n"
    "<i>Les boutons sous chaque carte couvrent ces actions ; les commandes</i>\n"
    "<i>restent disponibles si tu préfères écrire.</i>\n"
    "/edit — Modifier une alerte\n"
    "/pause — Mettre une alerte en pause\n"
    "/resume — Réactiver une alerte\n"
    "/delete — Supprimer une alerte\n"
    "/list — Liste compacte, par catégorie ou globale\n"
    "/categories — Catégories surveillées\n\n"
    "<b>Suivi et réglages</b>\n"
    "/stats — Statistiques d'activité\n"
    "/status — État du service, du radar et du moteur IA\n"
    "/autotune — Suggestions d'exclusions à partir de tes faux positifs"
)


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    """Affiche la liste des commandes disponibles et leur description."""
    await message.answer(HELP_TEXT)
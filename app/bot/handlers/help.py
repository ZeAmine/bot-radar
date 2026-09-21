"""Handler pour la commande /help."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="help_router")

HELP_TEXT = (
    "📖 Commandes disponibles :\n\n"
    "/add [titre] — Créer une alerte (recherche automatique)\n"
    "/show [titre/ISBN] — Consulter les alertes (ou chercher directement)\n"
    "/list [catégorie] — Liste compacte des alertes (par catégorie ou globale)\n"
    "/edit — Modifier une alerte\n"
    "/pause — Mettre en pause une alerte\n"
    "/resume — Réactiver une alerte\n"
    "/delete — Supprimer une alerte\n"
    "/categories — Afficher les catégories\n"
    "/stats — Voir les statistiques\n"
    "/autotune — Auto-tuning et suggestions d'exclusions IA (faux positifs)\n"
    "/status — État du service, du radar et de l'IA (DeepSeek)\n"
    "/cancel — Annuler l'opération en cours\n"
    "/help — Afficher l’aide"
)


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    """Affiche la liste des commandes disponibles et leur description."""
    await message.answer(HELP_TEXT)

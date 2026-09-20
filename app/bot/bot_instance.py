"""Configuration et initialisation de l'instance Bot et Dispatcher aiogram."""

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand

from app.bot.handlers import get_bot_router
from app.bot.middlewares.auth import AuthMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.core.config import settings

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="add", description="Créer une alerte (recherche auto)"),
    BotCommand(command="show", description="Afficher les alertes"),
    BotCommand(command="list", description="Liste compacte par catégorie ou globale"),
    BotCommand(command="edit", description="Modifier une alerte"),
    BotCommand(command="pause", description="Mettre en pause une alerte"),
    BotCommand(command="resume", description="Réactiver une alerte"),
    BotCommand(command="delete", description="Supprimer une alerte"),
    BotCommand(command="categories", description="Afficher les catégories"),
    BotCommand(command="stats", description="Voir les statistiques"),
    BotCommand(command="autotune", description="Auto-tuning et exclusions IA"),
    BotCommand(command="status", description="État du bot et du radar"),
    BotCommand(command="health", description="Santé du bot et de l'IA (DeepSeek)"),
    BotCommand(command="cancel", description="Annuler l'opération en cours"),
    BotCommand(command="help", description="Afficher l’aide"),
]


def create_bot() -> Bot:
    """Instancie le Bot aiogram avec parse mode HTML par défaut."""
    return Bot(
        token=settings.TELEGRAM_BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_storage() -> BaseStorage:
    """Crée le stockage FSM (Redis par défaut, MemoryStorage en fallback)."""
    try:
        if settings.REDIS_URL:
            return RedisStorage.from_url(settings.REDIS_URL)
    except Exception as exc:
        logger.warning(
            "Impossible d'initialiser RedisStorage pour FSM, utilisation de MemoryStorage: %s",
            exc,
        )
    return MemoryStorage()


def create_dispatcher(storage: BaseStorage | None = None) -> Dispatcher:
    """Crée et configure le Dispatcher aiogram."""
    if storage is None:
        storage = create_storage()

    dp = Dispatcher(storage=storage)

    # Enregistrement du middleware d'authentification et de throttling
    auth_middleware = AuthMiddleware()
    throttling_middleware = ThrottlingMiddleware()
    dp.message.middleware(auth_middleware)
    dp.message.middleware(throttling_middleware)
    dp.callback_query.middleware(auth_middleware)
    dp.callback_query.middleware(throttling_middleware)

    # Enregistrement des routeurs
    dp.include_router(get_bot_router())

    return dp


async def setup_bot_commands(bot: Bot) -> None:
    """Configure la liste des commandes Telegram dans le menu (setMyCommands)."""
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        logger.info("Commandes Telegram configurées avec succès via setMyCommands.")
    except Exception as exc:
        logger.error("Erreur lors de la configuration des commandes Telegram: %s", exc)


# Instances singleton
bot: Bot = create_bot()
dp: Dispatcher = create_dispatcher()

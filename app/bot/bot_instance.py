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
from app.bot.middlewares.unhandled_callbacks import UnconsumedCallbackMiddleware
from app.core.config import settings
from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)

# Menu Telegram volontairement court : les quatre gestes du quotidien seulement ;
# les autres commandes restent vivantes et documentées dans /help.
BOT_COMMANDS = [
    BotCommand(command="add", description="Créer une alerte (recherche auto)"),
    BotCommand(command="show", description="Afficher les alertes"),
    BotCommand(command="help", description="Afficher l’aide"),
    BotCommand(command="cancel", description="Annuler l'opération en cours"),
]


def create_bot() -> Bot:
    """Instancie le Bot aiogram avec parse mode HTML par défaut."""
    raw_token = settings.TELEGRAM_BOT_TOKEN.get_secret_value()
    if not raw_token or ":" not in raw_token:
        raw_token = "123456789:AAG_dummy_token_for_ci_test_env_12345"
    return Bot(
        token=raw_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_storage() -> BaseStorage:
    """Crée le stockage FSM (Redis par défaut, MemoryStorage en fallback)."""
    try:
        if settings.REDIS_URL:
            return RedisStorage(redis=get_redis_client())
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

    auth_middleware = AuthMiddleware()
    throttling_middleware = ThrottlingMiddleware()
    dp.message.middleware(auth_middleware)
    dp.message.middleware(throttling_middleware)
    dp.callback_query.middleware(auth_middleware)
    dp.callback_query.middleware(throttling_middleware)

    # Filet au niveau de l'update : seul niveau qui voit les callbacks qu'aucun
    # handler ne filtre, contrairement à un middleware de callback_query.
    dp.update.outer_middleware(UnconsumedCallbackMiddleware())

    dp.include_router(get_bot_router())

    return dp


async def setup_bot_commands(bot: Bot) -> None:
    """Configure la liste des commandes Telegram dans le menu (setMyCommands)."""
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        logger.info("Commandes Telegram configurées avec succès via setMyCommands.")
    except Exception as exc:
        logger.error("Erreur lors de la configuration des commandes Telegram: %s", exc)


bot: Bot = create_bot()
dp: Dispatcher = create_dispatcher()

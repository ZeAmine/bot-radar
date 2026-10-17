"""Handlers Telegram pour BotRadar."""

from aiogram import Router

from app.bot.handlers import (
    alert_chain_approval,
    alerts,
    autotune,
    callbacks,
    categories,
    help,
    start,
    stats,
    status,
    vinted_checkout,
)


def get_bot_router() -> Router:
    """Agrège et configure tous les routeurs de handlers Telegram."""
    main_router = Router(name="main_bot_router")
    main_router.include_router(start.router)
    main_router.include_router(status.router)
    main_router.include_router(help.router)
    main_router.include_router(categories.router)
    main_router.include_router(stats.router)
    main_router.include_router(autotune.router)
    main_router.include_router(alerts.router)
    main_router.include_router(alert_chain_approval.router)
    main_router.include_router(callbacks.router)
    main_router.include_router(vinted_checkout.router)
    return main_router


__all__ = ["get_bot_router", "start", "status", "help",
    "categories", "alerts", "callbacks", "stats", "autotune", "vinted_checkout",
    "alert_chain_approval"]

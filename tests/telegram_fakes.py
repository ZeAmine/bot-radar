"""Reçus Telegram synthétiques, jamais de client réseau."""

from datetime import UTC, datetime

from aiogram.types import Chat, Message

from app.services.telegram_service import DeliveryResult


def telegram_message(chat_id: int = 601, message_id: int = 42) -> Message:
    return Message(
        message_id=message_id,
        date=datetime(2026, 10, 10, tzinfo=UTC),
        chat=Chat(id=chat_id, type="private"),
    )


def telegram_reply(**kwargs) -> Message:
    return telegram_message(kwargs.get("chat_id", 601))


def acknowledged_delivery(**kwargs) -> DeliveryResult:
    """Double du contrat interne d'envoi : succès avec un vrai objet Message synthétique."""
    return DeliveryResult(
        "acknowledged", "text", receipt=telegram_message(kwargs.get("chat_id", 601))
    )

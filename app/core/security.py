"""Fonctions de sécurité et de vérification des identités / secrets."""

import secrets

from app.core.config import settings


def verify_ingest_token(token: str) -> bool:
    """Vérifie le token Bearer d'ingestion en temps constant pour éviter les timing attacks."""
    expected = settings.INGEST_API_TOKEN.get_secret_value()
    return secrets.compare_digest(token, expected)


def verify_internal_token(token: str) -> bool:
    """Vérifie le token Bearer interne en temps constant."""
    expected = settings.INTERNAL_API_TOKEN.get_secret_value()
    return secrets.compare_digest(token, expected)


def verify_webhook_secret(secret: str) -> bool:
    """Vérifie le secret du webhook Telegram."""
    expected = settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()
    return secrets.compare_digest(secret, expected)


def is_telegram_user_allowed(user_id: int) -> bool:
    """Vérifie si un utilisateur Telegram fait partie de la liste autorisée."""
    return user_id in settings.TELEGRAM_ALLOWED_USER_IDS

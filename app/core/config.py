"""Configuration globale de l'application via Pydantic Settings.

Ce fichier définit le schéma et la validation des variables d'environnement.
Toutes les valeurs réelles et secrètes sont chargées dynamiquement depuis le fichier .env.
"""

from typing import Annotated

from pydantic import BeforeValidator, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_allowed_user_ids(v: str | list[int] | None) -> list[int]:
    """Parse une liste d'identifiants Telegram séparés par des virgules."""
    if isinstance(v, list):
        return v
    if not v:
        return []
    return [int(uid.strip()) for uid in str(v).split(",") if uid.strip().isdigit()]


class Settings(BaseSettings):
    """Schéma des paramètres applicatifs chargés depuis le fichier .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    # Base de données PostgreSQL & Cache Redis
    DATABASE_URL: str = "postgresql+asyncpg://radar_user:radar_password@localhost:5432/radar_db"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: SecretStr = SecretStr("")
    TELEGRAM_ALLOWED_USER_IDS: Annotated[list[int], BeforeValidator(parse_allowed_user_ids)] = []
    TELEGRAM_WEBHOOK_SECRET: SecretStr = SecretStr("")

    # API Tokens (Ingestion et communication interne)
    INGEST_API_TOKEN: SecretStr = SecretStr("")
    INTERNAL_API_TOKEN: SecretStr = SecretStr("")

    # eBay Browse API (Optionnel)
    EBAY_CLIENT_ID: str = ""
    EBAY_CLIENT_SECRET: SecretStr = SecretStr("")
    EBAY_ENV: str = "PRODUCTION"

    # Planification
    PRICE_REFRESH_TIMES: str = "07:00,19:00"

    # Surveillance Vinted en temps réel
    RADAR_API_URL: str = "http://127.0.0.1:8000"
    VINTED_POLL_INTERVAL_SECONDS: int = 20
    VINTED_POLLER_ENABLED: bool = True
    VINTED_PROXY_URL: str | None = None

    # Mode Nuit (Heures Creuses pour préserver l'IP et éviter les bans Cloudflare)
    NIGHT_MODE_ENABLED: bool = True
    NIGHT_MODE_START_HOUR: int = 0  # 00:00 (minuit) heure de Paris
    NIGHT_MODE_END_HOUR: int = 7    # 07:00 heure de Paris
    NIGHT_MODE_TIMEZONE: str = "Europe/Paris"

    def get_vinted_proxy(self) -> str | None:
        """Retourne l'URL d'un proxy Vinted configuré.

        Supporte soit une URL unique, soit une liste d'URLs séparées par des virgules.
        """
        if not self.VINTED_PROXY_URL:
            return None
        proxies = [p.strip() for p in self.VINTED_PROXY_URL.split(",") if p.strip()]
        if not proxies:
            return None
        import random
        return random.choice(proxies)

    # Moteur IA & DeepSeek-V4.1-Flash
    DEEPSEEK_API_KEY: SecretStr = SecretStr("")
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL: str = "deepseek-flash"
    AI_ENABLED: bool = True
    AI_FEATURE_NATURAL_ADD: bool = True
    AI_FEATURE_PRICE_GLITCH: bool = False
    AI_FEATURE_HIDDEN_CONDITION: bool = True
    AI_FEATURE_VISION_GUARD: bool = False
    AI_FEATURE_PROFIT_SCORING: bool = True
    AI_FEATURE_NEGOTIATION: bool = True
    AI_FEATURE_BUNDLE_DECOMPOSITION: bool = False
    AI_FEATURE_KEYWORD_VARIANTS: bool = True
    AI_FEATURE_AUTO_TUNING: bool = True

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() == "production"


def validate_production_secrets() -> None:
    """Refuse de démarrer en production si un secret d'accès est vide."""
    if not settings.is_production:
        return
    missing = [
        name
        for name in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_WEBHOOK_SECRET",
            "INGEST_API_TOKEN",
            "INTERNAL_API_TOKEN",
        )
        if not getattr(settings, name).get_secret_value()
    ]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Secrets de production vides : {joined}")


settings = Settings()

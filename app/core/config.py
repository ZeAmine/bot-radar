"""Configuration globale de l'application via Pydantic Settings."""

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
    """Paramètres applicatifs chargés depuis l'environnement ou .env."""

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
    TELEGRAM_BOT_TOKEN: SecretStr = SecretStr("123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ")
    TELEGRAM_ALLOWED_USER_IDS: Annotated[list[int], BeforeValidator(parse_allowed_user_ids)] = []
    TELEGRAM_WEBHOOK_SECRET: SecretStr = SecretStr("mock_secret")
    TELEGRAM_WEBHOOK_URL: str = "https://localhost:8000/api/v1/telegram/webhook/"

    # API Tokens
    INGEST_API_TOKEN: SecretStr = SecretStr("mock_ingest_token")
    INTERNAL_API_TOKEN: SecretStr = SecretStr("mock_internal_token")

    # eBay Browse API
    EBAY_CLIENT_ID: str = ""
    EBAY_CLIENT_SECRET: SecretStr = SecretStr("")
    EBAY_ENV: str = "PRODUCTION"

    # Planification
    PRICE_REFRESH_TIMES: str = "07:00,19:00"

    # Surveillance Vinted en temps réel
    RADAR_API_URL: str = "http://127.0.0.1:8000"
    PUBLIC_BASE_URL: str = "https://radar.aminezegmou.fr"
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

    # Observabilité
    SENTRY_DSN: str | None = None

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() == "production"


settings = Settings()

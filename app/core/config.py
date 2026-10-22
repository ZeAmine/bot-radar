"""Configuration globale de l'application via Pydantic Settings.

Ce fichier définit le schéma et la validation des variables d'environnement.
Toutes les valeurs réelles et secrètes sont chargées dynamiquement depuis le fichier .env.
"""

from typing import Annotated

from pydantic import BeforeValidator, Field, SecretStr
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

    PROJECT_NAME: str = "BotRadar"
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    DATABASE_URL: str = "postgresql+asyncpg://radar_user:radar_password@localhost:5432/radar_db"
    REDIS_URL: str = "redis://localhost:6379/0"

    TELEGRAM_BOT_TOKEN: SecretStr = SecretStr("")
    TELEGRAM_ALLOWED_USER_IDS: Annotated[list[int], BeforeValidator(parse_allowed_user_ids)] = []
    TELEGRAM_WEBHOOK_SECRET: SecretStr = SecretStr("")

    INGEST_API_TOKEN: SecretStr = SecretStr("")
    INTERNAL_API_TOKEN: SecretStr = SecretStr("")

    EBAY_CLIENT_ID: str = ""
    EBAY_CLIENT_SECRET: SecretStr = SecretStr("")
    EBAY_ENV: str = "PRODUCTION"

    # Session utilisateur Chasse-aux-livres (historique 1 an, API /rest/jcd)
    CHASSE_AUX_LIVRES_DUIH: str | None = None
    CHASSE_ACCOUNT_EMAIL: SecretStr = SecretStr("")
    CHASSE_ACCOUNT_PASSWORD: SecretStr = SecretStr("")

    # Troisième source d'identification (ADR-033), optionnelle : vide, elle n'est ni
    # interrogée ni annoncée, et l'app démarre sans.
    GOOGLE_BOOKS_API_KEY: SecretStr = SecretStr("")

    PRICE_REFRESH_TIMES: str = "07:00,19:00"

    RADAR_API_URL: str = "http://127.0.0.1:8000"
    BOTRADAR_API_URL: str | None = None

    @property
    def api_url(self) -> str:
        """URL de l'API (supporte BOTRADAR_API_URL avec repli sur RADAR_API_URL)."""
        return self.BOTRADAR_API_URL or self.RADAR_API_URL

    VINTED_POLL_INTERVAL_SECONDS: int = 12
    VINTED_POLLER_ENABLED: bool = True
    VINTED_PROXY_ENABLED: bool = False
    VINTED_PROXY_URL: str | None = None

    # Collecte Leboncoin : désactivée par défaut, et sans sortie résidentielle
    # DataDome répond 403 même avec l'empreinte TLS de Chrome.
    LEBONCOIN_POLLER_ENABLED: bool = False
    LEBONCOIN_POLL_INTERVAL_SECONDS: int = 600
    LEBONCOIN_SEARCH_URL: str = (
        "https://www.leboncoin.fr/recherche?category=27&leisure_book_genre=comics%26mangas"
    )
    LEBONCOIN_PROXY_ENABLED: bool = False
    LEBONCOIN_PROXY_URL: str | None = None
    LEBONCOIN_CATCHUP_MAX_PAGES: int = 2
    LEBONCOIN_BLOCK_STOP_SECONDS: int = Field(default=7200, gt=0)

    # Mode Nuit (Heures Creuses pour préserver l'IP et éviter les bans Cloudflare)
    NIGHT_MODE_ENABLED: bool = True
    NIGHT_MODE_START_HOUR: int = 0  # 00:00 (minuit) heure de Paris
    NIGHT_MODE_END_HOUR: int = 7    # 07:00 heure de Paris
    NIGHT_MODE_TIMEZONE: str = "Europe/Paris"
    NIGHT_MAINTENANCE_BATCH_SIZE: int = 10

    def get_vinted_proxy(self) -> str | None:
        """Retourne l'URL d'un proxy Vinted actif.

        `VINTED_PROXY_ENABLED` doit être vrai : une URL seule n'active rien.
        Supporte soit une URL unique, soit une liste d'URLs séparées par des virgules.
        """
        if not self.VINTED_PROXY_ENABLED or not self.VINTED_PROXY_URL:
            return None
        proxies = [p.strip() for p in self.VINTED_PROXY_URL.split(",") if p.strip()]
        if not proxies:
            return None
        import random
        return random.choice(proxies)

    def get_leboncoin_proxy(self) -> str | None:
        """Proxy résidentiel de la collecte Leboncoin ; une URL seule ne l'active pas."""
        if not self.LEBONCOIN_PROXY_ENABLED or not self.LEBONCOIN_PROXY_URL:
            return None
        proxies = [p.strip() for p in self.LEBONCOIN_PROXY_URL.split(",") if p.strip()]
        if not proxies:
            return None
        import random
        return random.choice(proxies)

    # Moteur IA interchangeable via l'environnement (voir `app/providers/llm/`).
    AI_PROVIDER: str = "openai-compatible"
    AI_API_KEY: SecretStr = SecretStr("")
    AI_BASE_URL: str = "https://api.deepseek.com/v1"
    AI_MODEL: str = "deepseek-flash"
    AI_TIMEOUT_SECONDS: float = 12.0
    AI_ENABLED: bool = True
    AI_FEATURE_NATURAL_ADD: bool = True
    AI_FEATURE_PROFIT_SCORING: bool = True
    AI_FEATURE_NEGOTIATION: bool = True
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

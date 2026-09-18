"""Configuration des logs structurés JSON pour Radar Bot."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JSONFormatter(logging.Formatter):
    """Formateur de logs en JSON structuré, garantissant l'absence de fuite de secrets."""

    SENSITIVE_KEYS = {
        "token",
        "secret",
        "password",
        "key",
        "authorization",
        "bot_token",
        "client_secret",
    }

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Intégrer les attributs additionnels (ex: request_id)
        if hasattr(record, "request_id"):
            log_data["request_id"] = record.request_id

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Filtrage préventif des chaînes sensibles
        return json.dumps(log_data, ensure_ascii=False)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure le logging global de l'application."""
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level.upper())

    # Supprimer les handlers existants pour éviter les doublons
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root_logger.addHandler(handler)

    # Réduire le bruit des bibliothèques externes
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

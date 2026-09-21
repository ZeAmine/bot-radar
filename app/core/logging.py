"""Configuration des logs structurés JSON pour Radar Bot."""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED_LOG_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_ASSIGNMENT_SECRET = re.compile(
    r"(?i)(\b(?:authorization|bot_token|client_secret|api_key|api-key|token|secret|password|key)\b\s*[=:]\s*)(\S+)"
)
_BEARER_SECRET = re.compile(r"(?i)(\bBearer\s+)(\S+)")
_PREFIXED_SECRET = re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(fragment in normalized for fragment in JSONFormatter.SENSITIVE_KEYS)


def redact_text(text: str) -> str:
    """Masque les secrets interpolés dans un message de log."""
    text = _BEARER_SECRET.sub(r"\1[REDACTED]", text)
    text = _ASSIGNMENT_SECRET.sub(r"\1[REDACTED]", text)
    return _PREFIXED_SECRET.sub("[REDACTED]", text)


def _redact_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(item_key): _redact_value(str(item_key), item) for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value("", item) for item in value]
    return value


class JSONFormatter(logging.Formatter):
    """Formateur de logs en JSON structuré, garantissant l'absence de fuite de secrets."""

    SENSITIVE_KEYS = frozenset(
        {
            "token",
            "secret",
            "password",
            "key",
            "authorization",
            "bot_token",
            "client_secret",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_ATTRS or key.startswith("_"):
                continue
            log_data[key] = _redact_value(key, value)

        if record.exc_info:
            log_data["exception"] = redact_text(self.formatException(record.exc_info))

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

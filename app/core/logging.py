"""Configuration des logs structurés JSON pour BotRadar."""

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
    r'''(?i)(\b(?:authorization|bot_token|telegram_bot_token|access_token|refresh_token|'''
    r'''ingest_api_token|internal_api_token|telegram_webhook_secret|ai_api_key|'''
    r'''ebay_client_secret|google_books_api_key|client_secret|api_key|api-key|token|'''
    r'''secret|password|key)\b["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)'''
)
_TELEGRAM_TOKEN = re.compile(r"(?:\bbot|\b)\d{5,}:[A-Za-z0-9_-]{20,}")
_BEARER_SECRET = re.compile(r"(?i)(\bBearer\s+)(\S+)")
_PREFIXED_SECRET = re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")
_WEBHOOK_SECRET = re.compile(r"(/api/v1/telegram/webhook/)[^/\s?#\"']+")
_DUIH_SECRET = re.compile(r"(?i)(\bduih\s*=\s*)[^&\s\"'<>]+")
_COOKIE_HEADER = re.compile(r"(?i)(\bCookie\s*:\s*)[^\r\n]+")


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in {"cookie", "set_cookie", "cookies", "duih"} or any(
        fragment in normalized for fragment in JSONFormatter.SENSITIVE_KEYS
    )


def redact_text(text: str) -> str:
    """Masque les secrets interpolés dans un message de log."""
    text = _BEARER_SECRET.sub(r"\1[REDACTED]", text)
    text = _ASSIGNMENT_SECRET.sub(r"\1[REDACTED]", text)
    text = _WEBHOOK_SECRET.sub(r"\1[REDACTED]", text)
    text = _DUIH_SECRET.sub(r"\1[REDACTED]", text)
    text = _COOKIE_HEADER.sub(r"\1[REDACTED]", text)
    text = _TELEGRAM_TOKEN.sub("[REDACTED]", text)
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
    if isinstance(value, (list, tuple)):
        return [_redact_value("", item) for item in value]
    return value


class JSONFormatter(logging.Formatter):
    """Formateur JSON masquant les formats de secrets reconnus, y compris dans les exceptions."""

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

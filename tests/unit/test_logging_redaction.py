"""Les secrets de session ne traversent pas la sortie JSON des journaux."""

import io
import json
import logging
from collections.abc import Iterator

import pytest

from app.core.logging import JSONFormatter


@pytest.fixture
def captured_log() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.Logger("radar.redaction.test")
    logger.addHandler(handler)
    try:
        yield logger, stream
    finally:
        handler.close()


@pytest.mark.parametrize(
    ("text", "secret", "diagnostic"),
    [
        (
            "POST https://radar.test/api/v1/telegram/webhook/webhook-factice?status=413",
            "webhook-factice",
            "/api/v1/telegram/webhook/[REDACTED]?status=413",
        ),
        (
            "GET https://catalogue.test/rest/jcd?id=42&duih=duih-factice&days=365",
            "duih-factice",
            "id=42&duih=[REDACTED]&days=365",
        ),
        ("Cookie: session=cookie-factice; theme=dark", "cookie-factice", "Cookie:"),
    ],
)
def test_session_secrets_are_redacted_in_every_json_output(
    captured_log: tuple[logging.Logger, io.StringIO],
    text: str,
    secret: str,
    diagnostic: str,
) -> None:
    """Message interpolé, extras imbriqués et exception passent par le vrai handler."""
    logger, stream = captured_log
    logger.warning("Échec HTTP : %s", text, extra={"request_id": "req-42"})
    logger.info(
        "Diagnostic HTTP",
        extra={"context": {"url": text, "details": [text, {"response": text}]}},
    )
    try:
        raise RuntimeError(text)
    except RuntimeError:
        logger.exception("Échec de la requête", extra={"request_id": "req-42"})

    output = stream.getvalue()
    assert secret not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert len(records) == 3
    assert diagnostic in records[0]["message"]
    assert records[0]["request_id"] == "req-42"
    assert diagnostic in records[1]["context"]["url"]
    assert diagnostic in records[1]["context"]["details"][0]
    assert diagnostic in records[1]["context"]["details"][1]["response"]
    assert "RuntimeError" in records[2]["exception"]
    assert diagnostic in records[2]["exception"]


def test_session_headers_and_tokens_are_sensitive_structured_fields(
    captured_log: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = captured_log
    logger.info(
        "Requête HTTP",
        extra={
            "context": {
                "headers": {"Cookie": "session=cookie-factice", "Accept": "application/json"},
                "duih": "duih-factice",
                "cookie_count": 2,
            },
        },
    )
    output = stream.getvalue()
    assert "cookie-factice" not in output
    assert "duih-factice" not in output
    context = json.loads(output)["context"]
    assert context["headers"]["Cookie"] == "[REDACTED]"
    assert context["headers"]["Accept"] == "application/json"
    assert context["duih"] == "[REDACTED]"
    assert context["cookie_count"] == 2


def test_nested_tuples_do_not_bypass_secret_redaction(
    captured_log: tuple[logging.Logger, io.StringIO],
) -> None:
    """Les tuples, sérialisés en tableaux JSON, suivent le même masque récursif."""
    logger, stream = captured_log
    logger.info(
        "Diagnostic HTTP 413",
        extra={
            "context": (
                {
                    "url": "/api/v1/telegram/webhook/webhook-factice",
                    "duih": "duih-factice",
                    "request_id": "req-42",
                },
                ("Cookie: session=cookie-factice",),
            ),
        },
    )
    output = stream.getvalue()
    for secret in ("webhook-factice", "duih-factice", "cookie-factice"):
        assert secret not in output
    context = json.loads(output)["context"]
    assert context[0]["url"] == "/api/v1/telegram/webhook/[REDACTED]"
    assert context[0]["duih"] == "[REDACTED]"
    assert context[0]["request_id"] == "req-42"
    assert context[1] == ["Cookie: [REDACTED]"]


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer bearer-factice", "bearer-factice"),
        ("token=token-factice", "token-factice"),
        ("secret=secret-factice", "secret-factice"),
        ("password=password-factice", "password-factice"),
        ("api_key=api-factice", "api-factice"),
        ("key=key-factice", "key-factice"),
        ("Clé sk-factice123456789", "sk-factice123456789"),
    ],
)
def test_existing_secret_formats_stay_redacted_in_emitted_logs(
    captured_log: tuple[logging.Logger, io.StringIO], text: str, secret: str
) -> None:
    logger, stream = captured_log
    logger.warning("Erreur HTTP 403 : %s", text, extra={"request_id": "req-42"})
    output = stream.getvalue()
    assert secret not in output
    payload = json.loads(output)
    assert "Erreur HTTP 403" in payload["message"]
    assert "[REDACTED]" in payload["message"]
    assert payload["request_id"] == "req-42"


def test_existing_sensitive_extras_stay_redacted_in_emitted_logs(
    captured_log: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = captured_log
    logger.info(
        "Diagnostic HTTP 403",
        extra={
            "bot_token": "bot-factice",
            "context": [{"Authorization": "Bearer bearer-factice", "api_key": "api-factice"}],
            "request_id": "req-42",
        },
    )
    output = stream.getvalue()
    for secret in ("bot-factice", "bearer-factice", "api-factice"):
        assert secret not in output
    payload = json.loads(output)
    assert payload["bot_token"] == "[REDACTED]"
    assert payload["context"] == [{"Authorization": "[REDACTED]", "api_key": "[REDACTED]"}]
    assert payload["request_id"] == "req-42"


@pytest.mark.parametrize("location", ["message", "exception", "nested"])
@pytest.mark.parametrize(("text", "secret"), [
    ("https://api.telegram.org/bot123456789:synthetic_token_abcdefghijklmnopqrstuvwxyz/sendMessage",
     "123456789:synthetic_token_abcdefghijklmnopqrstuvwxyz"),
    ('{"api_key": "synthetic-private-key"}', "synthetic-private-key"),
    ("secret='synthetic alpha beta'", "synthetic alpha beta"),
    ("TELEGRAM_BOT_TOKEN=synthetic-bot-value", "synthetic-bot-value"),
    ('{"access_token": "synthetic-access-value"}', "synthetic-access-value"),
    ("AI_API_KEY=synthetic-engine-value", "synthetic-engine-value"),
])
def test_url_and_quoted_secrets_are_redacted(captured_log, location, text, secret):
    logger, stream = captured_log
    if location == "message":
        logger.error("Échec : %s", text)
    elif location == "nested":
        logger.error("Échec", extra={"context": [{"detail": (text,)}]})
    else:
        try:
            raise RuntimeError(text)
        except RuntimeError:
            logger.exception("Échec")
    output = stream.getvalue()
    assert secret not in output
    assert all(part not in output for part in secret.split())
    assert "[REDACTED]" in output
    json.loads(output)


def test_public_paths_and_diagnostics_stay_readable(
    captured_log: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = captured_log
    logger.warning(
        "HTTP 413 sur /api/v1/ingest/listings et /api/v1/telegram/status",
        extra={"route": "/api/v1/internal/alerts", "request_id": "req-42"},
    )
    payload = json.loads(stream.getvalue())
    assert payload["message"] == "HTTP 413 sur /api/v1/ingest/listings et /api/v1/telegram/status"
    assert payload["route"] == "/api/v1/internal/alerts"
    assert payload["request_id"] == "req-42"

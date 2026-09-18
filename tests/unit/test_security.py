from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, Message, User
from fastapi.testclient import TestClient
import pytest

from app.api.middlewares.security import rate_limiter
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.core.config import settings
from app.core.rate_limiter import SlidingWindowRateLimiter
from app.core.security import (
    is_telegram_user_allowed,
    verify_ingest_token,
    verify_internal_token,
    verify_webhook_secret,
)
from app.core.url_validator import is_safe_external_url
from app.main import app


def test_verify_ingest_token():
    valid = settings.INGEST_API_TOKEN.get_secret_value()
    assert verify_ingest_token(valid) is True
    assert verify_ingest_token("wrong_token") is False


def test_verify_internal_token():
    valid = settings.INTERNAL_API_TOKEN.get_secret_value()
    assert verify_internal_token(valid) is True
    assert verify_internal_token("wrong_token") is False


def test_verify_webhook_secret():
    valid = settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()
    assert verify_webhook_secret(valid) is True
    assert verify_webhook_secret("wrong_secret") is False


def test_is_telegram_user_allowed():
    settings.TELEGRAM_ALLOWED_USER_IDS = [12345678, 87654321]
    assert is_telegram_user_allowed(12345678) is True
    assert is_telegram_user_allowed(87654321) is True
    assert is_telegram_user_allowed(99999999) is False


def test_security_headers_middleware():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert "Strict-Transport-Security" in response.headers
    assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert response.headers.get("Server") == "Radar-API"


def test_payload_size_limit_middleware():
    client = TestClient(app)
    # Test rejet payload > 512 Ko
    large_headers = {"content-length": "600000", "content-type": "application/json"}
    response = client.post("/api/v1/ingest/listings", headers=large_headers, content=b"{}")
    assert response.status_code == 413
    assert "Payload trop volumineux" in response.json()["detail"]

    # Test payload de taille normale
    normal_headers = {"content-length": "20", "content-type": "application/json"}
    response = client.post("/api/v1/ingest/listings", headers=normal_headers, content=b'{"test": 1}')
    assert response.status_code != 413


def test_rate_limit_middleware():
    client = TestClient(app)
    rate_limiter.reset_for_test()
    ip_headers = {"X-Forwarded-For": "203.0.113.42"}

    # Route /api/v1/internal est limitée à 30 req/min
    # Les 30 premières requêtes passent
    for _ in range(30):
        res = client.get("/api/v1/internal/refresh-prices", headers=ip_headers)
        assert res.status_code != 429

    # La 31e doit être rejetée en HTTP 429
    res_blocked = client.get("/api/v1/internal/refresh-prices", headers=ip_headers)
    assert res_blocked.status_code == 429
    assert res_blocked.headers.get("Retry-After") is not None
    assert res_blocked.headers.get("X-RateLimit-Remaining") == "0"


@pytest.mark.asyncio
async def test_sliding_window_rate_limiter():
    limiter = SlidingWindowRateLimiter()
    key = "test_user_limit"

    # 3 requêtes max sur 10 secondes
    ok1, rem1, _ = await limiter.check_rate_limit(key, max_requests=3, window_seconds=10)
    ok2, rem2, _ = await limiter.check_rate_limit(key, max_requests=3, window_seconds=10)
    ok3, rem3, _ = await limiter.check_rate_limit(key, max_requests=3, window_seconds=10)

    assert ok1 is True and rem1 == 2
    assert ok2 is True and rem2 == 1
    assert ok3 is True and rem3 == 0

    # 4e requête doit être bloquée
    blocked, rem4, retry_after = await limiter.check_rate_limit(key, max_requests=3, window_seconds=10)
    assert blocked is False
    assert rem4 == 0
    assert retry_after > 0


def test_anti_ssrf_validator():
    # Hôtes et IPs interdits
    assert is_safe_external_url("http://localhost:8000")[0] is False
    assert is_safe_external_url("http://127.0.0.1:8000")[0] is False
    assert is_safe_external_url("http://169.254.169.254/latest/meta-data")[0] is False
    assert is_safe_external_url("http://10.0.0.1/admin")[0] is False
    assert is_safe_external_url("http://192.168.1.50")[0] is False
    assert is_safe_external_url("file:///etc/passwd")[0] is False
    assert is_safe_external_url("ftp://example.com")[0] is False

    # URLs légitimes
    assert is_safe_external_url("https://www.vinted.fr/catalog?search_text=rip")[0] is True
    assert is_safe_external_url("https://www.chasse-aux-livres.fr/prix/2800142449")[0] is True

    # Filtrage par domaine autorisé
    ok_domain, _ = is_safe_external_url(
        "https://www.vinted.fr/items/123", allowed_domains=("vinted.fr", "vinted.com")
    )
    assert ok_domain is True
    bad_domain, _ = is_safe_external_url(
        "https://evil.com/items/123", allowed_domains=("vinted.fr", "vinted.com")
    )
    assert bad_domain is False


@pytest.mark.asyncio
async def test_throttling_middleware():
    limiter = SlidingWindowRateLimiter()
    middleware = ThrottlingMiddleware(rate_limit_per_second=2, window_seconds=1, limiter=limiter)
    handler = AsyncMock(return_value="success")

    user = User(id=4242, is_bot=False, first_name="Amine")
    cb = MagicMock(spec=CallbackQuery)
    cb.answer = AsyncMock()
    data = {"event_from_user": user}

    # 1ère et 2e actions autorisées
    r1 = await middleware(handler, cb, data)
    r2 = await middleware(handler, cb, data)
    assert r1 == "success"
    assert r2 == "success"

    # 3e action dans la même seconde bloquée
    r3 = await middleware(handler, cb, data)
    assert r3 is None
    assert cb.answer.call_count == 1


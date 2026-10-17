"""Middlewares de sécurité HTTP pour l'API FastAPI."""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.rate_limiter import SlidingWindowRateLimiter
from app.core.redis import get_redis_pool

logger = logging.getLogger("radar.security.middleware")

# Instance partagée par les middlewares de rate limiting
rate_limiter = SlidingWindowRateLimiter(redis_pool_factory=get_redis_pool)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Injecte des en-têtes HTTP de sécurité stricts et masque les bannières serveur."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        # Anti MIME-sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Anti clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # HSTS strict : HTTPS imposé, sous-domaines inclus
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Référent restreint
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Masque la bannière serveur
        response.headers["Server"] = "BotRadar-API"
        # Capteurs navigateur désactivés
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        return response


class PayloadSizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejette immédiatement les requêtes dont la taille dépasse la limite
    autorisée (Anti-OOM / Anti-DDoS).

    Une requête en ``Transfer-Encoding: chunked`` n'annonce pas sa taille : sans
    refus explicite, elle échappe au contrôle et son corps entier finit chargé en
    mémoire par le handler (`await request.json()`), ce qui suffit à tuer le
    processus. Aucun client de cette API — webhook Telegram, ingestion, routes
    internes — n'envoie de corps streamé : le chunked est donc refusé.
    """

    def __init__(self, app: Callable[..., Any], max_bytes: int = 524_288) -> None:  # 512 Ko
        super().__init__(app)
        self.max_bytes = max_bytes

    def _payload_too_large(self) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={
                "detail": (f"Payload trop volumineux (limite: {self.max_bytes // 1024} Ko).")
            },
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length_header = request.headers.get("content-length")
        if content_length_header:
            try:
                content_length = int(content_length_header)
                if content_length > self.max_bytes:
                    logger.warning(
                        "Requête rejetée : taille du payload (%d octets) "
                        "supérieure à la limite (%d octets).",
                        content_length,
                        self.max_bytes,
                    )
                    return self._payload_too_large()
            except ValueError:
                pass
        elif "chunked" in request.headers.get("transfer-encoding", "").lower():
            path = request.url.path
            webhook_prefix = "/api/v1/telegram/webhook/"
            if path.startswith(webhook_prefix):
                path = f"{webhook_prefix}[REDACTED]"
            logger.warning(
                "Requête rejetée : taille de payload indéterminable "
                "(Transfer-Encoding: chunked) sur %s.",
                path,
            )
            return self._payload_too_large()

        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Protection anti-brute-force et anti-flood sur les routes sensibles de l'API."""

    # (préfixe de route, max_requêtes, fenêtre_secondes)
    ROUTE_LIMITS = [
        ("/api/v1/internal", 30, 60),       # Anti brute-force
        ("/api/v1/books", 30, 60),          # Même classe que les routes internes
        ("/api/v1/ingest", 1200, 60),       # Trafic d'ingestion attendu élevé
        ("/api/v1/telegram/webhook", 120, 60), # Trafic webhook Telegram attendu
    ]

    def __init__(
        self, app: Callable[..., Any], limiter: SlidingWindowRateLimiter | None = None
    ) -> None:
        super().__init__(app)
        self.limiter = limiter or rate_limiter

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        """Résout l'IP cliente en faisant confiance au dernier hop du proxy.

        La passerelle (Caddy) *ajoute* l'IP réelle en fin de `X-Forwarded-For`.
        Prendre le premier élément laisserait un client forger un en-tête unique par
        requête et contourner entièrement le rate limiting. On retient donc la
        dernière IP non vide, posée par notre proxy de confiance, avec repli sur
        l'adresse de la socket pour les connexions directes.
        """
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
            if hops:
                return hops[-1]
        if request.client and request.client.host:
            return request.client.host
        return "unknown"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        matching_rule = None
        for prefix, max_req, window_sec in self.ROUTE_LIMITS:
            if path.startswith(prefix):
                matching_rule = (prefix, max_req, window_sec)
                break

        if not matching_rule:
            return await call_next(request)

        prefix, max_req, window_sec = matching_rule
        client_ip = self._get_client_ip(request)
        if client_ip in {"127.0.0.1", "::1", "localhost"}:
            return await call_next(request)
        rate_key = f"api:{prefix.strip('/').replace('/', '_')}:{client_ip}"

        is_allowed, remaining, retry_after = await self.limiter.check_rate_limit(
            key=rate_key,
            max_requests=max_req,
            window_seconds=window_sec,
        )

        if not is_allowed:
            logger.warning(
                "Rate limit dépassé pour IP %s sur la route %s (retry-after: %ds)",
                client_ip,
                path,
                retry_after,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Trop de requêtes. Veuillez patienter avant de réessayer."},
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(max_req),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(max_req)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

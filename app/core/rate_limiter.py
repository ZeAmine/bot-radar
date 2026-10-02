"""Gestionnaire de rate-limiting par fenêtre glissante (Redis avec repli mémoire)."""

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable

import redis.asyncio as aioredis

logger = logging.getLogger("radar.security.rate_limiter")


class SlidingWindowRateLimiter:
    """Limiteur de débit à fenêtre glissante ultra-rapide.

    Utilise Redis (ZSET) si disponible pour une synchronisation multi-processus / multi-workers,
    avec repli automatique et transparent en mémoire locale si Redis est
    temporairement inaccessible.
    """

    def __init__(
        self,
        redis_pool_factory: Callable[[], aioredis.ConnectionPool] | None = None,
    ) -> None:
        self._redis_pool_factory = redis_pool_factory
        # Repli mémoire : dict de deque de timestamps (float)
        self._memory_store: dict[str, deque[float]] = defaultdict(deque)
        self._last_cleanup: float = time.time()
        # Client Redis partagé, construit paresseusement une seule fois par instance
        self._redis_client: aioredis.Redis | None = None

    def _get_redis_client(self) -> aioredis.Redis | None:
        """Retourne le client Redis de l'instance (pool de connexions partagé)."""
        if self._redis_client is not None:
            return self._redis_client
        if self._redis_pool_factory:
            try:
                pool = self._redis_pool_factory()
                if pool:
                    self._redis_client = aioredis.Redis(connection_pool=pool)
                    return self._redis_client
            except Exception as exc:
                logger.warning("Échec d'obtention de connexion Redis pour rate-limiter: %s", exc)
        return None

    def _cleanup_memory_if_needed(self, now: float) -> None:
        """Nettoie périodiquement les clés inactives en mémoire pour éviter toute fuite mémoire."""
        if now - self._last_cleanup > 300:  # toutes les 5 minutes
            self._last_cleanup = now
            keys_to_remove = []
            for k, timestamps in self._memory_store.items():
                if not timestamps or now - timestamps[-1] > 3600:
                    keys_to_remove.append(k)
            for k in keys_to_remove:
                self._memory_store.pop(k, None)

    async def check_rate_limit(
        self,
        key: str,
        max_requests: int,
        window_seconds: int = 60,
    ) -> tuple[bool, int, int]:
        """Vérifie si une requête est autorisée dans la fenêtre glissante.

        Args:
            key: Identifiant unique (ex: IP, token ou user_id).
            max_requests: Nombre maximal de requêtes autorisées dans la fenêtre.
            window_seconds: Durée de la fenêtre glissante en secondes (défaut: 60s).

        Returns:
            tuple (is_allowed: bool, remaining_requests: int, retry_after_seconds: int)
        """
        now = time.time()
        clear_before = now - window_seconds

        # 1. Tentative via Redis
        client = self._get_redis_client()
        if client:
            redis_key = f"rl:{key}"
            try:
                pipe = client.pipeline()
                pipe.zremrangebyscore(redis_key, 0, clear_before)
                pipe.zadd(redis_key, {str(now): now})
                pipe.zcard(redis_key)
                pipe.expire(redis_key, window_seconds + 5)
                results = await pipe.execute()

                current_count = int(results[2])
                if current_count <= max_requests:
                    remaining = max(0, max_requests - current_count)
                    return True, remaining, 0

                # Trop de requêtes : récupérer le plus ancien élément pour calculer retry_after
                oldest = await client.zrange(redis_key, 0, 0, withscores=True)
                if oldest:
                    oldest_time = float(oldest[0][1])
                    retry_after = max(1, int(window_seconds - (now - oldest_time)))
                else:
                    retry_after = window_seconds

                return False, 0, retry_after
            except Exception as exc:
                logger.debug("Repli sur rate-limiter mémoire suite erreur Redis: %s", exc)

        # 2. Repli en mémoire locale
        self._cleanup_memory_if_needed(now)
        timestamps = self._memory_store[key]

        # Nettoyage des timestamps expirés dans la deque
        while timestamps and timestamps[0] <= clear_before:
            timestamps.popleft()

        if len(timestamps) < max_requests:
            timestamps.append(now)
            remaining = max_requests - len(timestamps)
            return True, remaining, 0

        # Limite atteinte
        oldest_ts = timestamps[0]
        retry_after = max(1, int(window_seconds - (now - oldest_ts)))
        return False, 0, retry_after

    def reset_for_test(self) -> None:
        """Réinitialise le stockage mémoire (utile pour les tests unitaires)."""
        self._memory_store.clear()

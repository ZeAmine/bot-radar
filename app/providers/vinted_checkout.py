"""Préparation d'un checkout Vinted à la demande (bouton « Acheter »).

Établi en lisant le client web Vinted lui-même (le bundle JS servi par
`www.vinted.fr`), pas par supposition :

1. **Aucun lien simple n'ouvre le checkout.** La page `/checkout` exige un
   `purchase_id` créé côté serveur ; le bouton « Acheter » du site enchaîne
   deux `POST` authentifiés avant de naviguer :
   - `POST {site}/api/v2/conversations` (repli) ou
     `POST api.vinted.fr/messaging/main/inquiries` — crée une transaction
     acheteur↔vendeur. Effet de bord réel : un fil de discussion avec le
     vendeur est ouvert **avant tout paiement** ;
   - `POST {site}/api/v2/purchases/checkout/build` — transforme cette
     transaction en panier (`checkout.id`), puis redirige vers
     `/checkout?purchase_id=…&order_id=…&order_type=transaction`.
2. **La session doit être authentifiée.** Les deux `POST` sont refusés à un
   visiteur anonyme (`user_login_required`, `Access denied`) ; le résultat
   appartient au **compte**, pas à la session : l'utilisateur ouvre donc le
   lien de checkout depuis son propre appareil, avec sa propre session, et
   choisit lui-même point relais et paiement. Aucune session n'est transférée.
3. **DataDome protège le build.** Une sonde serveur sur
   `purchases/checkout/build` a reçu le défi `geo.captcha-delivery.com`.
   Aucun contournement n'est tenté : un défi se traduit par `BLOCKED` et le
   repli « Voir l'annonce ». Un cookie `datadome` valide dans la session
   injectée peut lever ce défi ; rien ne le garantit.

Le module ne journalise jamais la valeur d'un cookie, d'un jeton ni d'un lien
de checkout.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.http_client import get_shared_http_client
from app.core.redis import get_redis_client
from app.domain.enums import VintedCheckoutStatus

logger = logging.getLogger("radar.providers.vinted_checkout")

SITE_URL = "https://www.vinted.fr"
# Passerelle des achats du site : le client web préfixe ces routes par
# `/api/v2` (`API_BASE_PATH` du bundle).
API_BASE_URL = f"{SITE_URL}/api/v2"
# Messagerie « nouveau backend » : service distinct appelé par le bouton
# « Acheter » du site lorsque les drapeaux de bascule sont actifs.
INQUIRIES_URL = "https://api.vinted.fr/messaging/main/inquiries"

# Clé Redis de la session Vinted **authentifiée**, injectée par
# `scripts/sync_vinted_checkout_session.py`. Elle est séparée de la session du
# scraper (`VintedSessionManager`) : ce dernier régénère la sienne en anonyme,
# ce qui écraserait les cookies de compte.
REDIS_CHECKOUT_SESSION_KEY = "radar:vinted_checkout_session"
CHECKOUT_SESSION_TTL_SECONDS = 18 * 3600

# Noms de cookies qui portent l'identité du compte. Sans l'un des deux, la
# session injectée n'est qu'un visiteur anonyme.
AUTH_COOKIE_NAMES = ("access_token_web", "_vinted_fr_session")

# Coupe-circuit : clés Redis, durées et seuils. Les pauses « dures » couvrent le
# temps qu'un défi ou une limitation se relâche d'eux-mêmes ; aucune relance
# automatique n'a lieu, seule une expiration suivie d'un clic explicite.
PAUSE_KEY = "radar:vinted_checkout:pause"
FAILURE_COUNTER_KEY = "radar:vinted_checkout:failures"
BLOCKING_STATUSES = frozenset(
    {VintedCheckoutStatus.BLOCKED, VintedCheckoutStatus.RATE_LIMITED}
)
SOFT_FAILURE_STATUSES = frozenset(
    {
        VintedCheckoutStatus.AUTH_REQUIRED,
        VintedCheckoutStatus.TIMEOUT,
        VintedCheckoutStatus.NETWORK,
        VintedCheckoutStatus.UNEXPECTED,
    }
)
BLOCK_PAUSE_SECONDS = 30 * 60
FAILURE_PAUSE_SECONDS = 15 * 60
FAILURE_WINDOW_SECONDS = 15 * 60
FAILURE_THRESHOLD = 3

# Jeton CSRF statique du client web (constante du bundle Vinted).
CSRF_TOKEN = "75f6c9fa-dc8e-4e52-a000-e09dd4084b3e"

# En-têtes du client web authentifié, repris du bundle. Aucun cookie ni jeton
# n'y figure : ils sont passés séparément, par requête.
VINTED_WEB_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Locale": "fr-FR",
    "Platform": "web",
    "X-Next-App": "marketplace-web",
    "X-CSRF-Token": CSRF_TOKEN,
}

# Réponses d'authentification observées sur la passerelle : la session n'est
# plus acceptée. On ne devine pas d'autres codes.
_AUTH_MESSAGE_CODES = frozenset(
    {"user_login_required", "access_denied", "invalid_token", "not_authenticated"}
)
_CAPTCHA_HOST = "captcha-delivery.com"
_SELLER_ID_PATTERN = re.compile(r'"seller_id"\s*:\s*"?(\d+)"?')


class VintedCheckoutError(Exception):
    """Échec typé d'une préparation de checkout, sans secret ni lien."""

    def __init__(self, status: VintedCheckoutStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Session authentifiée (lecture/écriture Redis, jamais loguée)
# ---------------------------------------------------------------------------


async def resolve_checkout_cookies() -> dict[str, str]:
    """Cookies de la session de compte, ou dictionnaire vide si absente.

    Une session illisible (Redis muet, JSON corrompu) se lit « absente » :
    elle ne doit jamais lever au milieu d'un clic utilisateur.
    """
    try:
        raw = await get_redis_client().get(REDIS_CHECKOUT_SESSION_KEY)
    except Exception as exc:
        logger.debug("Session checkout Vinted illisible : %s", exc)
        return {}
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        data: Any = json.loads(raw)
    except ValueError:
        logger.warning("Session checkout Vinted illisible : JSON invalide (valeur masquée).")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }


async def store_checkout_cookies(cookies: Mapping[str, str]) -> int:
    """Persiste la session injectée ; rend le nombre de cookies retenus.

    Redis indisponible lève : c'est un geste d'installation explicite, son
    échec doit se voir plutôt que se perdre.
    """
    payload = {str(key): str(value) for key, value in cookies.items() if str(value)}
    await get_redis_client().set(
        REDIS_CHECKOUT_SESSION_KEY,
        json.dumps(payload),
        ex=CHECKOUT_SESSION_TTL_SECONDS,
    )
    return len(payload)


async def clear_checkout_cookies() -> None:
    """Purge la session de compte (geste explicite, jamais automatique)."""
    await get_redis_client().delete(REDIS_CHECKOUT_SESSION_KEY)


def session_is_authenticated(cookies: Mapping[str, str]) -> bool:
    """Vrai si la session porte un cookie d'identité de compte."""
    return any(bool(cookies.get(name)) for name in AUTH_COOKIE_NAMES)


# ---------------------------------------------------------------------------
# Coupe-circuit : ne pas insister après un blocage, une limitation ou une panne
# ---------------------------------------------------------------------------


class CheckoutBackoff:
    """Suspend temporairement les préparations après un signal défavorable.

    Le but est de **ne pas insister** : un défi anti-robot, une limitation ou une
    rafale d'échecs ne se contournent pas en recliquant, et réessayer à chaque
    clic abîme la réputation de l'IP et du compte. La suspension est portée par
    Redis, donc partagée si plusieurs clics arrivent en parallèle.

    Elle est **temporaire et sans boucle** : rien ne se relance tout seul. Après
    expiration, c'est un clic **explicite** de l'utilisateur qui retente. Redis
    muet ne bloque jamais l'utilisateur : la protection est un garde-fou, pas une
    dépendance dure.

    Le client Redis est résolu **à chaque appel** (`redis_getter` facultatif),
    jamais capturé à la construction : un client figé à l'import rendrait la
    classe insensible à toute injection (tests compris) et garderait une
    connexion ouverte au-delà de son pool.
    """

    def __init__(self, redis_getter: Any = None) -> None:
        self._redis_getter = redis_getter or get_redis_client

    def _client(self) -> Any:
        return self._redis_getter()

    async def request_failure(self, status: VintedCheckoutStatus) -> None:
        """Compte un échec : pose une suspension si le signal est défavorable.

        Un défi anti-robot ou une limitation (429) suspendent **immédiatement**,
        pour la durée la plus longue. Une rafale d'échecs « doux » (session,
        réseau, délai, réponse inattendue) suspend après `FAILURE_THRESHOLD`.
        """
        if status in BLOCKING_STATUSES:
            await self._pause(BLOCK_PAUSE_SECONDS)
            logger.warning(
                "Préparations de checkout suspendues %d s après %s.",
                BLOCK_PAUSE_SECONDS,
                status.value,
            )
            return
        if status not in SOFT_FAILURE_STATUSES:
            return
        try:
            client = self._client()
            count = await client.incr(FAILURE_COUNTER_KEY)
            if count == 1:
                await client.expire(FAILURE_COUNTER_KEY, FAILURE_WINDOW_SECONDS)
            if count >= FAILURE_THRESHOLD:
                await self._pause(FAILURE_PAUSE_SECONDS)
                await client.delete(FAILURE_COUNTER_KEY)
                logger.warning(
                    "Préparations de checkout suspendues %d s après %d échecs.",
                    FAILURE_PAUSE_SECONDS,
                    count,
                )
        except Exception as exc:  # pragma: no cover - Redis indisponible
            logger.debug("Compteur d'échecs de checkout indisponible : %s", exc)

    async def clear_failures(self) -> None:
        """Une réussite efface la série d'échecs doux (jamais la suspension posée)."""
        try:
            await self._client().delete(FAILURE_COUNTER_KEY)
        except Exception as exc:  # pragma: no cover - Redis indisponible
            logger.debug("Compteur d'échecs de checkout non effacé : %s", exc)

    async def remaining_seconds(self) -> int:
        """Secondes restantes de suspension, 0 si aucune ou si Redis est muet."""
        try:
            ttl = await self._client().ttl(PAUSE_KEY)
        except Exception as exc:
            logger.debug("Suspension de checkout illisible : %s", exc)
            return 0
        return ttl if isinstance(ttl, int) and ttl > 0 else 0

    async def _pause(self, seconds: int) -> None:
        try:
            await self._client().set(PAUSE_KEY, "1", ex=seconds)
        except Exception as exc:  # pragma: no cover - Redis indisponible
            logger.debug("Suspension de checkout non posée : %s", exc)



# ---------------------------------------------------------------------------
# Frontière HTTP
# ---------------------------------------------------------------------------


def web_request_headers(cookies: Mapping[str, str]) -> dict[str, str]:
    """En-têtes d'une requête au nom du compte, cookies compris.

    Les cookies sont posés dans l'en-tête, par requête : les mettre dans le
    client partagé les ferait fuiter vers les autres appels du processus.
    """
    headers = dict(VINTED_WEB_HEADERS)
    headers["Origin"] = SITE_URL
    headers["Referer"] = f"{SITE_URL}/"
    token = cookies.get("access_token_web")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    cookie_header = _cookie_header(cookies)
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def _cookie_header(cookies: Mapping[str, str]) -> str:
    """En-tête `Cookie` construit à la main.

    Un nom ou une valeur portant un séparateur est écarté plutôt que de
    corrompre l'en-tête.
    """
    parts: list[str] = []
    for name, value in cookies.items():
        if not name:
            continue
        if any(char in f"{name}{value}" for char in (";", "\r", "\n")):
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _is_datadome_challenge(response: httpx.Response) -> bool:
    """Distingue un défi anti-robot d'un simple refus d'accès."""
    if response.status_code not in (403, 429):
        return False
    if response.headers.get("x-dd-b"):
        return True
    return _CAPTCHA_HOST in response.text[:4000]


def _message_code(response: httpx.Response) -> str | None:
    try:
        data: Any = response.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        code = data.get("message_code")
        if isinstance(code, str):
            return code
    return None


def _raise_for_response(response: httpx.Response) -> None:
    """Traduit un refus Vinted en échec typé — sans inventer de code."""
    status = response.status_code
    if 200 <= status < 300:
        return
    if _is_datadome_challenge(response):
        raise VintedCheckoutError(
            VintedCheckoutStatus.BLOCKED,
            "Vinted a demandé une vérification anti-robot.",
        )
    if status in (401, 403):
        raise VintedCheckoutError(
            VintedCheckoutStatus.AUTH_REQUIRED,
            "La session Vinted n'est plus acceptée.",
        )
    if status == 429:
        raise VintedCheckoutError(
            VintedCheckoutStatus.RATE_LIMITED,
            "Vinted limite temporairement les requêtes.",
        )
    if _message_code(response) in _AUTH_MESSAGE_CODES:
        raise VintedCheckoutError(
            VintedCheckoutStatus.AUTH_REQUIRED,
            "La session Vinted n'est plus acceptée.",
        )
    if status in (404, 409, 410, 422):
        raise VintedCheckoutError(
            VintedCheckoutStatus.UNAVAILABLE,
            "L'article n'est plus disponible.",
        )
    raise VintedCheckoutError(
        VintedCheckoutStatus.UNEXPECTED,
        f"Réponse inattendue de Vinted ({status}).",
    )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        data: Any = response.json()
    except ValueError as exc:
        raise VintedCheckoutError(
            VintedCheckoutStatus.UNEXPECTED,
            "Réponse illisible de Vinted.",
        ) from exc
    if not isinstance(data, dict):
        raise VintedCheckoutError(
            VintedCheckoutStatus.UNEXPECTED,
            "Réponse inattendue de Vinted.",
        )
    return data


def _positive_int(value: object, *, what: str) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise VintedCheckoutError(
            VintedCheckoutStatus.UNEXPECTED,
            f"Identifiant {what} illisible chez Vinted.",
        ) from exc
    if number <= 0:
        raise VintedCheckoutError(
            VintedCheckoutStatus.UNEXPECTED,
            f"Identifiant {what} illisible chez Vinted.",
        )
    return number


def _non_empty_id(value: object, *, what: str) -> str:
    if value is None or str(value).strip() in ("", "0"):
        raise VintedCheckoutError(
            VintedCheckoutStatus.UNEXPECTED,
            f"Vinted n'a pas retourné d'identifiant {what}.",
        )
    return str(value)


def build_checkout_url_for(*, transaction_id: str, purchase_id: str) -> str:
    """URL de checkout ouverte par l'utilisateur dans sa propre session.

    Forme exacte du client web (`SINGLE_CHECKOUT_URL`) : `purchase_id` est le
    panier créé, `order_id` la transaction, `order_type` vaut `transaction`.
    """
    query = urlencode(
        {
            "purchase_id": purchase_id,
            "order_id": transaction_id,
            "order_type": "transaction",
        }
    )
    return f"{SITE_URL}/checkout?{query}"


class VintedCheckoutProvider:
    """Traduit le parcours « Acheter » du web Vinted en deux appels bornés."""

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.http_client = http_client
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.VINTED_CHECKOUT_TIMEOUT_SECONDS
        )

    def _client(self) -> httpx.AsyncClient:
        return self.http_client or get_shared_http_client()

    async def _post_json(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        cookies: Mapping[str, str],
    ) -> httpx.Response:
        headers = web_request_headers(cookies)
        try:
            return await self._client().post(
                url,
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise VintedCheckoutError(
                VintedCheckoutStatus.TIMEOUT,
                "Vinted n'a pas répondu à temps.",
            ) from exc
        except httpx.HTTPError as exc:
            raise VintedCheckoutError(
                VintedCheckoutStatus.NETWORK,
                "Connexion à Vinted impossible.",
            ) from exc

    async def create_buyer_inquiry(
        self,
        *,
        item_id: str,
        seller_id: str,
        cookies: Mapping[str, str],
    ) -> str:
        """Ouvre la transaction acheteur↔vendeur et rend son identifiant.

        Effet de bord assumé du parcours Vinted lui-même : un fil de
        discussion avec le vendeur est créé avant tout paiement.
        """
        payload = {
            "item_ids": [item_id],
            "receiver_id": _positive_int(seller_id, what="vendeur"),
        }
        response = await self._post_json(
            INQUIRIES_URL, payload=payload, cookies=cookies
        )
        if response.status_code == 404:
            # Le backend de messagerie du compte n'est pas celui-ci : le site
            # lui-même se rabat sur `/api/v2/conversations`. Un 404 est
            # définitif (rien n'a été créé), le repli ne duplique pas.
            return await self._create_legacy_conversation(
                item_id=item_id, seller_id=seller_id, cookies=cookies
            )
        _raise_for_response(response)
        data = _json_object(response)
        return _non_empty_id(data.get("transaction_id"), what="de transaction")

    async def _create_legacy_conversation(
        self,
        *,
        item_id: str,
        seller_id: str,
        cookies: Mapping[str, str],
    ) -> str:
        """Chemin de messagerie historique, avec l'intention d'achat du site."""
        payload = {
            "initiator": "buy",
            "item_id": _positive_int(item_id, what="d'article"),
            "opposite_user_id": _positive_int(seller_id, what="vendeur"),
        }
        response = await self._post_json(
            f"{API_BASE_URL}/conversations", payload=payload, cookies=cookies
        )
        _raise_for_response(response)
        data = _json_object(response)
        conversation = data.get("conversation")
        transaction = conversation.get("transaction") if isinstance(conversation, dict) else None
        transaction_id = transaction.get("id") if isinstance(transaction, dict) else None
        return _non_empty_id(transaction_id, what="de transaction")

    async def build_single_checkout(
        self,
        *,
        transaction_id: str,
        cookies: Mapping[str, str],
    ) -> str:
        """Transforme la transaction en panier et rend le `purchase_id`."""
        payload = {
            "purchase_items": [
                {
                    "id": _positive_int(transaction_id, what="de transaction"),
                    "type": "transaction",
                }
            ]
        }
        response = await self._post_json(
            f"{API_BASE_URL}/purchases/checkout/build",
            payload=payload,
            cookies=cookies,
        )
        _raise_for_response(response)
        data = _json_object(response)
        checkout = data.get("checkout")
        purchase_id = checkout.get("id") if isinstance(checkout, dict) else None
        return _non_empty_id(purchase_id, what="de checkout")

    async def fetch_item_seller_id(
        self,
        item_id: str,
        cookies: Mapping[str, str] | None = None,
    ) -> str | None:
        """Lit l'identifiant vendeur sur la page publique de l'annonce.

        Repli pour les annonces ingérées avant que la charge d'ingestion ne
        transporte l'identifiant. Une page servie sans `seller_id` rend `None` :
        l'appelant décide, ce module n'invente rien.

        La page est demandée **avec la session de compte** quand elle est
        fournie : une page personnalisée porte l'identifiant vendeur de façon
        plus fiable qu'une page anonyme, et son `seller_id` est celui du vendeur
        de l'article, pas d'une recommandation.
        """
        url = f"{SITE_URL}/items/{item_id}"
        headers = (
            web_request_headers(cookies) if cookies else dict(VINTED_WEB_HEADERS)
        )
        try:
            response = await self._client().get(
                url, headers=headers, timeout=self.timeout_seconds
            )
        except httpx.TimeoutException as exc:
            raise VintedCheckoutError(
                VintedCheckoutStatus.TIMEOUT,
                "Vinted n'a pas répondu à temps.",
            ) from exc
        except httpx.HTTPError as exc:
            raise VintedCheckoutError(
                VintedCheckoutStatus.NETWORK,
                "Connexion à Vinted impossible.",
            ) from exc
        if response.status_code == 404:
            raise VintedCheckoutError(
                VintedCheckoutStatus.UNAVAILABLE,
                "L'article n'est plus disponible.",
            )
        if response.status_code != 200:
            return None
        match = _SELLER_ID_PATTERN.search(response.text)
        return match.group(1) if match else None

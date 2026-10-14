"""Préparation du checkout Vinted au clic sur « Acheter ».

Le clic ne prépare rien à l'avance : aucun appel réseau n'existe sur le chemin
d'envoi de l'alerte (invariant n°1). Tout part d'ici, une seule fois par clic :

1. l'annonce est relue et **doit** être associée à une alerte de l'utilisateur
   Telegram qui agit ;
2. un lien déjà préparé récemment est réutilisé tel quel (aucun nouvel appel) ;
3. sinon, une transaction d'achat puis un panier sont créés chez Vinted avec la
   session de compte injectée, et le lien de checkout est renvoyé à l'utilisateur.

Concurrence : un verrou Redis court empêche deux clics rapprochés de lancer
deux opérations. Un échec incertain (délai dépassé, réseau coupé) garde le
verrou jusqu'à son expiration — le résultat peut exister côté Vinted, et une
relance immédiate pourrait dupliquer une conversation. Un échec définitif
(session invalide, article indisponible) libère le verrou : recliquer est
légitime.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis_client
from app.db.models.listing import Listing
from app.db.repositories.listing_repo import ListingRepository
from app.domain.enums import VintedCheckoutStatus
from app.providers.vinted_checkout import (
    SITE_URL,
    VintedCheckoutError,
    VintedCheckoutProvider,
    build_checkout_url_for,
    resolve_checkout_cookies,
    session_is_authenticated,
)

logger = logging.getLogger(__name__)

# Durée du verrou anti-double-clic. Plus court que le cache de lien : au-delà,
# un nouveau clic est une nouvelle tentative demandée par l'utilisateur.
LOCK_TTL_SECONDS = 45

_LINK_KEY = "radar:vinted_checkout:link:{user_id}:{listing_id}"
_LOCK_KEY = "radar:vinted_checkout:lock:{user_id}:{listing_id}"

# Une panne de transport laisse un résultat possible côté Vinted (le POST a pu
# aboutir sans que sa réponse nous parvienne) : on ne libère pas le verrou.
_UNCERTAIN_STATUSES = frozenset(
    {VintedCheckoutStatus.TIMEOUT, VintedCheckoutStatus.NETWORK}
)


@dataclass(frozen=True)
class CheckoutPreparation:
    """Ce que le handler peut dire et montrer après un clic sur « Acheter »."""

    status: VintedCheckoutStatus
    checkout_url: str | None = None
    listing_url: str | None = None
    listing_title: str | None = None


def _link_key(telegram_user_id: int, listing_id: uuid.UUID) -> str:
    return _LINK_KEY.format(user_id=telegram_user_id, listing_id=listing_id)


def _lock_key(telegram_user_id: int, listing_id: uuid.UUID) -> str:
    return _LOCK_KEY.format(user_id=telegram_user_id, listing_id=listing_id)


def _listing_identity(listing: Listing | None) -> tuple[str | None, str | None]:
    if listing is None:
        return None, None
    return listing.url, listing.title


async def _read_cached_link(key: str) -> str | None:
    """Lien déjà préparé, ou `None` — Redis muet ou contenu illisible."""
    try:
        raw = await get_redis_client().get(key)
    except Exception as exc:
        logger.warning("Cache de checkout Vinted illisible : %s", exc)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    url = data.get("url") if isinstance(data, dict) else None
    if isinstance(url, str) and url.startswith(f"{SITE_URL}/checkout?"):
        return url
    return None


async def _write_cached_link(key: str, url: str) -> None:
    try:
        await get_redis_client().set(
            key,
            json.dumps({"url": url}),
            ex=settings.VINTED_CHECKOUT_LINK_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("Cache de checkout Vinted non écrit : %s", exc)


async def _acquire_lock(key: str) -> bool:
    """Verrou court anti-double-clic ; Redis muet ne bloque pas l'utilisateur."""
    try:
        acquired = await get_redis_client().set(key, "1", ex=LOCK_TTL_SECONDS, nx=True)
    except Exception as exc:
        logger.warning("Verrou de checkout Vinted indisponible : %s", exc)
        return True
    return bool(acquired)


async def _release_lock(key: str) -> None:
    try:
        await get_redis_client().delete(key)
    except Exception as exc:
        logger.debug("Verrou de checkout Vinted non libéré : %s", exc)


def _seller_id_from_listing(listing: Listing) -> str:
    raw = listing.raw_payload if isinstance(listing.raw_payload, dict) else {}
    value = raw.get("seller_external_id")
    seller = str(value).strip() if value is not None else ""
    return seller if seller.isdigit() else ""


class PrepareVintedCheckoutUseCase:
    """Prépare le checkout d'une annonce à la demande, sans jamais payer."""

    def __init__(
        self,
        db: AsyncSession,
        provider: VintedCheckoutProvider | None = None,
    ) -> None:
        self.db = db
        self.provider = provider or VintedCheckoutProvider()

    async def execute(
        self,
        *,
        telegram_user_id: int,
        listing_id: uuid.UUID,
    ) -> CheckoutPreparation:
        if not settings.VINTED_CHECKOUT_PREPARATION_ENABLED:
            return CheckoutPreparation(VintedCheckoutStatus.DISABLED)

        listing = await ListingRepository(self.db).get_listing_matched_to_user(
            listing_id, telegram_user_id
        )
        if listing is None:
            return CheckoutPreparation(VintedCheckoutStatus.NOT_ASSOCIATED)
        listing_url, listing_title = _listing_identity(listing)

        def outcome(status: VintedCheckoutStatus, url: str | None = None) -> CheckoutPreparation:
            return CheckoutPreparation(
                status,
                checkout_url=url,
                listing_url=listing_url,
                listing_title=listing_title,
            )

        cached = await _read_cached_link(_link_key(telegram_user_id, listing_id))
        if cached is not None:
            return outcome(VintedCheckoutStatus.ALREADY_READY, cached)

        cookies = await resolve_checkout_cookies()
        if not cookies or not session_is_authenticated(cookies):
            return outcome(VintedCheckoutStatus.NOT_CONFIGURED)

        seller_id = _seller_id_from_listing(listing)
        if not seller_id:
            try:
                seller_id = await self.provider.fetch_item_seller_id(listing.external_id) or ""
            except VintedCheckoutError as exc:
                return outcome(exc.status)
        if not seller_id:
            return outcome(VintedCheckoutStatus.SELLER_UNKNOWN)

        lock_key = _lock_key(telegram_user_id, listing_id)
        if not await _acquire_lock(lock_key):
            return outcome(VintedCheckoutStatus.BUSY)

        try:
            transaction_id = await self.provider.create_buyer_inquiry(
                item_id=listing.external_id,
                seller_id=seller_id,
                cookies=cookies,
            )
            purchase_id = await self.provider.build_single_checkout(
                transaction_id=transaction_id,
                cookies=cookies,
            )
            checkout_url = build_checkout_url_for(
                transaction_id=transaction_id, purchase_id=purchase_id
            )
        except VintedCheckoutError as exc:
            if exc.status not in _UNCERTAIN_STATUSES:
                await _release_lock(lock_key)
            logger.info(
                "Préparation de checkout Vinted non aboutie (statut=%s).",
                exc.status.value,
            )
            return outcome(exc.status)
        except Exception:
            await _release_lock(lock_key)
            logger.exception("Préparation de checkout Vinted en échec inattendue.")
            return outcome(VintedCheckoutStatus.UNEXPECTED)

        await _write_cached_link(_link_key(telegram_user_id, listing_id), checkout_url)
        return outcome(VintedCheckoutStatus.READY, checkout_url)

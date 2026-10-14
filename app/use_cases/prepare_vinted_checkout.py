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
    CheckoutBackoff,
    VintedCheckoutError,
    VintedCheckoutProvider,
    build_checkout_url_for,
    resolve_checkout_cookies,
    session_is_authenticated,
)

logger = logging.getLogger(__name__)

# Durée du verrou d'exécution. Elle couvre le pire cas des deux appels sous
# verrou (transaction + panier), soit `2 × VINTED_CHECKOUT_TIMEOUT_SECONDS` ; un
# test verrouille cette relation. Ce n'est pas un garde-fou suffisant à lui seul :
# si le verrou expire pendant qu'une opération est en vol, un autre clic peut
# entrer (voir `_acquire_lock` — le contrôle du coupe-circuit dans le verrou est
# la seconde barrière).
LOCK_TTL_SECONDS = 45

# Le lien préparé est **par annonce** (on le réutilise tel quel) ; le verrou
# d'exécution est **global** : une seule création de checkout à la fois, quel que
# soit le nombre d'annonces cliquées. Deux clics sur deux annonces ne peuvent donc
# pas produire deux transactions **tant que le verrou vit**.
_LINK_KEY = "radar:vinted_checkout:link:{user_id}:{listing_id}"
_LOCK_KEY = "radar:vinted_checkout:lock"


@dataclass(frozen=True)
class CheckoutPreparation:
    """Ce que le handler peut dire et montrer après un clic sur « Acheter »."""

    status: VintedCheckoutStatus
    checkout_url: str | None = None
    listing_url: str | None = None
    listing_title: str | None = None
    # Renseigné quand la création est coupée : le handler peut annoncer le délai.
    retry_after_seconds: int | None = None


def _link_key(telegram_user_id: int, listing_id: uuid.UUID) -> str:
    return _LINK_KEY.format(user_id=telegram_user_id, listing_id=listing_id)


def _lock_key() -> str:
    """Clé du verrou **global** : une seule création de checkout à la fois."""
    return _LOCK_KEY


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
        backoff: CheckoutBackoff | None = None,
    ) -> None:
        self.db = db
        self.provider = provider or VintedCheckoutProvider()
        self.backoff = backoff or CheckoutBackoff()

    async def execute(
        self,
        *,
        telegram_user_id: int,
        listing_id: uuid.UUID,
    ) -> CheckoutPreparation:
        # Les prérequis les moins chers d'abord : rien ne touche la base tant que
        # la fonctionnalité est coupée, la session absente ou la création coupée.
        # Chaque refus évite une requête SQL inutile sur le clic.
        if not settings.VINTED_CHECKOUT_PREPARATION_ENABLED:
            return CheckoutPreparation(VintedCheckoutStatus.DISABLED)

        cookies = await resolve_checkout_cookies()
        if not cookies or not session_is_authenticated(cookies):
            return CheckoutPreparation(VintedCheckoutStatus.NOT_CONFIGURED)

        pause = await self.backoff.remaining_seconds()
        if pause > 0:
            return CheckoutPreparation(
                VintedCheckoutStatus.PAUSED, retry_after_seconds=pause
            )

        listing = await ListingRepository(self.db).get_listing_matched_to_user(
            listing_id, telegram_user_id
        )
        if listing is None:
            return CheckoutPreparation(VintedCheckoutStatus.NOT_ASSOCIATED)
        listing_url, listing_title = _listing_identity(listing)

        def outcome(
            status: VintedCheckoutStatus,
            url: str | None = None,
            *,
            retry_after: int | None = None,
        ) -> CheckoutPreparation:
            return CheckoutPreparation(
                status,
                checkout_url=url,
                listing_url=listing_url,
                listing_title=listing_title,
                retry_after_seconds=retry_after,
            )

        cached = await _read_cached_link(_link_key(telegram_user_id, listing_id))
        if cached is not None:
            return outcome(VintedCheckoutStatus.ALREADY_READY, cached)

        seller_id = _seller_id_from_listing(listing)
        if not seller_id:
            try:
                seller_id = (
                    await self.provider.fetch_item_seller_id(
                        listing.external_id, cookies=cookies
                    )
                    or ""
                )
            except VintedCheckoutError as exc:
                seconds = await self.backoff.request_failure(exc.status)
                return outcome(exc.status, retry_after=seconds)
        if not seller_id:
            # Échec sans appel de création : la coupure s'applique quand même,
            # pour ne pas relancer une résolution qui vient d'échouer.
            seconds = await self.backoff.request_failure(VintedCheckoutStatus.SELLER_UNKNOWN)
            return outcome(VintedCheckoutStatus.SELLER_UNKNOWN, retry_after=seconds)

        lock_key = _lock_key()
        if not await _acquire_lock(lock_key):
            return outcome(VintedCheckoutStatus.BUSY)

        # Seconde barrière : le clic a gagné le verrou **après** l'échec d'un
        # précédent (ou pendant que sa coupure venait d'être posée). Le contrôle
        # est refait à l'intérieur du verrou, là où il est sûr — sinon « une
        # exécution à la fois » se lirait à tort comme « jamais de doublon ».
        seconds_in_lock = await self.backoff.remaining_seconds()
        if seconds_in_lock > 0:
            await _release_lock(lock_key)
            return outcome(VintedCheckoutStatus.PAUSED, retry_after=seconds_in_lock)

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
            # Coupe **avant** libération : il n'existe aucun instant où le verrou
            # est libre et la coupure absente (un clic concurrent sur ce point
            # relancerait une opération dont le résultat est déjà incertain).
            seconds = await self.backoff.request_failure(exc.status)
            await _release_lock(lock_key)
            logger.info(
                "Préparation de checkout Vinted non aboutie (statut=%s).",
                exc.status.value,
            )
            return outcome(exc.status, retry_after=seconds)
        except Exception:
            seconds = await self.backoff.request_failure(VintedCheckoutStatus.UNEXPECTED)
            await _release_lock(lock_key)
            logger.exception("Préparation de checkout Vinted en échec inattendue.")
            return outcome(VintedCheckoutStatus.UNEXPECTED, retry_after=seconds)

        await _write_cached_link(_link_key(telegram_user_id, listing_id), checkout_url)
        return outcome(VintedCheckoutStatus.READY, checkout_url)

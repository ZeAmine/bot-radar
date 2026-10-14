"""Endpoints internes sécurisés (dashboard et consultation du cache)."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_internal_token
from app.core.config import settings
from app.core.redis import get_redis_client
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.providers.chasse_aux_livres import REDIS_SESSION_KEY, resolve_session_token
from app.providers.vinted_checkout import (
    clear_checkout_cookies,
    resolve_checkout_cookies,
    session_is_authenticated,
    store_checkout_cookies,
)
from app.services.vinted_scraper import vinted_session_manager

router = APIRouter(
    prefix="/api/v1/internal",
    tags=["Internal"],
    dependencies=[Depends(require_internal_token)],
)


@router.get("/alerts")
async def list_internal_alerts(
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Retourne la liste des alertes sans secret."""
    alerts = await AlertRepository(db).list_not_deleted()

    return [
        {
            "id": str(a.id),
            "name": a.name,
            "category": a.category.slug if a.category else None,
            "status": a.status.value,
            "vinted_search_url": a.vinted_search_url,
            "max_item_price_cents": a.max_item_price_cents,
            "include_keywords": a.include_keywords,
            "exclude_keywords": a.exclude_keywords,
            "variants": a.variants,
            "accepted_conditions": a.accepted_conditions,
            "created_at": a.created_at.isoformat(),
        }
        for a in alerts
    ]


@router.get("/books/{isbn13}/market-price")
async def get_book_market_price(
    isbn13: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Retourne le cache de prix marché d'un livre catalogué."""
    book_repo = BookRepository(db)
    book = await book_repo.get_by_isbn13(isbn13)
    if not book or not book.market_cache:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Livre ou cache de prix introuvable.",
        )

    cache = book.market_cache
    return {
        "isbn13": book.isbn13,
        "title": book.title,
        "lowest_delivered_price_cents": cache.lowest_delivered_price_cents,
        "best_provider_name": cache.best_provider_name,
        "best_source_url": cache.best_source_url,
        "confidence": cache.confidence.value if cache.confidence else None,
        "comparison_status": cache.comparison_status.value,
        "checked_at": cache.checked_at.isoformat() if cache.checked_at else None,
        "expires_at": cache.expires_at.isoformat() if cache.expires_at else None,
        "historical_low_1y_cents": cache.historical_low_1y_cents,
        "historical_low_at": (
            cache.historical_low_at.isoformat()
            if cache.historical_low_at
            else None
        ),
    }


@router.post("/vinted/session")
async def update_vinted_session(
    payload: dict[str, str],
) -> dict[str, Any]:
    """Injecte manuellement des cookies de session Vinted valides dans Redis."""
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le dictionnaire de cookies ne peut pas être vide.",
        )
    await vinted_session_manager.set_manual_session(payload)
    return {
        "status": "success",
        "cookies_stored": len(payload),
        "keys": list(payload.keys()),
    }


@router.get("/chasse/session")
async def get_chasse_session_status() -> dict[str, Any]:
    """État du jeton Chasse-aux-livres, sans la valeur elle-même.

    `source` dit d'où vient le jeton *effectivement utilisé*, pas d'où il pourrait
    venir : aucun jeton se lit `None`, jamais « redis ». Dire « redis » pour une
    session absente enverrait chercher une clé qui n'existe pas.
    """
    token = await resolve_session_token()
    if settings.CHASSE_AUX_LIVRES_DUIH:
        source: str | None = "env"
    elif token:
        source = "redis"
    else:
        source = None
    return {
        "has_token": bool(token),
        "token_length": len(token),
        "source": source,
    }


@router.post("/chasse/session")
async def update_chasse_session(payload: dict[str, str]) -> dict[str, Any]:
    """Injecte le jeton anti-bot Chasse-aux-livres, après validation hors de prod.

    Le jeton n'est jamais renvoyé : sa longueur suffit à distinguer « vide » de
    « installé », sans jamais le laisser fuir dans un log.
    """
    token = (payload.get("duih") or "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le jeton ne peut pas être vide.",
        )
    await get_redis_client().set(REDIS_SESSION_KEY, token)
    return {"status": "success", "token_length": len(token)}


@router.get("/vinted/session")
async def get_vinted_session_status() -> dict[str, Any]:
    """Retourne l'état actuel de la session Vinted sans exposer les jetons sensibles."""
    cookies = vinted_session_manager.cookies
    if not cookies:
        cookies = await vinted_session_manager.load_cookies_from_redis()
    return {
        "active_cookies_count": len(cookies),
        "cookie_names": list(cookies.keys()),
        "has_access_token": bool(cookies.get("access_token_web")),
        "last_refreshed": vinted_session_manager.last_refreshed,
    }


@router.get("/vinted/checkout-session")
async def get_vinted_checkout_session_status() -> dict[str, Any]:
    """État de la session de compte utilisée par le bouton « Acheter ».

    Une session séparée de celle du scraper : ses cookies de compte ne sont
    jamais mélangés avec les cookies anonymes du poller. Aucune valeur n'est
    exposée — des noms et des faits suffisent à dire « installée » ou « à
    réinjecter ».
    """
    cookies = await resolve_checkout_cookies()
    return {
        "configured": bool(cookies),
        "authenticated": session_is_authenticated(cookies),
        "cookie_names": sorted(cookies),
        "has_access_token": bool(cookies.get("access_token_web")),
    }


@router.post("/vinted/checkout-session")
async def update_vinted_checkout_session(payload: dict[str, str]) -> dict[str, Any]:
    """Injecte la session de compte capturée dans le navigateur.

    Le script local `scripts/sync_vinted_checkout_session.py` valide la session
    contre Vinted avant d'appeler cette route ; celle-ci ne fait que la
    persister, et ne renvoie jamais une valeur de cookie.
    """
    cookies = {
        str(key): str(value).strip()
        for key, value in payload.items()
        if str(value).strip()
    }
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le dictionnaire de cookies ne peut pas être vide.",
        )
    stored = await store_checkout_cookies(cookies)
    return {
        "status": "success",
        "cookies_stored": stored,
        "cookie_names": sorted(cookies),
    }


@router.delete("/vinted/checkout-session")
async def clear_vinted_checkout_session() -> dict[str, Any]:
    """Purge la session de compte, sur geste explicite uniquement."""
    await clear_checkout_cookies()
    return {"status": "success"}


"""Tâche planifiée de rafraîchissement des prix de marché des BD et comics."""

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ComparisonStatus
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.providers.registry import ProviderRegistry
from app.utils.isbn import isbn10_to_isbn13

logger = logging.getLogger(__name__)


async def refresh_book_prices_task(
    registry: ProviderRegistry | None = None,
    session: AsyncSession | None = None,
    incremental: bool = False,
    batch_size: int = 8,
) -> dict[str, int]:
    """Rafraîchit les prix de marché pour les livres associés à des alertes BD actives.

    Args:
        registry: Registre des fournisseurs de marché (par défaut: tous les providers).
        session: Session SQLAlchemy optionnelle (utilisée principalement par les tests).
        incremental: Si True, traite uniquement un micro-lot prioritaire (cache expiré ou absent)
                     afin de lisser la charge 24h/24 sans aucun pic de performance.
        batch_size: Taille du lot en mode incrémental (défaut: 8 livres).
    """
    if registry is None:
        registry = ProviderRegistry()

    stats = {"processed": 0, "available": 0, "unavailable": 0, "errors": 0}

    # 1. Sélection des livres cibles
    if session is not None:
        # Mode session injectée (tests unitaires)
        book_repo = BookRepository(session)
        if incremental:
            books = await book_repo.get_books_needing_price_refresh(limit=batch_size)
        else:
            books = await book_repo.get_books_for_active_bd_alerts()
    else:
        # Mode autonome (production) : session dédiée et fermée immédiatement après lecture
        async with AsyncSessionLocal() as read_session:
            book_repo = BookRepository(read_session)
            if incremental:
                books = await book_repo.get_books_needing_price_refresh(limit=batch_size)
            else:
                books = await book_repo.get_books_for_active_bd_alerts()

    if not books:
        logger.debug("Aucun livre BD à rafraîchir pour cette passe.")
        return stats

    mode_label = (
        f"incrémental ({len(books)} livres)"
        if incremental
        else f"complet ({len(books)} livres)"
    )
    logger.info("Début du rafraîchissement des prix BD en mode %s.", mode_label)

    # 2. Traitement unitaire avec isolation stricte des sessions par livre
    for book in books:
        book_id = book.id
        book_title = book.title
        raw_isbn = book.isbn13.strip() if book.isbn13 else None
        clean_isbn = isbn10_to_isbn13(raw_isbn) if (raw_isbn and len(raw_isbn) == 10) else raw_isbn

        stats["processed"] += 1
        try:
            # Appel réseau protégé par timeout
            offers = await asyncio.wait_for(
                registry.fetch_all_offers(
                    isbn13=clean_isbn,
                    title=book.title,
                    author=book.author,
                    edition=book.edition,
                ),
                timeout=20.0,
            )

            # Minimum historique 1 an certifié présent dans les offres
            hist_lows = [
                o.raw_payload.get("historical_low_1y_cents")
                for o in offers
                if o.raw_payload and o.raw_payload.get("historical_low_1y_cents") is not None
            ]
            best_hist_low = min(hist_lows) if hist_lows else None

            # Meilleure offre d'occasion disponible
            best_offer = registry.select_best_offer(offers)

            # Persistance dans une session DB isolée
            if session is not None:
                # Mode test avec session partagée
                market_repo = MarketPriceRepository(session)
                for offer in offers:
                    await market_repo.save_snapshot(book_id, offer)

                if best_offer:
                    await market_repo.update_or_create_cache(
                        book_id=book_id,
                        lowest_delivered_price_cents=best_offer.delivered_price_cents,
                        best_provider_name=(
                            best_offer.raw_payload.get("best_merchant")
                            if best_offer.raw_payload
                            else None
                        )
                        or best_offer.provider_name,
                        best_source_url=best_offer.source_url,
                        confidence=best_offer.confidence,
                        comparison_status=ComparisonStatus.AVAILABLE,
                        cache_duration_hours=24,
                        historical_low_1y_cents=best_hist_low,
                    )
                    stats["available"] += 1
                elif best_hist_low is not None:
                    await market_repo.update_or_create_cache(
                        book_id=book_id,
                        lowest_delivered_price_cents=None,
                        best_provider_name=None,
                        best_source_url=None,
                        confidence=None,
                        comparison_status=ComparisonStatus.UNAVAILABLE,
                        cache_duration_hours=24,
                        historical_low_1y_cents=best_hist_low,
                    )
                    stats["available"] += 1
                else:
                    await market_repo.update_or_create_cache(
                        book_id=book_id,
                        lowest_delivered_price_cents=None,
                        best_provider_name=None,
                        best_source_url=None,
                        confidence=None,
                        comparison_status=ComparisonStatus.UNAVAILABLE,
                        cache_duration_hours=6,
                    )
                    stats["unavailable"] += 1
            else:
                # Mode production : session unitaire courte et hermétique
                async with AsyncSessionLocal() as book_session:
                    market_repo = MarketPriceRepository(book_session)
                    for offer in offers:
                        await market_repo.save_snapshot(book_id, offer)

                    existing_cache = await market_repo.get_cache_by_book_id(book_id)
                    now_utc = datetime.now(UTC)

                    if best_offer:
                        await market_repo.update_or_create_cache(
                            book_id=book_id,
                            lowest_delivered_price_cents=best_offer.delivered_price_cents,
                            best_provider_name=(
                                best_offer.raw_payload.get("best_merchant")
                                if best_offer.raw_payload
                                else None
                            )
                            or best_offer.provider_name,
                            best_source_url=best_offer.source_url,
                            confidence=best_offer.confidence,
                            comparison_status=ComparisonStatus.AVAILABLE,
                            cache_duration_hours=24,
                            historical_low_1y_cents=best_hist_low,
                        )
                        stats["available"] += 1
                    elif best_hist_low is not None:
                        # Pas d'offre en stock actuellement, mais historique 1 an certifié
                        await market_repo.update_or_create_cache(
                            book_id=book_id,
                            lowest_delivered_price_cents=None,
                            best_provider_name=None,
                            best_source_url=None,
                            confidence=None,
                            comparison_status=ComparisonStatus.UNAVAILABLE,
                            cache_duration_hours=24,
                            historical_low_1y_cents=best_hist_low,
                        )
                        stats["available"] += 1
                    else:
                        # Aucune offre trouvée lors de cet appel
                        # RÈGLE DE RÉSILIENCE : Si un prix valide existe déjà en cache, le préserver
                        # pour éviter de priver l'utilisateur de prix suite
                        # à un timeout réseau temporaire
                        if (
                            existing_cache
                            and existing_cache.lowest_delivered_price_cents is not None
                            and existing_cache.comparison_status == ComparisonStatus.AVAILABLE
                        ):
                            logger.info(
                                "Maintien résilient du prix en cache pour '%s' (%s cents).",
                                book_title,
                                existing_cache.lowest_delivered_price_cents,
                            )
                            existing_cache.checked_at = now_utc
                            existing_cache.expires_at = now_utc + timedelta(hours=6)
                            stats["available"] += 1
                        else:
                            await market_repo.update_or_create_cache(
                                book_id=book_id,
                                lowest_delivered_price_cents=None,
                                best_provider_name=None,
                                best_source_url=None,
                                confidence=None,
                                comparison_status=ComparisonStatus.UNAVAILABLE,
                                cache_duration_hours=6,
                            )
                            stats["unavailable"] += 1

                    await book_session.commit()

            # Pause aléatoire pour respecter les quotas partenaires et éviter le rate-limiting
            delay = random.uniform(1.2, 2.2) if incremental else random.uniform(2.5, 3.8)
            await asyncio.sleep(delay)

        except Exception as exc:
            logger.error(
                "Erreur lors du rafraîchissement pour le livre ID %s (%s): %s",
                book_id,
                book_title,
                exc,
            )
            stats["errors"] += 1

    logger.info(
        "Rafraîchissement BD terminé (%s) : %d traités, %d avec prix, "
        "%d indisponibles, %d erreurs.",
        mode_label,
        stats["processed"],
        stats["available"],
        stats["unavailable"],
        stats["errors"],
    )
    return stats

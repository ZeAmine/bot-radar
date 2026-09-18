"""Script d'enrichissement et d'association du catalogue BD pour toutes les alertes actives.

Pour chaque alerte BD :
1. Recherche l'ouvrage sur Chasse-aux-livres par son nom d'alerte canonique.
2. Crée ou met à jour la fiche du livre dans la table `books` (titre officiel, ISBN-13, auteur).
3. Associe l'alerte au livre dans la table `alert_books`.
4. Enregistre le snapshot de prix et initialise le cache `book_market_cache` avec le prix d'occasion
   actuel et le prix plancher historique initial.
"""

import asyncio
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.constants import AlertStatus, CategorySlug, ComparisonStatus, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.services.book_catalog_service import BookCatalogService
from app.services.matcher_service import normalize_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.enrich_catalog")


def clean_query_term(term: str) -> str:
    """Nettoie une chaîne pour optimiser la recherche sur les moteurs de livres."""
    cleaned = re.sub(r"[’'\"/&+\-_–—:]", " ", term)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def get_search_queries(alert_name: str) -> list[str]:
    """Génère les termes de recherche ordonnés pour maximiser les correspondances."""
    queries: list[str] = []

    parts = re.split(r"\s*[—–]\s*", alert_name)
    title_part = parts[0]
    author_part = parts[1] if len(parts) > 1 else ""

    clean_full = clean_query_term(alert_name)
    clean_title = clean_query_term(title_part)

    # 1. Titre + Premier nom d'auteur si présent (très sélectif)
    if author_part:
        author_clean = clean_query_term(author_part)
        author_first = " ".join(author_clean.split()[:2])
        comb = f"{clean_title} {author_first}".strip()
        if comb and comb not in queries:
            queries.append(comb)

    # 2. Titre seul ("Watchmen", "Tyler Cross", "These Savage Shores")
    if clean_title and clean_title not in queries:
        queries.append(clean_title)

    # 3. Nom complet nettoyé
    if clean_full and clean_full not in queries:
        queries.append(clean_full)

    return queries


async def enrich_bd_catalog(limit: int | None = None) -> dict[str, int]:
    """Parcourt toutes les alertes BD actives et enrichit leur livre et cache de marché."""
    chasse_provider = ChasseAuxLivresProvider(timeout_seconds=7.0)

    stats = {
        "total_alerts": 0,
        "already_linked": 0,
        "enriched_with_price": 0,
        "enriched_without_price": 0,
        "errors": 0,
    }

    async with AsyncSessionLocal() as session:
        book_repo = BookRepository(session)
        market_repo = MarketPriceRepository(session)
        catalog_service = BookCatalogService(session)

        # 1. Charger toutes les alertes BD actives
        stmt = (
            select(Alert)
            .join(Category, Alert.category_id == Category.id)
            .options(selectinload(Alert.alert_books))
            .where(
                Category.slug == CategorySlug.BD_COMICS.value,
                Alert.status == AlertStatus.ACTIVE,
            )
            .order_by(Alert.name.asc())
        )
        if limit:
            stmt = stmt.limit(limit)

        result = await session.execute(stmt)
        alerts = list(result.scalars().all())
        stats["total_alerts"] = len(alerts)
        logger.info("Début de l'enrichissement pour %d alertes BD...", len(alerts))

        for idx, alert in enumerate(alerts, 1):
            logger.info("[%d/%d] Alerte : '%s'", idx, len(alerts), alert.name)

            # Vérifier si l'alerte a déjà un livre lié avec un cache valide et un prix
            existing_book = await book_repo.get_book_for_alert(alert.id)
            if (
                existing_book
                and existing_book.market_cache
                and existing_book.market_cache.lowest_delivered_price_cents
            ):
                logger.info(
                    "  -> Déjà lié à '%s' avec cache valide (%.2f €)",
                    existing_book.title,
                    existing_book.market_cache.lowest_delivered_price_cents / 100,
                )
                stats["already_linked"] += 1
                continue

            search_queries = get_search_queries(alert.name)
            offers = []
            matched_query = ""

            try:
                for q in search_queries:
                    offers = await chasse_provider.get_offers(isbn13=None, title=q)
                    await asyncio.sleep(2.5)
                    if offers:
                        matched_query = q
                        break

                if offers:
                    best = offers[0]
                    payload = best.raw_payload or {}
                    found_isbn = payload.get("isbn13")
                    found_title = payload.get("title") or alert.name

                    # Trouver ou créer le Book
                    book = None
                    if found_isbn:
                        book = await book_repo.get_by_isbn13(found_isbn)
                    if not book:
                        norm = normalize_text(found_title)
                        cand = await book_repo.get_by_normalized_title(norm)
                        if cand:
                            book = cand[0]
                    if not book:
                        book = await catalog_service.add_book(
                            title=found_title,
                            isbn13=found_isbn,
                        )
                    elif found_isbn and not book.isbn13:
                        book.isbn13 = found_isbn

                    # Lier l'alerte au livre
                    await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)

                    # Enregistrer le snapshot et mettre à jour le cache
                    await market_repo.save_snapshot(book.id, best)
                    await market_repo.update_or_create_cache(
                        book_id=book.id,
                        lowest_delivered_price_cents=best.delivered_price_cents,
                        best_provider_name=best.provider_name,
                        best_source_url=best.source_url,
                        confidence=best.confidence,
                        comparison_status=ComparisonStatus.AVAILABLE,
                        cache_duration_hours=24,
                    )
                    stats["enriched_with_price"] += 1
                    logger.info(
                        "  ✅ Lié à '%s' (ISBN: %s) — Occasion : %.2f € (via '%s')",
                        book.title,
                        book.isbn13 or "N/A",
                        best.delivered_price_cents / 100,
                        matched_query,
                    )
                else:
                    # Associer un livre par défaut pour que l'alerte soit reliée
                    norm = normalize_text(alert.name)
                    cand = await book_repo.get_by_normalized_title(norm)
                    book = cand[0] if cand else await catalog_service.add_book(title=alert.name)
                    await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)
                    stats["enriched_without_price"] += 1
                    logger.warning(
                        "  ⚠️ Aucune offre trouvée sur Chasse-aux-livres pour '%s'", alert.name
                    )

                await session.commit()

            except Exception as exc:
                await session.rollback()
                logger.error("  ❌ Erreur lors de l'enrichissement pour '%s': %s", alert.name, exc)
                stats["errors"] += 1

        logger.info(
            "Enrichissement terminé : %d total, %d avec prix, %d sans prix, %d déjà liés, %d erreurs.",
            stats["total_alerts"],
            stats["enriched_with_price"],
            stats["enriched_without_price"],
            stats["already_linked"],
            stats["errors"],
        )
        return stats


if __name__ == "__main__":
    asyncio.run(enrich_bd_catalog())

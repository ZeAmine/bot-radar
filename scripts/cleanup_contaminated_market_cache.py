"""Script d'assainissement de la base de données :
1. Décontamination de l'historique bas (historical_low_1y_cents) sur tous les livres.
2. Exclusion stricte d'Amazon (De) et réassignation vers le meilleur marchand francophone.
3. Actualisation immédiate de la fiche de « 1984 » (9782377316588).
"""

import asyncio
import logging
import sys

from sqlalchemy import select

from app.core.constants import ComparisonStatus
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.services.market_price_service import (
    is_brand_new_partner,
    is_merchant_excluded,
    is_very_good_or_better,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cleanup_market_cache")


async def cleanup_database() -> None:
    logger.info("Démarrage de l'assainissement du cache de marché...")

    async with AsyncSessionLocal() as session:
        # 1. Parcourir tous les caches existants
        stmt = select(BookMarketCache, Book).join(Book, BookMarketCache.book_id == Book.id)
        result = await session.execute(stmt)
        rows = result.all()

        logger.info("Vérification de %d entrées de cache...", len(rows))

        updated_count = 0

        for cache, book in rows:
            changed = False
            old_hist = cache.historical_low_1y_cents
            old_best = cache.best_provider_name
            old_price = cache.lowest_delivered_price_cents

            # Récupérer les snapshots pour ce livre
            snap_stmt = (
                select(MarketPriceSnapshot)
                .where(MarketPriceSnapshot.book_id == book.id)
                .order_by(MarketPriceSnapshot.checked_at.desc())
            )
            snap_res = await session.execute(snap_stmt)
            snapshots = snap_res.scalars().all()

            # A. Extraire le véritable historique certifié 1 an depuis les snapshots
            certified_hist_lows: list[int] = []
            for snap in snapshots:
                if snap.raw_payload and isinstance(snap.raw_payload, dict):
                    h = snap.raw_payload.get("historical_low_1y_cents")
                    if h is not None and isinstance(h, (int, float)) and h > 0:
                        certified_hist_lows.append(int(h))

            true_hist_low = min(certified_hist_lows) if certified_hist_lows else None

            # Si l'historique en cache ne correspond pas au plus bas certifié
            if cache.historical_low_1y_cents != true_hist_low:
                cache.historical_low_1y_cents = true_hist_low
                if true_hist_low is None:
                    cache.historical_low_at = None
                changed = True

            # B. Vérifier si le meilleur marchand était Amazon (De)
            if is_merchant_excluded(cache.best_provider_name):
                logger.info(
                    "Marchand exclu '%s' détecté pour '%s' (ISBN: %s). Recherche d'une alternative...",
                    cache.best_provider_name,
                    book.title,
                    book.isbn13,
                )
                alternative_found = False
                for snap in snapshots:
                    if snap.raw_payload and isinstance(snap.raw_payload, dict):
                        partners = snap.raw_payload.get("partners")
                        if partners and isinstance(partners, dict):
                            valid_alts: list[dict] = []
                            for p_name, p_data in partners.items():
                                if (
                                    isinstance(p_data, dict)
                                    and not is_merchant_excluded(p_name)
                                    and not is_merchant_excluded(p_data.get("merchant"))
                                    and not is_brand_new_partner(p_data)
                                    and is_very_good_or_better(p_data.get("condition"))
                                    and p_data.get("total_cents", 0) > 0
                                ):
                                    valid_alts.append(p_data)

                            if valid_alts:
                                valid_alts.sort(key=lambda x: x["total_cents"])
                                best_alt = valid_alts[0]
                                cache.best_provider_name = best_alt.get("merchant", "Partenaire")
                                cache.lowest_delivered_price_cents = best_alt["total_cents"]
                                if best_alt.get("url"):
                                    cache.best_source_url = best_alt["url"]
                                cache.comparison_status = ComparisonStatus.AVAILABLE
                                alternative_found = True
                                changed = True
                                break

                if not alternative_found:
                    logger.warning(
                        "Aucune alternative trouvée pour '%s' hors marchands exclus.", book.title
                    )
                    cache.lowest_delivered_price_cents = None
                    cache.comparison_status = ComparisonStatus.UNAVAILABLE
                    changed = True

            if changed:
                updated_count += 1
                logger.info(
                    "Assaini '%s': HistLow %s -> %s | Marchand %s -> %s | Prix %s -> %s",
                    book.title,
                    f"{old_hist/100:.2f}€" if old_hist else "None",
                    f"{cache.historical_low_1y_cents/100:.2f}€"
                    if cache.historical_low_1y_cents
                    else "None",
                    old_best,
                    cache.best_provider_name,
                    f"{old_price/100:.2f}€" if old_price else "None",
                    f"{cache.lowest_delivered_price_cents/100:.2f}€"
                    if cache.lowest_delivered_price_cents
                    else "None",
                )

        await session.commit()
        logger.info(
            "Fin de la passe d'assainissement : %d/%d caches corrigés.", updated_count, len(rows)
        )

        # 2. Rafraîchissement direct de « 1984 »
        logger.info("Actualisation directe de '1984' (9782377316588)...")
        b_stmt = select(Book).where(Book.isbn13 == "9782377316588")
        b_res = await session.execute(b_stmt)
        b_1984 = b_res.scalar_one_or_none()
        if b_1984:
            chasse = ChasseAuxLivresProvider(timeout_seconds=10.0)
            offers = await chasse.get_offers(isbn13=b_1984.isbn13, title="")
            if offers:
                best = offers[0]
                repo = MarketPriceRepository(session)
                await repo.save_snapshot(b_1984.id, best)
                hist_1y = (
                    best.raw_payload.get("historical_low_1y_cents") if best.raw_payload else None
                )
                best_m = (
                    best.raw_payload.get("best_merchant") or best.provider_name
                    if best.raw_payload
                    else best.provider_name
                )
                new_cache = await repo.update_or_create_cache(
                    book_id=b_1984.id,
                    lowest_delivered_price_cents=best.delivered_price_cents,
                    best_provider_name=best_m,
                    best_source_url=best.source_url,
                    confidence=best.confidence,
                    comparison_status=ComparisonStatus.AVAILABLE,
                    cache_duration_hours=24,
                    historical_low_1y_cents=hist_1y,
                )
                await session.commit()
                logger.info(
                    "1984 actualisé avec succès : Prix=%s (%s), HistLow=%s, Statut=%s",
                    f"{new_cache.lowest_delivered_price_cents/100:.2f}€",
                    new_cache.best_provider_name,
                    f"{new_cache.historical_low_1y_cents/100:.2f}€"
                    if new_cache.historical_low_1y_cents
                    else "None",
                    new_cache.comparison_status.value,
                )
            else:
                logger.warning("Aucune offre reçue pour 1984 lors du rafraîchissement.")
        else:
            logger.info("Livre 1984 non trouvé par son ISBN dans cette base.")


if __name__ == "__main__":
    asyncio.run(cleanup_database())

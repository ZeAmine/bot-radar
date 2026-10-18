import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import pilot_mode
from app.db.repositories.book_repo import BookRepository
from app.services.market_refresh_policy import collection_due


def manifest_digest(manifest: dict[str, Any]) -> str:
    content = {key: value for key, value in manifest.items() if key != "digest"}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


async def inventory_market_backfill(session: AsyncSession) -> dict[str, Any]:
    from app.db.models.book_market_cache import BookMarketCache
    from app.db.repositories.market_price_repo import MarketPriceRepository

    rows = await MarketPriceRepository(session).market_backfill_rows()
    now = datetime.now(UTC)
    books: dict[str, Any] = {}
    alerts: dict[str, Any] = {}
    for row in rows:
        alert = alerts.setdefault(str(row["alert_id"]), {
            "alert_id": str(row["alert_id"]), "name": row["alert_name"],
            "isbn": row["alert_isbn"], "book_ids": [],
            "diagnostic": "catalogue_missing", "mismatched_book_ids": [],
        })
        if row["book_id"] is None:
            continue
        key = str(row["book_id"])
        alert["book_ids"].append(key)
        if row["alert_isbn"] and row["isbn13"] != row["alert_isbn"]:
            alert["mismatched_book_ids"].append(key)
            alert["diagnostic"] = "identity_mismatch"
        elif alert["diagnostic"] != "identity_mismatch":
            alert["diagnostic"] = "linked"
        cache = BookMarketCache(**{name.removeprefix("cache_"): value
                                   for name, value in row.items() if name.startswith("cache_")})
        books[key] = {
            "book_id": key, "isbn13": row["isbn13"], "title": row["title"],
            "language": row["language"], "edition": row["edition"],
            "offers_succeeded_at": (cache.last_successful_collection_at.isoformat()
                                    if cache.last_successful_collection_at else None),
            "history_succeeded_at": (cache.historical_checked_at.isoformat()
                                     if cache.source_history and cache.historical_checked_at
                                     else None),
            "source_history_verified": bool(cache.source_history),
            "due": collection_due(cache, now),
        }
    from app.utils.isbn import normalize_listing_isbn

    equivalent: dict[str, list[dict[str, Any]]] = {}
    for book in books.values():
        code = normalize_listing_isbn(book["isbn13"])
        if code:
            equivalent.setdefault(code, []).append(book)
    aliases = []
    for code, group in equivalent.items():
        canonical = next((book for book in group if book["isbn13"] == code), None)
        if canonical is not None and len(group) > 1:
            aliases.append({"isbn13": code, "canonical_book_id": canonical["book_id"],
                            "alias_book_ids": [book["book_id"] for book in group
                                               if book["book_id"] != canonical["book_id"]]})
    manifest: dict[str, Any] = {"version": 1, "created_at": now.isoformat(),
                                "alerts": list(alerts.values()), "books": list(books.values()),
                                "isbn_aliases": aliases}
    manifest["digest"] = manifest_digest(manifest)
    return manifest


async def apply_catalogue_repair(session: AsyncSession, repair: dict[str, Any]) -> uuid.UUID | None:
    from app.core.constants import AlertStatus
    from app.db.repositories.alert_repo import AlertRepository, BibliographicSnapshot
    from app.services.book_market_pricing import recalculate_invalidated_series
    from app.utils.isbn import normalize_listing_isbn
    from app.utils.text import normalize_text

    pilot_mode.require_isbn(repair["isbn13"])
    code = normalize_listing_isbn(repair["isbn13"])
    if code is None or code != repair["isbn13"] or not repair.get("evidence_url"):
        raise ValueError("Référence de réparation incomplète")
    alert_id = uuid.UUID(repair["alert_id"])
    repo = AlertRepository(session)
    alert = await repo.get_by_id(alert_id)
    if (alert is None or alert.status not in {AlertStatus.ACTIVE, AlertStatus.PAUSED}
            or alert.deleted_at is not None or alert.name != repair["expected_name"]
            or alert.isbn != repair["expected_isbn"]):
        return None
    if alert.isbn and normalize_listing_isbn(alert.isbn) != code:
        raise ValueError("La réparation contredit l’ISBN de l’alerte")
    snapshot = BibliographicSnapshot.capture(alert)
    if await repo.lock_bibliographic_result(alert_id, snapshot) is None:
        return None
    books = BookRepository(session)
    linked = await books.get_all_books_for_alert(alert_id)
    if (len(linked) == 1 and normalize_listing_isbn(linked[0].isbn13) == code
            and linked[0].title == repair["title"]):
        return linked[0].id
    if {str(book.id) for book in linked} != set(repair["expected_book_ids"]):
        return None
    book = await books.ensure_confirmed_book(
        isbn13=code, title=repair["title"], author=repair.get("author"),
        publisher=repair.get("publisher"), publication_year=repair.get("publication_year"),
        format_book=repair.get("format_detail"), language=repair.get("language") or "und",
    )
    book.title = repair["title"]
    book.normalized_title = normalize_text(book.title)
    await books.clear_alert_books(alert_id)
    await books.link_alert_to_book(alert_id, book.id)
    await recalculate_invalidated_series(session)
    await session.commit()
    return book.id


async def apply_market_backfill(
    manifest: dict[str, Any], *, digest: str, limit: int = 8,
) -> dict[str, Any]:
    from app.db.session import AsyncSessionLocal
    from app.services.book_market_pricing import price_book_now

    if digest != manifest.get("digest") or digest != manifest_digest(manifest):
        raise ValueError("Inventaire modifié ou empreinte incorrecte")
    if pilot_mode.active() and pilot_mode.read_state()["digest"] != digest:
        raise pilot_mode.PilotBlockedError("Manifeste différent du pilote activé")
    blocked = {key for row in manifest["alerts"] for key in row["mismatched_book_ids"]}
    allowed_alerts = {uuid.UUID(row["alert_id"]) for row in manifest["alerts"]}
    outcomes: list[dict[str, str | None]] = []
    skipped: list[dict[str, str]] = []
    aliases_relinked: list[dict[str, Any]] = []
    repaired_alerts: list[dict[str, Any]] = []
    for repair in manifest.get("catalogue_repairs", []):
        if pilot_mode.active() and pilot_mode.read_state()["halted"]:
            break
        if len(repaired_alerts) >= limit:
            break
        if uuid.UUID(repair["alert_id"]) not in allowed_alerts:
            raise ValueError("Réparation hors inventaire")
        async with AsyncSessionLocal() as session:
            book_id = await apply_catalogue_repair(session, repair)
        if book_id is not None:
            cache = await price_book_now(book_id, reason="backfill")
            repaired_alerts.append({"alert_id": repair["alert_id"], "book_id": str(book_id),
                                    "outcome": cache.collection_status if cache else "failed"})
    alias_ids: set[str] = set()
    for group in manifest.get("isbn_aliases", []):
        if pilot_mode.active() and pilot_mode.read_state()["halted"]:
            break
        for key in group["alias_book_ids"]:
            alias_ids.add(key)
            if len(aliases_relinked) + len(repaired_alerts) >= limit:
                continue
            async with AsyncSessionLocal() as session:
                changed = await BookRepository(session).relink_isbn_alias(
                    uuid.UUID(key), uuid.UUID(group["canonical_book_id"]),
                    isbn13=group["isbn13"], alert_ids=allowed_alerts,
                )
                from app.services.book_market_pricing import recalculate_invalidated_series

                await recalculate_invalidated_series(session)
                await session.commit()
                if changed:
                    aliases_relinked.append({"book_id": key,
                                             "alerts": [str(key) for key in changed]})
    for row in manifest["books"]:
        if pilot_mode.active() and pilot_mode.read_state()["halted"]:
            break
        if len(outcomes) + len(aliases_relinked) + len(repaired_alerts) >= limit:
            break
        if row["book_id"] in blocked or row["book_id"] in alias_ids:
            continue
        book_id = uuid.UUID(row["book_id"])
        async with AsyncSessionLocal() as session:
            book = await BookRepository(session).get_by_id(book_id)
            if book is None or (book.isbn13, book.title, book.language, book.edition) != (
                row["isbn13"], row["title"], row["language"], row["edition"],
            ):
                skipped.append({"book_id": str(book_id), "outcome": "identity_changed"})
                continue
            from app.db.repositories.market_price_repo import MarketPriceRepository

            linked_ids = await MarketPriceRepository(session).active_alert_ids_for_book(book_id)
            if not any(key in allowed_alerts for key in linked_ids):
                skipped.append({"book_id": str(book_id), "outcome": "membership_changed"})
                continue
            if not collection_due(book.market_cache, datetime.now(UTC)):
                continue
        cache = await price_book_now(book_id, reason="backfill")
        outcomes.append({"book_id": str(book_id),
                         "outcome": cache.collection_status if cache else "failed"})
    return {"digest": digest, "processed": outcomes, "skipped": skipped,
            "aliases_relinked": aliases_relinked,
            "blocked_identity_books": sorted(blocked), "repaired_alerts": repaired_alerts,
            "pilot_halted": pilot_mode.active() and pilot_mode.read_state()["halted"]}

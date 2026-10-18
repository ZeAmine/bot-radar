#!/usr/bin/env python3
"""Applique un plan d'audit relu aux alertes, sans heuristique et sous verrou.

Le plan est une liste de corrections explicites : chacune nomme l'alerte par son
identifiant, son nom et son ISBN, puis la valeur attendue **avant** et **après**.
Un plan périmé est refusé intégralement — aucune écriture partielle.

Par défaut le script ne fait que vérifier (`--apply` absent) : il relit chaque
alerte, contrôle que l'état observé correspond à `avant`, et rapporte ce qu'il
ferait. Rien n'est écrit. `--apply` exécute le lot dans une seule transaction.

Le retrait d'un ISBN passe par `reset_bibliographic_identity`, qui détache les
liaisons de cette seule alerte et remet à NULL son total de série et son verdict
de catalogue. Les livres et leurs cotes restent intacts (contrat F06).

Usage :
    python -m scripts.apply_audit_plan .audit/plan.json           # vérifie
    python -m scripts.apply_audit_plan .audit/plan.json --apply   # écrit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db.models.alert import Alert
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.db.session import AsyncSessionLocal

SUPPORTED_FIELDS = ("isbn", "include_keywords", "exclude_keywords", "variants")


def _validate_entry(entry: dict[str, Any]) -> None:
    if not isinstance(entry.get("id"), str):
        raise ValueError("id manquant")
    uuid.UUID(entry["id"])
    if not entry.get("name"):
        raise ValueError("name manquant")
    field = entry.get("field")
    if field not in SUPPORTED_FIELDS and field != "unlink_book":
        raise ValueError(f"champ non supporté : {field}")
    if field == "unlink_book":
        if not entry.get("book_isbn13"):
            raise ValueError("book_isbn13 manquant pour unlink_book")
    elif "before" not in entry or "after" not in entry:
        raise ValueError("before/after requis")
    reason = entry.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason doit être une chaîne")


async def _apply_entry(
    session: Any, entry: dict[str, Any], *, apply: bool, already_removed: set[str]
) -> dict[str, Any]:
    alert_id = uuid.UUID(entry["id"])
    alert = await session.scalar(select(Alert).where(Alert.id == alert_id).with_for_update())
    if alert is None:
        raise ValueError(f"alerte introuvable : {alert_id}")
    if alert.name != entry["name"]:
        raise ValueError(f"plan périmé (nom) : {entry['name']} != {alert.name}")

    field = entry["field"]
    if field == "unlink_book":
        return await _apply_unlink(
            session, alert, entry, apply=apply, already_removed=already_removed
        )

    current = getattr(alert, field)
    if current != entry["before"]:
        if current == entry["after"]:
            return {"alert": alert.name, "field": field, "status": "déjà appliqué"}
        raise ValueError(
            f"plan périmé ({field}) pour « {alert.name} » : "
            f"observé {current!r}, attendu {entry['before']!r}"
        )
    if not apply:
        return {
            "alert": alert.name,
            "field": field,
            "status": "à corriger",
            "raison": entry.get("reason"),
            "avant": entry["before"],
            "apres": entry["after"],
        }

    was_isbn = getattr(alert, "isbn", None)
    setattr(alert, field, entry["after"])
    if field == "isbn" and was_isbn != entry["after"]:
        await AlertRepository(session).reset_bibliographic_identity(alert)
    return {
        "alert": alert.name,
        "field": field,
        "status": "corrigé",
        "raison": entry.get("reason"),
        "avant": entry["before"],
        "apres": entry["after"],
    }


async def _apply_unlink(
    session: Any, alert: Alert, entry: dict[str, Any], *, apply: bool, already_removed: set[str]
) -> dict[str, Any]:
    """Détache une fiche, en tenant compte des retraits déjà décidés pour cette alerte.

    Plusieurs entrées peuvent viser la même alerte : `clear_alert_books` efface
    toutes les liaisons, donc on conserve tout sauf l'ensemble cumulé des fiches
    à retirer — sinon chaque entrée relierait à nouveau celles des précédentes.
    """
    book_repo = BookRepository(session)
    code = entry["book_isbn13"]
    book = await book_repo.get_by_isbn13(code)
    if book is None:
        return {
            "alert": alert.name,
            "field": "unlink_book",
            "isbn": code,
            "status": "livre introuvable, rien à faire",
        }
    linked = await book_repo.get_all_books_for_alert(alert.id)
    if book.id not in {b.id for b in linked}:
        return {
            "alert": alert.name,
            "field": "unlink_book",
            "isbn": code,
            "status": "déjà dissocié",
        }
    already_removed.add(code)
    if not apply:
        return {
            "alert": alert.name,
            "field": "unlink_book",
            "isbn": code,
            "status": "à dissocier",
            "raison": entry.get("reason"),
            "livre": book.title,
        }
    doomed = {book.id}
    for other in already_removed:
        if other != code and (extra := await book_repo.get_by_isbn13(other)) is not None:
            doomed.add(extra.id)
    remaining = [b for b in linked if b.id not in doomed]
    await book_repo.clear_alert_books(alert.id)
    for keeper in remaining:
        await book_repo.link_alert_to_book(alert_id=alert.id, book_id=keeper.id)
    return {
        "alert": alert.name,
        "field": "unlink_book",
        "isbn": code,
        "status": "dissocié",
        "raison": entry.get("reason"),
        "livre": book.title,
        "restants": [b.title for b in remaining],
    }


async def apply_plan(
    session: Any, plan: list[dict[str, Any]], *, apply: bool
) -> list[dict[str, Any]]:
    """Applique ou vérifie un plan relu ; le propriétaire gère la transaction."""
    results: list[dict[str, Any]] = []
    already_removed: set[str] = set()
    for entry in plan:
        results.append(
            await _apply_entry(session, entry, apply=apply, already_removed=already_removed)
        )
    if not apply:
        await session.rollback()
    return results


async def run(path: Path, *, apply: bool) -> int:
    plan = json.loads(path.read_text())
    if not isinstance(plan, list):
        raise ValueError("le plan doit être une liste")
    for entry in plan:
        _validate_entry(entry)

    async with AsyncSessionLocal() as session, session.begin():
        results = await apply_plan(session, plan, apply=apply)
    print(
        json.dumps(
            {"revues": len(plan), "applique": apply, "resultats": results},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.plan, apply=args.apply)))

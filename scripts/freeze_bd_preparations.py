"""Gèle les seules demandes F04 après arrêt vérifié de l'application.

Lecture seule par défaut. L'orchestrateur doit fermer les entrées Telegram et
vérifier l'arrêt avant --execute --confirmed-stopped. Aucun downgrade SQL.
"""

import argparse
import asyncio
import hashlib
import json
import uuid

from sqlalchemy import text

from app.core.logging import setup_logging
from app.db.session import AsyncSessionLocal


async def freeze(*, execute: bool) -> dict[str, int | str]:
    """Retire l'intention sans perdre contexte, état, erreur ni dates des marqueurs."""
    async with AsyncSessionLocal() as session:
        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        if revision != "0010_alert_preparation":
            raise RuntimeError("Schéma 0010 requis, aucun gel effectué")
        query = (
            "SELECT id, status, deleted_at, preparation FROM alerts WHERE preparation IS NOT NULL"
        )
        rows = (await session.execute(text(query + (" FOR UPDATE" if execute else "")))).all()
        changes = []
        for identifier, status, deleted_at, stored in rows:
            if deleted_at is not None or str(status).upper() == "DELETED":
                continue
            if str(status).upper() != "PAUSED":
                raise RuntimeError("Demande marquée hors pause : intervention explicite requise")
            request = json.loads(stored) if isinstance(stored, str) else stored
            if not isinstance(request, dict) or request.get("state") not in ("pending", "blocked"):
                raise RuntimeError("Demande illisible : aucun gel effectué")
            if request.get("activation_requested") is True:
                changes.append(
                    (
                        identifier,
                        {**request, "token": str(uuid.uuid4()), "activation_requested": False},
                    )
                )
        if execute:
            for identifier, frozen in changes:
                await session.execute(
                    text("UPDATE alerts SET preparation = CAST(:request AS jsonb) WHERE id = :id"),
                    {"id": identifier, "request": json.dumps(frozen)},
                )
            await session.commit()
        snapshot = (
            await session.execute(
                text("SELECT id, preparation FROM alerts WHERE preparation IS NOT NULL ORDER BY id")
            )
        ).all()
        canonical = [
            (str(identifier), json.loads(value) if isinstance(value, str) else value)
            for identifier, value in snapshot
        ]
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
        return {
            "marked_rows": len(rows),
            "requests_to_freeze": len(changes),
            "executed": int(execute),
            "marker_hash": digest,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirmed-stopped", action="store_true")
    args = parser.parse_args()
    if args.execute and not args.confirmed_stopped:
        parser.error("--execute exige un arrêt vérifié par l'orchestrateur")
    setup_logging()
    print(json.dumps(asyncio.run(freeze(execute=args.execute))))


if __name__ == "__main__":
    main()

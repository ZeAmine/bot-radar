#!/usr/bin/env python3
"""Applique un plan JSON relu de réparation des inclusions, sans heuristique en base.

Le plan garde les listes avant/après et l'identité de chaque alerte pour permettre
un retour arrière. Par défaut, vérifie le plan sans écrire ; --apply écrit en une
transaction, après verrouillage et validation de toutes les alertes. Un plan périmé
est refusé intégralement ; un plan déjà appliqué ne réécrit rien.

Usage : python -m scripts.repair_alert_inclusions chemin/plan.json [--apply]
Pour restaurer, inverser includes_before/after et variants_before/after dans le plan.
"""

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.alert import Alert
from app.db.session import AsyncSessionLocal
from app.utils.text import normalize_text


async def apply_plan(session: AsyncSession, plan: list[dict[str, Any]]) -> int:
    """Valide toutes les lignes avant de modifier ; le propriétaire gère la transaction."""
    ids = [uuid.UUID(row["id"]) for row in plan]
    if len(set(ids)) != len(ids):
        raise ValueError("Identifiants dupliqués dans le plan")
    alerts = {
        a.id: a for a in (await session.scalars(
            select(Alert).where(Alert.id.in_(ids)).with_for_update()
        )).all()
    }
    pending = []
    for row, alert_id in zip(plan, ids, strict=True):
        includes = row["includes_after"]
        variants = row["variants_after"]
        if (
            not isinstance(includes, list) or not includes
            or not isinstance(variants, list)
            or any(not isinstance(k, str) or not normalize_text(k) for k in includes + variants)
        ):
            raise ValueError(f"Termes invalides : {alert_id}")
        alert = alerts.get(alert_id)
        if (
            alert is None or alert.name != row["name"] or alert.isbn != row["isbn"]
            or (alert.deleted_at is not None) != row.get("deleted", False)
        ):
            raise ValueError(f"Plan périmé (identité) : {alert_id}")
        current = (alert.include_keywords, alert.variants)
        after = (includes, variants)
        if current == after:
            continue
        if current != (row["includes_before"], row["variants_before"]):
            raise ValueError(f"Plan périmé (mots-clés) : {alert_id}")
        pending.append((alert, includes, variants))
    for alert, includes, variants in pending:
        alert.include_keywords = includes
        alert.variants = variants
    return len(pending)


async def run(path: Path, *, apply: bool) -> None:
    """Relit le plan, puis valide ou applique le lot dans une seule transaction."""
    plan = json.loads(path.read_text())
    async with AsyncSessionLocal() as session, session.begin():
        changed = await apply_plan(session, plan)
        if not apply:
            await session.rollback()
    print(json.dumps({"reviewed": len(plan), "changed": changed, "applied": apply}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.plan, apply=args.apply))

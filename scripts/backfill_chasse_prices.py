import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(args: argparse.Namespace) -> int:
    from app.db.session import AsyncSessionLocal, engine
    from app.services.market_backfill import apply_market_backfill, inventory_market_backfill

    try:
        if args.apply:
            manifest = json.loads(args.manifest.read_text())
            report = await apply_market_backfill(manifest, digest=args.approved_digest,
                                                limit=args.limit)
            print(json.dumps(report, ensure_ascii=False))
            return 0
        else:
            async with AsyncSessionLocal() as session:
                from sqlalchemy import text

                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                manifest = await inventory_market_backfill(session)
                await session.rollback()
            if args.catalogue_plan:
                manifest["catalogue_repairs"] = json.loads(args.catalogue_plan.read_text())
                from app.services.market_backfill import manifest_digest

                manifest["digest"] = manifest_digest(manifest)
            with args.manifest.open("x") as output:
                json.dump(manifest, output, ensure_ascii=False, indent=2)
            print(json.dumps({"alerts": len(manifest["alerts"]), "books": len(manifest["books"]),
                              "digest": manifest["digest"], "database_modified": False}))
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    from app.core.logging import setup_logging

    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--catalogue-plan", type=Path)
    parser.add_argument("--approved-digest", default="")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    if args.apply and not args.approved_digest:
        parser.error("--apply exige --approved-digest")
    if not 1 <= args.limit <= 8:
        parser.error("--limit doit être compris entre 1 et 8")
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

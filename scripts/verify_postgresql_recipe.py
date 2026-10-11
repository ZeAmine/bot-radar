"""Recette synthétique PostgreSQL, sans configuration ni transport réels.

L'identifiant système doit être attesté directement dans le conteneur neuf avant
l'exécution. Le service et son nettoyage restent à la charge de l'opérateur.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HOST, PORT = "127.0.0.1", 55486
DATABASE, USER = "radar_recipe_f52f786", "radar_recipe_user"
PASSWORD = "synthetic_recipe_password"
URL = f"postgresql+asyncpg://{USER}:{PASSWORD}@{HOST}:{PORT}/{DATABASE}?ssl=disable"
TARGETS = [
    "tests/integration/test_notification_recovery.py",
    "tests/integration/test_delivery_persistence.py",
    "tests/integration/test_ingest_serialization.py",
    "tests/integration/test_postgresql_notification_reservation.py",
    "tests/integration/test_series_pricing_concurrency.py"
    "::test_two_committing_tomes_leave_the_complete_series_total",
]


def isolate(network: bool) -> dict[str, int]:
    """Pose les barrières avant tout import applicatif ou collecte."""
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.clear()
    os.environ.update({
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "APP_ENV": "test",
        "DATABASE_URL": URL if network else "sqlite+aiosqlite:///:memory:",
        "TEST_DATABASE_URL": URL if network else "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://example.invalid:6379/0",
        "TELEGRAM_BOT_TOKEN": "123456789:AAG_ci_test_dummy_token_abc123",
        "TELEGRAM_WEBHOOK_SECRET": "synthetic_webhook_secret",
        "INGEST_API_TOKEN": "synthetic_ingest_token",
        "INTERNAL_API_TOKEN": "synthetic_internal_token",
        "TELEGRAM_ALLOWED_USER_IDS": "424242", "AI_ENABLED": "false",
        "AI_BASE_URL": "https://example.invalid", "AI_MODEL": "synthetic-model",
        "VINTED_POLLER_ENABLED": "false",
        "PGSSLMODE": "disable",
    })
    counters = {"dotenv": 0, "local_denied": 0, "external_denied": 0,
                "postgres_config": 0, "allowed": 0}

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            if ".postgresql" in path.parts or path.name in {".pgpass", ".pg_service.conf"}:
                counters["postgres_config"] += 1
                raise PermissionError("Configuration PostgreSQL personnelle interdite")
        if (event == "open" and isinstance(args[0], (str, bytes, os.PathLike))
                and Path(os.fsdecode(args[0])).name.startswith(".env")):
            counters["dotenv"] += 1
            raise PermissionError("Dotenv interdit")
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            destination = args[1] if event == "socket.connect" else args[:2]
            if network and event != "socket.sendto" and destination == (HOST, PORT):
                counters["allowed"] += 1
                return
            host = destination[0] if isinstance(destination, tuple) else destination
            key = "local_denied" if host == HOST else "external_denied"
            counters[key] += 1
            raise PermissionError("Transport hors recette interdit")

    sys.addaudithook(audit)
    for destination in [(HOST, PORT + 1), ("192.0.2.1", 9)]:
        with socket.socket() as probe:
            try:
                probe.connect(destination)
            except PermissionError:
                pass
            else:
                raise AssertionError("Barrière réseau inactive")
    try:
        (root / ".env").open()
    except PermissionError:
        pass
    else:
        raise AssertionError("Barrière dotenv inactive")
    from pydantic_settings import BaseSettings

    original_init = BaseSettings.__init__

    def no_dotenv(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.update(_env_file=None, _secrets_dir=None)
        original_init(self, *args, **kwargs)

    def sources(_cls: Any, /, _settings_cls: Any, **values: Any) -> tuple[Any, ...]:
        return values["init_settings"], values["env_settings"]

    # Substitution dynamique des frontières d'I/O, propre au sous-processus.
    settings_type: Any = BaseSettings
    settings_type.__init__ = no_dotenv
    settings_type.settings_customise_sources = classmethod(sources)
    from apscheduler.schedulers.base import BaseScheduler

    def no_scheduler(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Scheduler interdit")

    BaseScheduler.start = no_scheduler
    return counters


async def verify_identity(system_id: str) -> None:
    """Compare l'instance au relevé Docker, pas seulement au nom de base."""
    import asyncpg

    connection = await asyncpg.connect(
        host=HOST, port=PORT, user=USER, password=PASSWORD, database=DATABASE, timeout=5,
    )
    try:
        row = await connection.fetchrow(
            "SELECT current_database() AS db, current_user AS usr, "
            "current_setting('server_version_num') AS version, "
            "inet_server_port() AS port, system_identifier::text AS identity "
            "FROM pg_control_system()"
        )
        assert row is not None
        assert (row["db"], row["usr"], row["port"], row["identity"]) == (
            DATABASE, USER, 5432, system_id,
        ), "Destination différente de la ressource attestée"
        assert 160000 <= int(row["version"]) < 170000
        print("POSTGRES_IDENTITY", json.dumps(dict(row)), flush=True)
    finally:
        await connection.close(timeout=5)


async def seed_old_schema() -> dict[str, list[dict[str, Any]]]:
    """Peuple le véritable schéma 0010 par réflexion, sans create_all."""
    from sqlalchemy import MetaData, insert, select, text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(URL)
    now = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
    uid, lid = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0010_alert_preparation"
            )
            metadata = MetaData()
            await connection.run_sync(metadata.reflect)
            tables = metadata.tables
            assert "notification_state" not in tables["listing_matches"].c
            categories = select(tables["categories"].c.id).limit(1)
            cid: Any = (await connection.execute(categories)).scalar_one()
            await connection.execute(insert(tables["users"]), {
                "id": uid, "telegram_user_id": 424242, "is_allowed": True,
                "created_at": now, "updated_at": now,
            })
            await connection.execute(insert(tables["listings"]), {
                "id": lid, "source_name": "synthetic", "external_id": "old-evidence",
                "url": "https://example.invalid/item", "title": "Synthétique",
                "normalized_title": "synthetique", "category_slug": "tech",
                "item_price_cents": 100, "photos": [], "raw_payload": {}, "received_at": now,
            })
            for index in range(3):
                aid = uuid.uuid4()
                await connection.execute(insert(tables["alerts"]), {
                    "id": aid, "user_id": uid, "category_id": cid, "name": f"Fictive {index}",
                    "include_keywords": ["synthetique"], "exclude_keywords": [], "variants": [],
                    "status": "ACTIVE", "created_at": now, "updated_at": now,
                })
                await connection.execute(insert(tables["listing_matches"]), {
                    "id": uuid.uuid4(), "alert_id": aid, "listing_id": lid,
                    "keyword_match_score": 100, "condition_matches": True,
                    "price_matches": True, "notified_at": now if index == 0 else None,
                    "created_at": now,
                })
            return {name: [dict(row) for row in (await connection.execute(
                select(tables[name]).order_by(tables[name].c.id)
            )).mappings()] for name in ("users", "alerts", "listings", "listing_matches")}
    finally:
        await engine.dispose()


async def reset_synthetic_schema() -> None:
    """Reset explicite, seulement après attestation de l'instance synthétique."""
    import asyncpg

    connection = await asyncpg.connect(
        host=HOST, port=PORT, user=USER, password=PASSWORD, database=DATABASE, timeout=5,
    )
    try:
        await connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public", timeout=5)
    finally:
        await connection.close(timeout=5)


async def verify_migration(before: dict[str, list[dict[str, Any]]]) -> None:
    from sqlalchemy import MetaData, select, text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(URL)
    try:
        async with engine.connect() as connection:
            metadata = MetaData()
            await connection.run_sync(metadata.reflect)
            for name, old_rows in before.items():
                table = metadata.tables[name]
                columns = [table.c[key] for key in old_rows[0]]
                rows = [dict(row) for row in (await connection.execute(
                    select(*columns).order_by(table.c.id)
                )).mappings()]
                assert rows == old_rows, f"Preuves historiques altérées : {name}"
            matches = (await connection.execute(
                select(metadata.tables["listing_matches"])
            )).mappings()
            for row in matches:
                assert row["notification_state"] == (
                    "notified" if row["notified_at"] is not None else "unknown"
                )
                for key in ("attempt_count", "attempt_id", "attempt_started_at", "next_attempt_at",
                            "telegram_chat_id", "telegram_bot_id", "telegram_message_id"):
                    assert row[key] is None, f"Preuve inventée : {key}"
                if row["notified_at"] is None:
                    assert row["notification_reason"] == "legacy_unclassified"
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0011_notification_state"
            )
            print("MIGRATION_PROOF", json.dumps({"old_matches": 3, "notified": 1,
                  "unknown": 2, "old_rows_unchanged": True, "invented_evidence": False}))
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system-id", required=True)
    parser.add_argument("--mode", choices=("migration", "tests", "sqlite-check", "typecheck"),
                        required=True)
    parser.add_argument("--isolated-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--reset-synthetic-schema", action="store_true",
                        help="Rejouer la migration sur cette seule base synthétique attestée")
    arguments = parser.parse_args()
    if arguments.reset_synthetic_schema and arguments.mode != "migration":
        parser.error("Le reset est réservé à la recette de migration")
    if not arguments.isolated_child:
        child = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve()), *sys.argv[1:],
             "--isolated-child"], env={}, start_new_session=True,
        )
        try:
            return child.wait(timeout=120)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            print("RECIPE_ABORTED: sous-processus terminé et attendu", flush=True)
            return 124
    counters = isolate(arguments.mode in {"migration", "tests"})
    result = 1
    try:
        if arguments.mode in {"migration", "tests"}:
            asyncio.run(verify_identity(arguments.system_id))
        if arguments.mode == "migration":
            from alembic.config import Config

            from alembic import command

            if arguments.reset_synthetic_schema:
                asyncio.run(reset_synthetic_schema())
            config = Config("alembic.ini")
            command.upgrade(config, "0010_alert_preparation")
            before = asyncio.run(seed_old_schema())
            asyncio.run(verify_identity(arguments.system_id))
            command.upgrade(config, "0011_notification_state")
            asyncio.run(verify_migration(before))
            result = 0
        elif arguments.mode == "typecheck":
            from mypy import api

            out, err, result = api.run([
                "--no-incremental", "--follow-imports=silent",
                "scripts/verify_postgresql_recipe.py",
            ])
            print(out, end="")
            print(err, end="", file=sys.stderr)
        else:
            import pytest

            plugins: list[Any] = []
            reports: dict[str, dict[str, str]] = {}

            class Completion:
                def pytest_runtest_logreport(self, report: Any) -> None:
                    reports.setdefault(report.nodeid, {})[report.when] = report.outcome
                    if report.when == "call":
                        for key, value in report.user_properties:
                            if key == "postgres_recipe":
                                print("POSTGRES_PARTICIPANTS", json.dumps(value), flush=True)

            plugins.append(Completion())
            if arguments.mode == "tests":
                from contextlib import asynccontextmanager

                from tests import database

                original = database._fresh_database

                @asynccontextmanager
                async def guarded(url: str) -> AsyncIterator[database.FreshDatabase]:
                    assert url == URL, "Reset hors base autorisée"
                    await verify_identity(arguments.system_id)
                    async with original(url) as db:
                        yield db

                database._fresh_database = guarded
            result = int(pytest.main([
                "-p", "pytest_asyncio.plugin", "-v", "-s", "-p", "no:cacheprovider",
                "--tb=short", *(TARGETS if arguments.mode == "tests" else TARGETS[-1:]),
            ], plugins=plugins))
            if arguments.mode == "tests":
                assert len(reports) == 78, "Collecte incomplète"
                assert all(phases == {"setup": "passed", "call": "passed", "teardown": "passed"}
                           for phases in reports.values()), "Scénario non exécuté ou en échec"
    finally:
        print("RECIPE_ISOLATION", json.dumps(counters), flush=True)
    assert {key: counters[key] for key in ("dotenv", "local_denied", "external_denied")} == {
        "dotenv": 1, "local_denied": 1, "external_denied": 1,
    }, "Tentative accidentelle hors recette"
    assert counters["postgres_config"] == 0, "Configuration personnelle sollicitée"
    return result


if __name__ == "__main__":
    raise SystemExit(main())

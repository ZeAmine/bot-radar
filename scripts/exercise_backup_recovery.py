"""Exercice exclusivement synthétique ; crée deux ressources neuves puis les détruit.

Aucun démarrage applicatif, Redis, scheduler, poller ou Telegram réel.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOCKER_SOCKET = "unix:///Users/aminezegmou/.docker/run/docker.sock"
IMAGE = "sha256:c05eced0bdb41ea9b95a656472a6aa4d50cad0d8a2e33d14eb1c53fd6204f2ae"


def isolation(endpoints: list[tuple[str, int]]) -> dict[str, int]:
    """Même contrat pré-import que les recettes précédentes, deux adresses exactes."""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    os.environ.clear()
    os.environ.update(
        {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "APP_ENV": "test",
            "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "PGSSLMODE": "disable",
            "REDIS_URL": "redis://example.invalid:6379/0",
            "AI_ENABLED": "false",
            "TELEGRAM_BOT_TOKEN": "123456789:AAG_ci_test_dummy_token_abc123",
            "TELEGRAM_ALLOWED_USER_IDS": "424242",
            "TELEGRAM_WEBHOOK_SECRET": "synthetic",
            "INGEST_API_TOKEN": "synthetic",
            "INTERNAL_API_TOKEN": "synthetic",
            "VINTED_POLLER_ENABLED": "false",
        }
    )
    counts = {"dotenv": 0, "local_probe": 0, "external_probe": 0, "allowed": 0, "pg_config": 0}

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            if path.name.startswith(".env"):
                counts["dotenv"] += 1
                raise PermissionError("Dotenv interdit")
            if ".postgresql" in path.parts or path.name in {".pgpass", ".pg_service.conf"}:
                counts["pg_config"] += 1
                raise PermissionError("Configuration personnelle interdite")
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            destination = args[1] if event == "socket.connect" else args[:2]
            if event != "socket.sendto" and destination in endpoints:
                counts["allowed"] += 1
                return
            key = "local_probe" if destination[0] == "127.0.0.1" else "external_probe"
            counts[key] += 1
            raise PermissionError("Transport hors exercice interdit")

    sys.addaudithook(audit)
    for address in [("127.0.0.1", 55522), ("192.0.2.1", 9)]:
        with socket.socket() as probe:
            try:
                probe.connect(address)
            except PermissionError:
                pass
            else:
                raise AssertionError("Barrière inactive")
    try:
        (ROOT / ".env").open()
    except PermissionError:
        pass
    else:
        raise AssertionError("Barrière dotenv inactive")
    from pydantic_settings import BaseSettings

    original = BaseSettings.__init__

    def no_dotenv(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.update(_env_file=None, _secrets_dir=None)
        original(self, *args, **kwargs)

    def sources(_cls: Any, /, _settings_cls: Any, **values: Any) -> tuple[Any, ...]:
        return values["init_settings"], values["env_settings"]

    settings_type: Any = BaseSettings
    settings_type.__init__ = no_dotenv
    settings_type.settings_customise_sources = classmethod(sources)
    from apscheduler.schedulers.base import BaseScheduler

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Service interdit")

    BaseScheduler.start = forbidden
    from aiogram.client.session.aiohttp import AiohttpSession

    session_type: Any = AiohttpSession
    session_type.make_request = forbidden
    return counts


async def exercise(data: dict[str, Any]) -> dict[str, Any]:
    from unittest.mock import AsyncMock

    from alembic.config import Config
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from alembic import command as alembic_command
    from app.core.config import settings
    from scripts.backup_recovery import (
        Endpoint,
        backup,
        gpg,
        publish_simulated,
        restore,
        verify,
    )

    source, target = [Endpoint(**value) for value in data["endpoints"]]
    workspace = Path(data["workspace"])
    binary = data["gpg"]
    homes = [workspace / name for name in ("public-key", "private-key", "wrong-key")]
    for home in homes:
        home.mkdir(mode=0o700)
    for home in homes[1:]:
        gpg(
            binary,
            home,
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Synthetic Recovery <synthetic@example.invalid>",
            "rsa2048",
            "encr",
            "1d",
            data=b"",
        )
    colon = gpg(binary, homes[1], "--with-colons", "--list-keys", data=b"").decode()
    recipient = next(line.split(":")[9] for line in colon.splitlines() if line.startswith("fpr:"))
    public = gpg(binary, homes[1], "--export", recipient, data=b"")
    gpg(binary, homes[0], "--import", data=public)
    assert b"sec:" not in gpg(binary, homes[0], "--with-colons", "--list-secret-keys", data=b"")
    source_url = (
        f"postgresql+asyncpg://{source.user}:{source.password}@{source.host}:"
        f"{source.port}/{source.database}?ssl=disable"
    )
    settings.DATABASE_URL = source_url
    # Alembic utilise son propre asyncio.run : exécution hors de cette boucle via thread.
    await asyncio.to_thread(alembic_command.upgrade, Config(str(ROOT / "alembic.ini")), "head")
    engine = create_async_engine(source_url)
    try:
        from app.db.models.alert import Alert
        from app.db.models.category import Category
        from app.db.models.listing_match import ListingMatch
        from app.db.models.user import User
        from app.db.repositories.listing_repo import ListingRepository
        from app.domain.entities.alert_preparation import PreparationRequest
        from app.schemas.listing import ListingIngestPayload

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            user = User(telegram_user_id=424242, is_allowed=True)
            # La catégorie tech est déjà semée par Alembic.
            from sqlalchemy import select

            category = (
                await session.scalars(select(Category).where(Category.slug == "tech"))
            ).one()
            session.add(user)
            await session.flush()
            alert = Alert(
                user_id=user.id,
                category_id=category.id,
                name="Console",
                include_keywords=["console"],
                status="ACTIVE",
            )
            prep_category = (
                await session.scalars(select(Category).where(Category.slug == "bd_comics"))
            ).one()
            original_request = PreparationRequest(author_hint="Synthetic").stored()
            original_request["context"] = "must-survive"
            prep = Alert(
                user_id=user.id,
                category_id=prep_category.id,
                name="Préparation",
                include_keywords=["preparation"],
                status="PAUSED",
                preparation=original_request,
            )
            session.add_all([alert, prep])
            await session.flush()
            for index, state in enumerate(("ready", "deferred", "sending", "unknown", "notified")):
                payload = ListingIngestPayload(
                    source_name="synthetic",
                    external_id=str(index),
                    title="Console",
                    url="https://example.invalid",
                    category_slug="tech",
                    item_price_cents=100,
                )
                listing = await ListingRepository(session).create_from_payload(payload, {})
                session.add(
                    ListingMatch(
                        alert_id=alert.id,
                        listing_id=listing.id,
                        notification_state=state,
                        attempt_count=0,
                        notified_at=(
                            datetime.now(UTC) - timedelta(hours=1) if state == "notified" else None
                        ),
                    )
                )
            from app.db.models.alert_book import AlertBook
            from app.db.models.book import Book
            from app.db.models.book_market_cache import BookMarketCache
            from app.db.repositories.market_price_repo import MarketPriceRepository
            from app.providers.base import MarketOffer

            book = Book(title="Synthetic book", normalized_title="synthetic book")
            session.add(book)
            await session.flush()
            session.add(AlertBook(alert_id=prep.id, book_id=book.id))
            session.add(
                BookMarketCache(
                    book_id=book.id,
                    historical_low_1y_cents=1,
                    historical_low_at=datetime.now(UTC) - timedelta(days=500),
                    lowest_delivered_price_cents=900,
                    comparison_status="AVAILABLE",
                    checked_at=datetime.now(UTC) - timedelta(hours=2),
                    expires_at=datetime.now(UTC) + timedelta(hours=22),
                )
            )
            await MarketPriceRepository(session).save_snapshot(
                book.id,
                MarketOffer(
                    provider_name="synthetic",
                    item_price_cents=200,
                    shipping_price_cents=0,
                    delivered_price_cents=200,
                    source_url="https://example.invalid",
                    item_condition="Très bon",
                    observed_at=datetime.now(UTC) - timedelta(days=80),
                    raw_payload={"synthetic": "only"},
                ),
            )
            await session.commit()
        simulated_fsm = {"draft_id": "synthetic-original-draft", "creation_outcome": "unknown"}
        out, published = workspace / "archives", workspace / "publication-simulated"
        out.mkdir(mode=0o700)
        published.mkdir(mode=0o700)
        start = time.monotonic()
        local = await backup(source, ROOT, out, binary, homes[0], recipient)
        backup_seconds = time.monotonic() - start
        start = time.monotonic()
        copy = publish_simulated(local, published)
        transfer_seconds = time.monotonic() - start
        manifest = verify(copy)
        negatives = []

        async def reject(name: str, operation: Any) -> None:
            try:
                await operation()
            except (ValueError, RuntimeError, FileNotFoundError, FileExistsError):
                negatives.append(name)
            else:
                raise AssertionError("Contrôle négatif accepté : " + name)

        await reject(
            "failed_new_backup",
            lambda: backup(
                source, ROOT, out, binary, homes[0], "0000000000000000000000000000000000000000"
            ),
        )
        assert verify(copy)["cipher_sha256"] == manifest["cipher_sha256"]
        await reject(
            "wrong_key",
            lambda: restore(copy, target, binary, homes[2], manifest["application_revision"]),
        )
        await reject(
            "wrong_target",
            lambda: restore(
                copy,
                replace(target, system_id="0"),
                binary,
                homes[1],
                manifest["application_revision"],
            ),
        )
        await reject(
            "wrong_revision", lambda: restore(copy, target, binary, homes[1], "incompatible")
        )
        try:
            publish_simulated(local, published)
        except FileExistsError:
            negatives.append("overwrite")
        else:
            raise AssertionError("Écrasement accepté")
        original_hash = (copy / "bundle.gpg").read_bytes()
        for name, body in [("corrupt", b"broken"), ("truncated", original_hash[:-10])]:
            damaged = workspace / name
            shutil.copytree(copy, damaged)
            (damaged / "bundle.gpg").write_bytes(body)
            try:
                verify(damaged)
            except ValueError:
                negatives.append(name)
            else:
                raise AssertionError("Corruption acceptée")
        incomplete = workspace / "incomplete"
        shutil.copytree(copy, incomplete)
        (incomplete / "COMPLETE").unlink()
        try:
            verify(incomplete)
        except FileNotFoundError:
            negatives.append("incomplete")
        else:
            raise AssertionError("Copie incomplète acceptée")
        assert verify(copy)["cipher_sha256"] == manifest["cipher_sha256"]
        # Événements synthétiques après instantané : reçu, inconnu, modification et suppression.
        from app.use_cases.ingest_listing import ProcessListingUseCase
        from tests.telegram_fakes import telegram_message

        acknowledged, uncertain = AsyncMock(), AsyncMock()
        acknowledged.id = uncertain.id = 123456789
        acknowledged.send_message.side_effect = [
            telegram_message(424242, 77),
            telegram_message(424242, 78),
        ]
        uncertain.send_message.side_effect = TimeoutError("synthetic acceptance unknown")
        for external_id, simulated_bot in [
            ("0", acknowledged),
            ("1", uncertain),
            ("absent-in-copy", acknowledged),
        ]:
            async with maker() as session:
                outcome = await ProcessListingUseCase(session, simulated_bot).execute(
                    ListingIngestPayload(
                        source_name="synthetic",
                        external_id=external_id,
                        title="Console",
                        url="https://example.invalid",
                        category_slug="tech",
                        item_price_cents=100,
                    ),
                    {},
                )
                assert outcome.matches[0].notification_sent == (simulated_bot is acknowledged)
        assert acknowledged.send_message.await_count == 2
        assert uncertain.send_message.await_count == 1
        acknowledged.send_photo.assert_not_awaited()
        uncertain.send_photo.assert_not_awaited()
        connection = await source.connect()
        try:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM listing_matches WHERE telegram_message_id IS NOT NULL"
                )
                == 2
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM listing_matches WHERE notification_state='unknown'"
                )
                == 2
            )
            await connection.execute(
                "UPDATE alerts SET name='Modification après sauvegarde' WHERE status='ACTIVE'"
            )
            await connection.execute("DELETE FROM alerts WHERE status='ACTIVE'")
        finally:
            await connection.close(timeout=5)
        simulated_fsm.clear()  # Aucun état mémoire/Redis n’est importé depuis la copie.
        restored = await restore(copy, target, binary, homes[1], manifest["application_revision"])
        assert simulated_fsm == {}
        assert (copy / "bundle.gpg").read_bytes() == original_hash
        await reject(
            "nonempty_target",
            lambda: restore(copy, target, binary, homes[1], manifest["application_revision"]),
        )
        connection = await target.connect()
        try:
            matches = await connection.fetch("SELECT * FROM listing_matches")
            assert sum(row["notified_at"] is not None for row in matches) == 1
            assert all(
                row["notification_state"] == "unknown"
                for row in matches
                if row["notified_at"] is None
            )
            alerts = await connection.fetch("SELECT * FROM alerts")
            assert all(row["status"] == "PAUSED" for row in alerts)
            requests = [json.loads(row["preparation"]) for row in alerts if row["preparation"]]
            assert requests[0]["context"] == "must-survive"
            assert requests[0]["activation_requested"] is False
            assert all(
                requests[0][key] == value
                for key, value in original_request.items()
                if key not in {"token", "activation_requested"}
            )
        finally:
            await connection.close(timeout=5)
        target_url = (
            f"postgresql+asyncpg://{target.user}:{target.password}@{target.host}:"
            f"{target.port}/{target.database}?ssl=disable"
        )
        target_engine = create_async_engine(target_url)
        try:
            target_maker = async_sessionmaker(target_engine, expire_on_commit=False)
            from app.use_cases import prepare_bd_alert
            from app.use_cases.ingest_listing import ProcessListingUseCase

            bot = AsyncMock()
            bot.id = 123456789
            async with target_maker() as session:
                response = await ProcessListingUseCase(session, bot).execute(
                    ListingIngestPayload(
                        source_name="synthetic",
                        external_id="absent-in-copy",
                        title="Console",
                        url="https://example.invalid",
                        category_slug="tech",
                        item_price_cents=100,
                    ),
                    {},
                )
                assert response.matches == []
            bot.send_message.assert_not_awaited()
            bot.send_photo.assert_not_awaited()
            preparation_module: Any = prepare_bd_alert
            prior = preparation_module.AsyncSessionLocal
            preparation_module.AsyncSessionLocal = target_maker
            try:
                await prepare_bd_alert.recover_bd_preparations()
            finally:
                preparation_module.AsyncSessionLocal = prior
            connection = await target.connect()
            try:
                assert (
                    await connection.fetchval("SELECT count(*) FROM alerts WHERE status='ACTIVE'")
                    == 0
                )
            finally:
                await connection.close(timeout=5)
        finally:
            await target_engine.dispose()
        assert "redis" not in manifest["files"]
        assert not {"redis.rdb", "fsm.json"}.intersection(manifest["files"])
        assert restored["business_released"] is False
        return {
            "backup_seconds": backup_seconds,
            "simulated_transfer_seconds": transfer_seconds,
            "stages": manifest["durations"],
            "restore": restored,
            "negative_controls": negatives,
            "publication": "simulated_verified",
            "independent_verified": False,
            "restored_data_verified_before_quarantine": True,
            "simulated_sends_after_restore": 0,
            "fsm_imported": False,
            "post_snapshot_events_recovered": False,
            "post_snapshot_acknowledged_sends": 2,
            "post_snapshot_unknown_attempts": 1,
        }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--unit", action="store_true")
    parser.add_argument("--typecheck", action="store_true")
    args = parser.parse_args()
    if args.child or args.unit or args.typecheck:
        data = json.loads(args.child.read_bytes()) if args.child else None
        child_addresses = [("127.0.0.1", row["port"]) for row in data["endpoints"]] if data else []
        counts = isolation(child_addresses)
        result = 0
        try:
            if args.child:
                assert data is not None
                report = asyncio.run(exercise(data))
                print("BACKUP_EXERCISE", json.dumps(report, sort_keys=True), flush=True)
            elif args.typecheck:
                from mypy import api

                out, err, result = api.run(
                    [
                        "--no-incremental",
                        "--explicit-package-bases",
                        "--follow-imports=silent",
                        "scripts/backup_recovery.py",
                        "scripts/exercise_backup_recovery.py",
                    ]
                )
                print(out, end="")
                print(err, end="", file=sys.stderr)
            else:
                import pytest

                reports: dict[str, dict[str, str]] = {}

                class Completion:
                    def pytest_runtest_logreport(self, report: Any) -> None:
                        reports.setdefault(report.nodeid, {})[report.when] = report.outcome

                result = int(
                    pytest.main(
                        [
                            "-p",
                            "pytest_asyncio.plugin",
                            "-q",
                            "-p",
                            "no:cacheprovider",
                            "tests/unit/test_backup_recovery.py",
                        ], plugins=[Completion()]
                    )
                )
                assert len(reports) == 16, "Collecte incomplète"
                assert all(
                    phases == {"setup": "passed", "call": "passed", "teardown": "passed"}
                    for phases in reports.values()
                ), "Scénario non exécuté ou en échec"
        finally:
            print("BACKUP_ISOLATION", json.dumps(counts), flush=True)
        assert counts["dotenv"] == counts["local_probe"] == counts["external_probe"] == 1
        assert counts["pg_config"] == 0
        return result
    sys.path.insert(0, str(ROOT))
    from scripts.backup_recovery import Endpoint, command

    def tool(name: str) -> str:
        located = shutil.which(name)
        if located is None:
            raise RuntimeError("Prérequis absent ; aucune installation autorisée")
        return located

    tools = {name: tool(name) for name in ("docker", "gpg", "gpgconf")}
    with tempfile.TemporaryDirectory(prefix="radar-backup9-", dir="/private/tmp") as directory:
        workspace = Path(directory)
        config = workspace / "docker-config"
        config.mkdir(mode=0o700)
        environment = {"DOCKER_HOST": DOCKER_SOCKET, "DOCKER_CONFIG": str(config)}
        nonce = uuid.uuid4().hex
        ids = []
        try:
            endpoints = []
            for port in (55520, 55521):
                identifier = (
                    command(
                        [
                            tools["docker"],
                            "create",
                            "--pull=never",
                            "--label",
                            "botradar.exercise=" + nonce,
                            "--publish",
                            f"127.0.0.1:{port}:5432",
                            "--tmpfs",
                            "/var/lib/postgresql/data:rw",
                            "--env",
                            "POSTGRES_USER=synthetic",
                            "--env",
                            "POSTGRES_PASSWORD=synthetic_password",
                            "--env",
                            "POSTGRES_DB=synthetic",
                            IMAGE,
                        ],
                        environment=environment,
                    )
                    .decode()
                    .strip()
                )
                ids.append(identifier)
                command([tools["docker"], "start", identifier], environment=environment)
                deadline = time.monotonic() + 30
                while True:
                    try:
                        system_id = (
                            command(
                                [
                                    tools["docker"],
                                    "exec",
                                    identifier,
                                    "psql",
                                    "-U",
                                    "synthetic",
                                    "-d",
                                    "synthetic",
                                    "-Atc",
                                    "SELECT system_identifier FROM pg_control_system();",
                                ],
                                environment=environment,
                            )
                            .decode()
                            .strip()
                        )
                        break
                    except RuntimeError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.25)
                endpoint = Endpoint(
                    identifier,
                    system_id,
                    "127.0.0.1",
                    port,
                    "synthetic",
                    "synthetic",
                    "synthetic_password",
                    nonce,
                    tools["docker"],
                    DOCKER_SOCKET,
                    str(config),
                )
                endpoint.metadata()
                endpoints.append(asdict(endpoint))
                print(
                    "SYNTHETIC_RESOURCE",
                    json.dumps({"id": identifier, "system_id": system_id, "port": port}),
                    flush=True,
                )
            attestation = workspace / "attestation.json"
            attestation.write_text(
                json.dumps(
                    {"endpoints": endpoints, "workspace": str(workspace), "gpg": tools["gpg"]}
                )
            )
            child = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--child", str(attestation)],
                env={},
                start_new_session=True,
            )
            try:
                return child.wait(timeout=240)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                return 124
        finally:
            failures = []
            for home in ("public-key", "private-key", "wrong-key"):
                if (workspace / home).exists():
                    try:
                        command(
                            [tools["gpgconf"], "--homedir", str(workspace / home), "--kill", "all"]
                        )
                    except RuntimeError:
                        failures.append("key-agent:" + home)
            for identifier in ids:
                try:
                    command(
                        [tools["docker"], "rm", "--force", "--volumes", identifier],
                        environment=environment,
                    )
                    if command(
                        [
                            tools["docker"],
                            "ps",
                            "-a",
                            "--no-trunc",
                            "--filter",
                            "id=" + identifier,
                            "--format",
                            "{{.ID}}",
                        ],
                        environment=environment,
                    ):
                        raise RuntimeError("Ressource encore présente")
                    print("RESOURCE_REMOVED", identifier, flush=True)
                except RuntimeError:
                    failures.append("container:" + identifier)
            if failures:
                raise RuntimeError("Nettoyage incomplet : " + json.dumps(failures))


if __name__ == "__main__":
    raise SystemExit(main())

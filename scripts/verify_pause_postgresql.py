"""Recette pause : instance PostgreSQL 16 neuve, point local exact, nettoyage attesté."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PORT = 55514


async def checks(recipe: Any, identity: str) -> None:
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.models.alert import Alert
    from app.db.models.pause_action import PauseAction
    from app.db.repositories.alert_repo import AlertRepository
    from app.db.repositories.pause_action_repo import PauseActionRepository
    from app.domain.entities.alert import PauseActionProgress
    from app.domain.enums import AlertStatus
    from app.use_cases.pause_alert import apply_pause, reconcile
    from scripts.backup_recovery import quarantine_connection

    await recipe.verify_identity(identity)
    engine = create_async_engine(recipe.URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            alert = (await session.scalars(select(Alert).order_by(Alert.id))).first()
            assert alert is not None
            aid, uid = alert.id, alert.user_id
            issued = await PauseActionRepository(session).issue(alert)
            await session.commit()
            assert issued is not None
            token = issued.id
            independent = (await session.scalars(select(Alert).where(Alert.id != aid))).first()
            assert independent is not None
            independent_action = await PauseActionRepository(session).issue(independent)
            await session.commit()
            assert independent_action is not None
        # Le second acteur doit attendre le vrai verrou PostgreSQL, pas une barrière
        # placée après commit. Les connexions et pid serveur sont différents.
        first_ready, release_first, second_entered = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        pids: list[int] = []

        async def first() -> PauseActionProgress:
            async with maker() as session:
                pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
                await PauseActionRepository(session).acquire(token, uid)
                first_ready.set()
                await release_first.wait()
                progress = PauseActionProgress(
                    token=token, user_id=uid, update_id=11, callback_id="a"
                )
                assert await apply_pause(session, progress) == "applied"
                return progress

        async def second() -> PauseActionProgress:
            await first_ready.wait()
            async with maker() as session:
                pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
                second_entered.set()
                progress = PauseActionProgress(
                    token=token, user_id=uid, update_id=12, callback_id="b"
                )
                assert await apply_pause(session, progress) == "consumed"
                return progress

        a, b = asyncio.create_task(first()), asyncio.create_task(second())
        try:
            async with asyncio.timeout(10):
                await second_entered.wait()
                async with maker() as observer:
                    # Synchronisation déterministe par l'attente serveur réellement observée.
                    while not await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE pid=:pid AND wait_event_type='Lock')"
                        ),
                        {"pid": pids[1]},
                    ):
                        await asyncio.sleep(0.01)
                    assert not b.done()
                    isolated = PauseActionProgress(
                        token=independent_action.id,
                        user_id=uid,
                        update_id=13,
                        callback_id="c",
                    )
                    assert await apply_pause(observer, isolated) == "applied"
                    assert await observer.scalar(select(Alert.status).where(Alert.id == aid)) == (
                        AlertStatus.ACTIVE
                    )
                release_first.set()
                await asyncio.gather(a, b)
        finally:
            release_first.set()
            for task in (a, b):
                if not task.done():
                    task.cancel()
            await asyncio.gather(a, b, return_exceptions=True)
        assert len(set(pids)) == 2
        async with maker() as reader:
            alert = await reader.get(Alert, aid)
            action = await reader.get(PauseAction, token)
            assert alert is not None
            assert action is not None
            assert alert.status == AlertStatus.PAUSED
            assert alert.decision_version == action.applied_version == 1
            assert (action.update_id, action.callback_id) == (11, "a")
        # Réconciliation d'une transaction encore verrouillée, sans prétendre connaître
        # son résultat avant sa résolution. Annuler la lecture ne modifie aucune preuve.
        from unittest.mock import patch

        import app.use_cases.pause_alert as use_case

        factory_patch = patch.object(use_case, "AsyncSessionLocal", maker)
        factory_patch.start()
        try:
            async with maker() as session:
                alert = await session.get(Alert, aid)
                assert alert is not None
                await AlertRepository(session).set_status(alert, AlertStatus.ACTIVE)
                pending = await PauseActionRepository(session).issue(alert)
                await session.commit()
                assert pending is not None
            writer = maker()
            try:
                await PauseActionRepository(writer).acquire(pending.id, uid)
                progress = PauseActionProgress(
                    token=pending.id,
                    user_id=uid,
                    update_id=21,
                    callback_id="uncertain",
                    effect="unknown",
                )
                await reconcile(progress)
                assert progress.effect == "unknown"
                assert progress.result == "unknown"
                await writer.rollback()
                await reconcile(progress)
                assert str(progress.effect) == "not_applied"
                assert progress.result == "ready"
                assert not writer.dirty
            finally:
                await writer.close()
        finally:
            factory_patch.stop()
        # Preuve historique après décision plus récente : ni pause réappliquée ni succès actuel.
        async with maker() as session:
            progress = PauseActionProgress(token=token, user_id=uid, update_id=11, callback_id="a")
            assert await apply_pause(session, progress) == "replay"
            assert progress.current_status == "active"
        # Quarantaine réelle sur ce schéma : la preuve reste intacte, l'autorisation
        # non consommée est invalidée, aucune préparation ne peut activer l'alerte.
        import asyncpg

        connection = await asyncpg.connect(
            host=recipe.HOST,
            port=recipe.PORT,
            user=recipe.USER,
            password=recipe.PASSWORD,
            database=recipe.DATABASE,
        )
        try:
            before = dict(
                await connection.fetchrow("SELECT * FROM pause_actions WHERE id=$1", token)
            )
            from app.domain.entities.alert_preparation import PreparationRequest

            prep = PreparationRequest().stored()
            await connection.execute(
                "UPDATE alerts SET preparation=$1::jsonb WHERE id=$2", json.dumps(prep), aid
            )
            await quarantine_connection(connection)
            stored = json.loads(
                await connection.fetchval("SELECT preparation::text FROM alerts WHERE id=$1", aid)
            )
            assert stored["activation_requested"] is False
            assert stored["token"] != prep["token"]
            assert stored["state"] == prep["state"]
            assert (
                dict(await connection.fetchrow("SELECT * FROM pause_actions WHERE id=$1", token))
                == before
            )
            assert (
                await connection.fetchval(
                    "SELECT invalidated FROM pause_actions WHERE id=$1", pending.id
                )
                is True
            )
            await connection.execute("UPDATE alembic_version SET version_num='unsupported'")
            try:
                await quarantine_connection(connection)
            except ValueError:
                pass
            else:
                raise AssertionError("Schéma non pris en charge accepté")
            finally:
                await connection.execute(
                    "UPDATE alembic_version SET version_num='0013_pause_actions'"
                )
        finally:
            await connection.close(timeout=5)
        async with maker() as reader:
            assert (
                await reader.scalar(select(Alert.status).where(Alert.id == aid))
                == AlertStatus.PAUSED
            )
            progress = PauseActionProgress(
                token=pending.id, user_id=uid, update_id=21, callback_id="u"
            )
            assert await apply_pause(reader, progress) == "unknown"
        print(
            "PAUSE_POSTGRES_PROOF",
            json.dumps(
                {
                    "pids": pids,
                    "lock_wait_observed": True,
                    "single_mutation": True,
                    "independent_alert_proceeds": True,
                    "unresolved_unknown": True,
                    "resolved_not_applied": True,
                    "quarantine": True,
                }
            ),
            flush=True,
        )
    finally:
        await engine.dispose()


def child(identity: str) -> int:
    sys.path.insert(0, str(ROOT))
    from scripts import verify_postgresql_recipe as recipe

    recipe.PORT = PORT
    recipe.DATABASE = "radar_pause_recipe"
    recipe.URL = (
        f"postgresql+asyncpg://{recipe.USER}:{recipe.PASSWORD}@127.0.0.1:"
        f"{PORT}/{recipe.DATABASE}?ssl=disable"
    )
    counts = recipe.isolate(True)
    try:
        from alembic.config import Config

        from alembic import command

        config = Config(str(ROOT / "alembic.ini"))
        asyncio.run(recipe.verify_identity(identity))
        command.upgrade(config, "0011_notification_state")
        asyncio.run(recipe.seed_old_schema())
        command.upgrade(config, "0012_annual_observations")
        import asyncpg

        async def snapshot() -> dict[str, list[Any]]:
            connection = await asyncpg.connect(
                host=recipe.HOST,
                port=PORT,
                user=recipe.USER,
                password=recipe.PASSWORD,
                database=recipe.DATABASE,
            )
            try:
                result = {}
                for name in (
                    "users",
                    "alerts",
                    "listings",
                    "listing_matches",
                    "books",
                    "book_market_cache",
                    "market_price_snapshots",
                    "alert_books",
                ):
                    subtraction = "-'decision_version'" if name == "alerts" else ""
                    order = "alert_id, book_id" if name == "alert_books" else "id"
                    result[name] = [
                        row["data"]
                        for row in await connection.fetch(
                            f"SELECT to_jsonb(t){subtraction} AS data "
                            f"FROM {name} t ORDER BY {order}"
                        )
                    ]
                return result
            finally:
                await connection.close(timeout=5)

        before = asyncio.run(snapshot())
        command.upgrade(config, "0013_pause_actions")
        assert asyncio.run(snapshot()) == before

        async def new_fields() -> None:
            connection = await asyncpg.connect(
                host=recipe.HOST,
                port=PORT,
                user=recipe.USER,
                password=recipe.PASSWORD,
                database=recipe.DATABASE,
            )
            try:
                assert await connection.fetchval("SELECT count(*) FROM pause_actions") == 0
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM alerts WHERE decision_version<>0"
                    )
                    == 0
                )
            finally:
                await connection.close(timeout=5)

        asyncio.run(new_fields())
        print(
            "PAUSE_MIGRATION",
            json.dumps(
                {
                    "from": "0012_annual_observations",
                    "to": "0013_pause_actions",
                    "old_alerts_preserved": len(before["alerts"]),
                    "old_tables_preserved": sorted(before),
                }
            ),
            flush=True,
        )
        asyncio.run(checks(recipe, identity))
    finally:
        print("PAUSE_RECIPE_ISOLATION", json.dumps(counts), flush=True)
    assert counts["dotenv"] == counts["local_denied"] == counts["external_denied"] == 1
    assert counts["postgres_config"] == 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--local-checks", action="store_true")
    parser.add_argument("--typecheck", action="store_true")
    args = parser.parse_args()
    if args.local_checks or args.typecheck:
        sys.path.insert(0, str(ROOT))
        from scripts import verify_postgresql_recipe as recipe

        counts = recipe.isolate(False)
        try:
            if args.typecheck:
                from mypy import api

                out, err, result = api.run(
                    [
                        "--no-incremental",
                        "--explicit-package-bases",
                        "app",
                        "scripts/verify_pause_postgresql.py",
                        "scripts/backup_recovery.py",
                    ]
                )
                print(out, end="")
                print(err, end="", file=sys.stderr)
            else:
                import pytest

                result = pytest.main(
                    [
                        "-p",
                        "pytest_asyncio.plugin",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        "tests/integration/test_bd_preparation.py",
                        "tests/integration/test_isbn_removal.py",
                        "tests/unit/test_alerts_edit_workflow.py",
                        "tests/integration/test_alert_market_panel.py",
                        "tests/unit/test_alert_keyboards.py",
                        "tests/unit/test_preparation_display.py",
                        "tests/unit/test_alembic_revisions.py",
                    ]
                )
        finally:
            print("PAUSE_LOCAL_ISOLATION", json.dumps(counts))
        assert counts["dotenv"] == counts["local_denied"] == counts["external_denied"] == 1
        assert counts["postgres_config"] == counts["allowed"] == 0
        return int(result)
    if args.child:
        return child(args.child)
    sys.path.insert(0, str(ROOT))
    from scripts.backup_recovery import command
    from scripts.exercise_backup_recovery import DOCKER_SOCKET, IMAGE

    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker absent ; aucune installation autorisée")
    with tempfile.TemporaryDirectory(prefix="radar-pause-", dir="/private/tmp") as directory:
        config = Path(directory) / "docker-config"
        config.mkdir(mode=0o700)
        env = {"DOCKER_HOST": DOCKER_SOCKET, "DOCKER_CONFIG": str(config)}
        nonce = uuid.uuid4().hex
        identifier = (
            command(
                [
                    docker,
                    "create",
                    "--pull=never",
                    "--label",
                    f"botradar.exercise={nonce}",
                    "--publish",
                    f"127.0.0.1:{PORT}:5432",
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw",
                    "--env",
                    "POSTGRES_USER=radar_recipe_user",
                    "--env",
                    "POSTGRES_PASSWORD=synthetic_recipe_password",
                    "--env",
                    "POSTGRES_DB=radar_pause_recipe",
                    IMAGE,
                ],
                environment=env,
            )
            .decode()
            .strip()
        )
        try:
            command([docker, "start", identifier], environment=env)
            deadline = time.monotonic() + 30
            while True:
                try:
                    identity = (
                        command(
                            [
                                docker,
                                "exec",
                                identifier,
                                "psql",
                                "-U",
                                "radar_recipe_user",
                                "-d",
                                "radar_pause_recipe",
                                "-Atc",
                                "SELECT system_identifier FROM pg_control_system()",
                            ],
                            environment=env,
                        )
                        .decode()
                        .strip()
                    )
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.25)
            metadata = json.loads(command([docker, "inspect", identifier], environment=env))[0]
            assert metadata["Config"]["Labels"]["botradar.exercise"] == nonce
            assert metadata["Image"] == IMAGE
            assert not metadata["Mounts"]  # tmpfs seul, aucun volume ou stockage existant
            print(
                "PAUSE_RESOURCE",
                json.dumps(
                    {"id": identifier, "system_id": identity, "host": "127.0.0.1", "port": PORT}
                ),
                flush=True,
            )
            process = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--child", identity],
                env={},
                start_new_session=True,
            )
            try:
                return process.wait(timeout=120)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                return 124
        finally:
            command([docker, "rm", "--force", "--volumes", identifier], environment=env)
            remaining = command(
                [
                    docker,
                    "ps",
                    "-a",
                    "--no-trunc",
                    "--filter",
                    f"id={identifier}",
                    "--format",
                    "{{.ID}}",
                ],
                environment=env,
            ).strip()
            assert not remaining
            print("PAUSE_RESOURCE_REMOVED", identifier, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

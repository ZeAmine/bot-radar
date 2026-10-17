"""Validation finale isolée ; aucune correction ni livraison applicative."""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
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
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = "bfabcca7dbf4f8f6af2de058b167a93dd80816b8"
PORT = 55514

# Opérations métier que le planificateur appelle et que les tests d'occurrence
# substituent avant le scheduler ; liste explicite, vérifiée par résolution de nom.
OPERATIONS_METIER_SCHEDULER = (
    "recover_bd_preparations",
    "recover_notifications",
    "cleanup_old_data_task",
    "run_proactive_autotune_scan",
    "refresh_book_prices_task",
)


def _noms_globaux_referenced(fonction: Any) -> tuple[str, ...]:
    """Noms globaux qu'une fonction peut résoudre à l'exécution.

    `co_names` couvre les attributs et les globales ; on ne prétend pas en déduire
    tous les effets possibles, seulement repérer les opérations métier connues.
    """
    code = getattr(fonction, "__code__", None)
    if code is None:
        return ()
    return tuple(code.co_names)


def _identite_operations_applicatives() -> dict[str, int]:
    """Identité des opérations métier telles que leurs **modules d'origine** les portent.

    Les tests d'occurrence ne remplacent que l'attribut du module du planificateur :
    l'attribut homonyme du module d'origine reste la fonction applicative. C'est cette
    référence-là qui sert de témoin, et elle ne dépend donc pas de l'ordre entre le
    relevé et la substitution.

    Lecture seule de `sys.modules` : aucun import n'est déclenché ici, sans quoi les
    tests dont un sous-processus importe un paquet `app` temporaire seraient perturbés.

    Un nom absent du relevé n'est pas une erreur en soi : le contrôle qui l'utilise
    échoue explicitement s'il a besoin d'un témoin manquant.
    """
    origine = {
        "recover_bd_preparations": "app.use_cases.prepare_bd_alert",
        "recover_notifications": "app.tasks.recover_notifications",
        "cleanup_old_data_task": "app.tasks.cleanup",
        "run_proactive_autotune_scan": "app.tasks.proactive_autotune",
        "refresh_book_prices_task": "app.tasks.refresh_book_prices",
    }
    identites: dict[str, int] = {}
    for nom, module_nom in origine.items():
        module = sys.modules.get(module_nom)
        if module is None:
            continue
        fonction = getattr(module, nom, None)
        if fonction is not None:
            identites[nom] = id(fonction)
    return identites


def _isolated_occurrence_scheduler(
    scheduler: Any, node: str, original_operations: dict[str, int],
) -> bool:
    from inspect import iscoroutinefunction

    from apscheduler.executors.asyncio import AsyncIOExecutor
    from apscheduler.jobstores.memory import MemoryJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    if node not in {
        "tests/integration/test_scheduler_occurrence.py::" + name
        for name in (
            "test_the_real_scheduler_hands_the_intended_run_time_to_the_job",
            "test_the_transmitted_time_is_the_triggered_run_not_the_next_one",
            "test_a_delayed_run_is_still_identified_by_its_own_occurrence",
        )
    } or type(scheduler) is not AsyncIOScheduler:
        return False
    if any(type(executor) is not AsyncIOExecutor for executor in scheduler._executors.values()):
        return False
    if any(type(store) is not MemoryJobStore for store in scheduler._jobstores.values()):
        return False
    jobs = scheduler.get_jobs()
    if len(jobs) != 1 or jobs[0].id != "refresh_prices_07_00":
        return False
    function = jobs[0].func
    if not iscoroutinefunction(function):
        return False
    if function.__module__.split(".")[-1] == "test_scheduler_occurrence":
        return True
    if function.__name__ != "job_avec_occurrence":
        assert not function.__module__.startswith("app.") or (
            function.__module__ == "app.tasks.scheduler"
        ), "job pointe vers une opération applicative non substituée"
        return False
    assert function.__module__ == "app.tasks.scheduler", "fermeture d'occurrence inattendue"
    scheduler_module = sys.modules.get("app.tasks.scheduler")
    if not original_operations:
        original_operations.update(_identite_operations_applicatives())
    for reference in _noms_globaux_referenced(function):
        if reference not in OPERATIONS_METIER_SCHEDULER:
            continue
        assert reference in original_operations, (
            f"opération applicative {reference} non relevée — contrôle impossible"
        )
        current = getattr(scheduler_module, reference, None)
        assert current is not None, f"opération {reference} introuvable dans le planificateur"
        assert id(current) != original_operations[reference], (
            f"fermeture encore liée à l'opération applicative non substituée {reference}"
        )
    return True


def guard(*, site: bool = False) -> dict[str, int]:
    endpoints = json.loads(os.environ.get("RADAR_FINAL_ENDPOINTS", "[]"))
    counters = {
        "dotenv": 0,
        "network_denied": 0,
        "postgres_config": 0,
        "allowed": 0,
        "process_denied": 0,
        "service_denied": 0,
    }

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "subprocess.Popen":
            argv = args[1]
            if isinstance(argv, (list, tuple)) and argv:
                executable = Path(os.fsdecode(argv[0]))
                name = executable.name
                temporary = str(executable).startswith(
                    ("/private/tmp/", "/private/var/folders/", "/tmp/")
                )
                if name in {"ssh", "rsync", "curl", "wget", "pip", "uvicorn", "redis-server"} or (
                    name == "docker"
                    and not temporary
                    and not any(
                        value in argv
                        for value in json.loads(os.environ.get("RADAR_FINAL_BACKUP_IDS", "[]"))
                    )
                ):
                    counters["process_denied"] += 1
                    raise PermissionError("Processus externe interdit")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            write_only = (args[1] == "w" or bool(args[2] & os.O_WRONLY)) and str(
                path.resolve()
            ).startswith(("/private/tmp/", "/private/var/folders/", "/tmp/"))
            if (
                path.name.startswith(".env")
                and not (path.name == ".env.example" and path.resolve() == ROOT / ".env.example")
                and not write_only
            ):
                counters["dotenv"] += 1
                raise PermissionError("Dotenv interdit")
            if ".postgresql" in path.parts or path.name in {".pgpass", ".pg_service.conf"}:
                counters["postgres_config"] += 1
                raise PermissionError("Configuration personnelle interdite")
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            target = args[1] if event == "socket.connect" else args[:2]
            if event != "socket.sendto" and list(target) in endpoints:
                counters["allowed"] += 1
                return
            counters["network_denied"] += 1
            raise PermissionError("Transport hors recette interdit")

    sys.addaudithook(audit)
    if not site:
        with contextlib.suppress(PermissionError):
            (ROOT / ".env").open()
        for address in [("127.0.0.1", 55522), ("192.0.2.1", 9)]:
            with socket.socket() as probe, contextlib.suppress(PermissionError):
                probe.connect(address)
        assert counters["dotenv"] == 1
        assert counters["network_denied"] == 2
    try:
        from pydantic_settings import BaseSettings
    except ImportError:
        pass  # Le double Docker en Python système n'importe aucune application.
    else:
        original = BaseSettings.__init__

        def init(self: Any, *args: Any, **kwargs: Any) -> None:
            kwargs.update(_env_file=None, _secrets_dir=None)
            original(self, *args, **kwargs)

        def sources(_cls: Any, /, _settings_cls: Any, **values: Any) -> tuple[Any, ...]:
            return values["init_settings"], values["env_settings"]

        settings_type: Any = BaseSettings
        settings_type.__init__ = init
        settings_type.settings_customise_sources = classmethod(sources)
        from apscheduler.schedulers.base import BaseScheduler

        original_start = BaseScheduler.start
        permitted_schedulers = []
        # Relevé paresseux : calculé seulement si un wrapper d'occurrence est
        # effectivement inspecté, pour ne rien importer à l'installation de la garde.
        identite_operations: dict[str, int] = {}

        def forbidden(scheduler: Any, *args: Any, **kwargs: Any) -> None:
            node = os.environ.get("RADAR_FINAL_NODE", "")
            allowed = (
                "test_scheduler_cleanup_finishes_before_drain_and_client_closure",
                "test_cancelling_shutdown_does_not_recancel_the_poller_cleanup",
                "test_shutdown_cancellation_still_drains_and_closes_clients[drain]",
                "test_shutdown_cancellation_still_drains_and_closes_clients[http]",
                "test_async_job_in_flight_is_cancelled_and_cleaned_before_resource_closure",
                "test_shutdown_awaits_async_cleanup_of_a_real_scheduler_job",
                "test_caller_cancellation_during_job_cleanup_still_closes_in_order",
                "test_missing_default_executor_is_not_treated_as_no_producer",
                "test_shutdown_waits_for_a_slow_producer_then_finishes_cleanly",
            )
            if not site and node in {
                "tests/unit/test_unified_lifespan.py::" + name for name in allowed
            }:
                assert scheduler.__class__.__name__ == "AsyncIOScheduler"
                assert all(
                    job.func.__module__.endswith("test_unified_lifespan")
                    for job in scheduler.get_jobs()
                )
                permitted_schedulers.append(scheduler)
                original_start(scheduler, *args, **kwargs)
                return
            if not site and _isolated_occurrence_scheduler(
                scheduler, node, identite_operations,
            ):
                permitted_schedulers.append(scheduler)
                original_start(scheduler, *args, **kwargs)
                return
            counters["service_denied"] += 1
            raise AssertionError("Scheduler réel interdit")

        BaseScheduler.start = forbidden
        guard_namespace: Any = guard
        guard_namespace.scheduler_instances = permitted_schedulers
        # Frontières de transport uniquement : les mocks explicites des tests restent actifs.
        import httpx
        from redis.asyncio.connection import Connection
        from redis.exceptions import ConnectionError as RedisConnectionError

        async def no_redis(_self: Any, *_args: Any, **_kwargs: Any) -> None:
            raise RedisConnectionError("Redis synthétique indisponible")

        async def no_http(_self: Any, request: Any) -> Any:
            raise httpx.ConnectError("Transport synthétique non configuré", request=request)

        connection_type: Any = Connection
        transport_type: Any = httpx.AsyncHTTPTransport
        connection_type.connect = no_redis
        transport_type.handle_async_request = no_http
    if site:
        log = os.environ["RADAR_FINAL_CHILD_LOG"]

        def report() -> None:
            with open(log, "a") as stream:
                stream.write(json.dumps(counters) + "\n")

        atexit.register(report)
    return counters


def environment(mode: str) -> None:
    os.environ.clear()
    os.environ.update(
        {
            "PATH": os.defpath,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "APP_ENV": "test",
            "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "PGSSLMODE": "disable",
            "REDIS_URL": "redis://example.invalid:6379/0",
            "AI_ENABLED": "true",
            "AI_BASE_URL": "https://example.invalid",
            "AI_MODEL": "synthetic-model",
            "TELEGRAM_BOT_TOKEN": "123456789:AAG_ci_test_dummy_token_abc123",
            "TELEGRAM_ALLOWED_USER_IDS": "424242",
            "TELEGRAM_WEBHOOK_SECRET": "synthetic",
            "INGEST_API_TOKEN": "synthetic",
            "INTERNAL_API_TOKEN": "synthetic",
            "VINTED_POLLER_ENABLED": "false",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "BOTRADAR_WEBHOOK_ISOLATED": "1",
            "BOTRADAR_WEBHOOK_ROLE": "parent",
        }
    )
    if mode == "postgres":
        from scripts import verify_postgresql_recipe as recipe

        recipe.PORT = PORT
        recipe.DATABASE = "radar_pause_recipe"
        recipe.URL = (
            f"postgresql+asyncpg://{recipe.USER}:{recipe.PASSWORD}@127.0.0.1:"
            f"{PORT}/{recipe.DATABASE}?ssl=disable"
        )
        os.environ.update(
            DATABASE_URL=recipe.URL,
            TEST_DATABASE_URL=recipe.URL,
            RADAR_FINAL_ENDPOINTS=json.dumps([["127.0.0.1", PORT]]),
        )


async def migrations() -> dict[str, Any]:
    from sqlalchemy import MetaData, insert, select, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.domain.entities.alert_preparation import PreparationRequest
    from scripts import verify_postgresql_recipe as recipe

    engine = create_async_engine(recipe.URL)
    import uuid
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    uid, aid, lid, bid = [uuid.uuid4() for _ in range(4)]
    try:
        async with engine.begin() as c:
            assert await c.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0010_alert_preparation"
            )
            metadata = MetaData()
            await c.run_sync(metadata.reflect)
            t = metadata.tables
            cid = await c.scalar(select(t["categories"].c.id).limit(1))
            await c.execute(
                insert(t["users"]),
                {
                    "id": uid,
                    "telegram_user_id": 424242,
                    "is_allowed": True,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await c.execute(
                insert(t["alerts"]),
                {
                    "id": aid,
                    "user_id": uid,
                    "category_id": cid,
                    "name": "Synthétique",
                    "include_keywords": [],
                    "variants": [],
                    "exclude_keywords": [],
                    "status": "PAUSED",
                    "preparation": PreparationRequest(author_hint="Fictif").stored(),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await c.execute(
                insert(t["listings"]),
                {
                    "id": lid,
                    "source_name": "synthetic",
                    "external_id": "chain",
                    "title": "Fictif",
                    "normalized_title": "fictif",
                    "url": "https://example.invalid",
                    "category_slug": "tech",
                    "item_price_cents": 100,
                    "photos": [],
                    "raw_payload": {},
                    "received_at": now,
                },
            )
            for notified in (True, False):
                listing_id = lid
                if notified:
                    listing_id = uuid.uuid4()
                    await c.execute(
                        insert(t["listings"]),
                        {
                            "id": listing_id,
                            "source_name": "synthetic",
                            "external_id": "notified",
                            "title": "Fictif",
                            "normalized_title": "fictif",
                            "url": "https://example.invalid",
                            "category_slug": "tech",
                            "item_price_cents": 100,
                            "photos": [],
                            "raw_payload": {},
                            "received_at": now,
                        },
                    )
                await c.execute(
                    insert(t["listing_matches"]),
                    {
                        "id": uuid.uuid4(),
                        "alert_id": aid,
                        "listing_id": listing_id,
                        "keyword_match_score": 100,
                        "condition_matches": True,
                        "price_matches": True,
                        "notified_at": now if notified else None,
                        "created_at": now,
                    },
                )
            await c.execute(
                insert(t["books"]),
                {
                    "id": bid,
                    "title": "Fictif",
                    "normalized_title": "fictif",
                    "language": "fr",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await c.execute(
                insert(t["alert_books"]),
                {"alert_id": aid, "book_id": bid, "confidence": "HIGH", "created_at": now},
            )
            await c.execute(
                insert(t["book_market_cache"]),
                {
                    "id": uuid.uuid4(),
                    "book_id": bid,
                    "lowest_delivered_price_cents": 1200,
                    "historical_low_1y_cents": 200,
                    "historical_low_at": now - timedelta(days=500),
                    "checked_at": now,
                    "expires_at": now + timedelta(days=1),
                    "comparison_status": "AVAILABLE",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await c.execute(
                insert(t["market_price_snapshots"]),
                {
                    "id": uuid.uuid4(),
                    "book_id": bid,
                    "provider_name": "synthetic",
                    "item_price_cents": 1200,
                    "shipping_price_cents": 0,
                    "delivered_price_cents": 1200,
                    "currency": "EUR",
                    "source_url": "https://example.invalid",
                    "item_condition": "Très bon",
                    "checked_at": now - timedelta(days=80),
                    "is_comparable": True,
                    "confidence": "HIGH",
                    "raw_payload": {"synthetic": True},
                },
            )
            before = {
                name: [dict(row) for row in (await c.execute(select(table))).mappings()]
                for name, table in t.items()
                if name != "alembic_version"
            }
    finally:
        await engine.dispose()
    return before


async def verify_migrations(before: dict[str, Any]) -> None:
    from sqlalchemy import MetaData, select, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from scripts import verify_postgresql_recipe as recipe

    engine = create_async_engine(recipe.URL)
    try:
        async with engine.connect() as c:
            metadata = MetaData()
            await c.run_sync(metadata.reflect)
            for name, rows in before.items():
                if not rows:
                    continue
                columns = [metadata.tables[name].c[key] for key in rows[0]]
                after = [dict(row) for row in (await c.execute(select(*columns))).mappings()]
                assert sorted(map(str, after)) == sorted(map(str, rows)), name
            assert (
                await c.scalar(text("SELECT version_num FROM alembic_version"))
                == "0013_pause_actions"
            )
            assert await c.scalar(text("SELECT count(*) FROM pause_actions")) == 0
            assert (
                await c.scalar(text("SELECT count(*) FROM alerts WHERE decision_version<>0")) == 0
            )
            pending = (
                (await c.execute(text("SELECT * FROM listing_matches WHERE notified_at IS NULL")))
                .mappings()
                .one()
            )
            assert pending["notification_state"] == "unknown"
            assert pending["attempt_id"] is None
            assert pending["telegram_message_id"] is None
            assert pending["telegram_bot_id"] is None
            assert (
                await c.scalar(
                    text(
                        "SELECT count(*) FROM book_market_cache WHERE annual_low_cents IS NOT NULL"
                    )
                )
                == 0
            )
            print(
                "FINAL_MIGRATION",
                json.dumps(
                    {
                        "chain": "0010→0011→0012→0013",
                        "old_tables": sorted(before),
                        "pending_unknown_without_receipt": True,
                        "old_financial_data_preserved": True,
                        "no_pause_backfill": True,
                    }
                ),
                flush=True,
            )
    finally:
        await engine.dispose()


async def verify_empty_install(expected_revision: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from scripts import verify_postgresql_recipe as recipe

    engine = create_async_engine(recipe.URL)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == expected_revision, "Installation incomplète"
            print("FINAL_EMPTY_INSTALL", revision, flush=True)
    finally:
        await engine.dispose()


def _record_test_report(
    reports: dict[str, dict[str, str]], nodeid: str, phase: str, outcome: str
) -> None:
    """Conserve chaque rapport une fois, sans écraser un échec par un doublon."""
    assert phase in {"setup", "call", "teardown"}, "Phase de rapport pytest invalide"
    assert outcome in {"passed", "failed", "skipped"}, "Statut de rapport pytest invalide"
    phases = reports.setdefault(nodeid, {})
    assert phase not in phases, "Rapport pytest dupliqué"
    phases[phase] = outcome


def _validate_test_reports(
    collected: list[str], reports: dict[str, dict[str, str]], pytest_exit_code: int
) -> int:
    """Vérifie le protocole pytest sans convertir un verdict non nul en succès."""
    assert type(pytest_exit_code) is int, "Code de sortie pytest invalide"
    assert 0 <= pytest_exit_code <= 5, "Code de sortie pytest invalide"
    assert len(collected) == len(set(collected)), "Collecte dupliquée"
    assert set(collected) == set(reports), "Scénario collecté sans exécution ou rapport inattendu"
    assert collected or pytest_exit_code != 0, "Aucun scénario exécuté malgré un succès pytest"
    for phases in reports.values():
        assert all(value in {"passed", "failed", "skipped"} for value in phases.values()), (
            "Statut de rapport pytest invalide"
        )
        # Pytest appelle le test seulement si setup a passé, puis toujours teardown.
        expected = (
            ("setup", "call", "teardown")
            if phases.get("setup") == "passed"
            else ("setup", "teardown")
        )
        assert tuple(phases) == expected, "Scénario pytest incomplet ou phases incohérentes"
        assert pytest_exit_code != 0 or "failed" not in phases.values(), (
            "Échec pytest annoncé comme succès"
        )
    return pytest_exit_code


def suite(
    mode: str, identity: str | None = None, targets: list[str] | None = None,
    timings: Path | None = None,
) -> int:
    started = time.monotonic()
    if timings is not None:
        timings = timings.resolve()
        timings.touch(exist_ok=False)

    def record_timing(event: str, **values: Any) -> None:
        if timings is not None:
            with timings.open("a") as stream:
                stream.write(json.dumps({
                    "event": event, "elapsed": time.monotonic() - started, **values,
                }) + "\n")

    record_timing("suite_start")
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    environment(mode)
    record_timing("environment_ready")
    counts = guard()
    record_timing("guard_ready")
    if mode == "postgres":
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from alembic import command
        from scripts import verify_postgresql_recipe as recipe

        assert identity is not None
        asyncio.run(recipe.verify_identity(identity))
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        head = ScriptDirectory.from_config(config).get_current_head()
        assert head is not None
        asyncio.run(verify_empty_install(head))
        asyncio.run(recipe.reset_synthetic_schema())
        command.upgrade(config, "0010_alert_preparation")
        before = asyncio.run(migrations())
        for target in ("0011_notification_state", "0012_annual_observations", "0013_pause_actions"):
            command.upgrade(config, target)
        asyncio.run(verify_migrations(before))
        populated = asyncio.run(recipe.seed_market_freshness_migration())
        command.upgrade(config, "0014_alert_market_freshness")
        asyncio.run(recipe.verify_market_freshness_migration(populated))
    if mode == "typecheck":
        from mypy import api

        result = 0
        for targets in (
            ["app", "scripts/matching_diagnostics.py"],
            [
                "--follow-imports=silent",
                "scripts/backup_recovery.py",
                "scripts/exercise_backup_recovery.py",
                "scripts/verify_pause_postgresql.py",
                "scripts/verify_consolidation.py",
            ],
        ):
            out, err, code = api.run(["--no-incremental", "--explicit-package-bases", *targets])
            print(out, end="")
            print(err, end="", file=sys.stderr)
            result = max(result, code)
        print("FINAL_ISOLATION", counts)
        return int(result)
    import pytest

    class Reports:
        def __init__(self) -> None:
            self.collected: list[str] = []
            self.reports: dict[str, dict[str, str]] = {}

        def pytest_runtest_setup(self, item: Any) -> None:
            record_timing("test_start", nodeid=item.nodeid)
            print("FINAL_NODE", item.nodeid, flush=True)
            os.environ["RADAR_FINAL_NODE"] = item.nodeid

            def timeout(_signum: int, _frame: Any) -> None:
                raise TimeoutError("Échéance diagnostique de 20 s ; scénario non validé")

            signal.signal(signal.SIGALRM, timeout)
            signal.alarm(20)

        def pytest_runtest_teardown(self, item: Any) -> None:
            assert item.nodeid
            signal.alarm(0)
            for scheduler in getattr(guard, "scheduler_instances", []):
                assert not scheduler.running
                assert all(
                    task.done()
                    for executor in scheduler._executors.values()
                    for task in getattr(executor, "_pending_futures", ())
                )

        def pytest_collection_finish(self, session: Any) -> None:
            self.collected = [node.nodeid for node in session.items]
            record_timing("collection_done", collected=len(self.collected))

        def pytest_runtest_logreport(self, report: Any) -> None:
            _record_test_report(self.reports, report.nodeid, report.when, report.outcome)
            record_timing(
                "test_phase", nodeid=report.nodeid, phase=report.when,
                outcome=report.outcome, duration=report.duration,
            )

    complete_selection = not targets
    reports = Reports()
    with tempfile.TemporaryDirectory(prefix="radar-final-guard-", dir="/tmp") as directory:
        workspace = Path(directory)
        child_log = workspace / "children.jsonl"
        os.environ["RADAR_FINAL_CHILD_LOG"] = str(child_log)
        os.environ["PYTHONPATH"] = directory
        bootstrap = (
            "import importlib.util\n"
            "s=importlib.util.spec_from_file_location('radar_final_guard', "
            f"{str(Path(__file__).resolve())!r})\n"
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.guard(site=True)\n"
        )
        (workspace / "sitecustomize.py").write_text(bootstrap)
        targets = targets or (
            ["tests/integration"] if mode == "postgres" else ["tests/unit", "tests/performance"]
        )
        record_timing("pytest_start")
        result = int(
            pytest.main(
                ["-p", "pytest_asyncio.plugin", "-q", "-p", "no:cacheprovider", *targets],
                plugins=[reports],
            )
        )
        record_timing("pytest_done", exit_code=result)
        children = (
            [json.loads(line) for line in child_log.read_text().splitlines()]
            if child_log.exists()
            else []
        )
    summary = {
        "backend": mode,
        "pytest_exit_code": result,
        "collected": len(reports.collected),
        "passed": sum(
            p.get("call") == "passed" and p.get("teardown") == "passed"
            for p in reports.reports.values()
        ),
        "failed": sum("failed" in p.values() for p in reports.reports.values()),
        "not_applicable": sum("skipped" in p.values() for p in reports.reports.values()),
        "deselected": 0,
        "isolation": counts,
        "children": children,
        "nodes": reports.collected,
        "reports": reports.reports,
    }
    if complete_selection:
        baseline = json.loads((ROOT / "docs/consolidation-nodeids.json").read_text())
        prefix = (
            "tests/integration/" if mode == "postgres" else ("tests/unit/", "tests/performance/")
        )
        expected = {node for node in baseline if node.startswith(prefix)}
        actual = set(reports.collected)
        summary["missing_baseline"] = sorted(expected - actual)
        summary["added_nodes"] = sorted(actual - expected)
        assert len(reports.collected) == len(actual), "Collecte dupliquée"
    summary["scheduler_started"] = len(getattr(guard, "scheduler_instances", []))
    print("FINAL_SUITE_REPORT", json.dumps(summary, sort_keys=True), flush=True)
    assert not summary.get("missing_baseline"), "Nodeids de référence manquants"
    result = _validate_test_reports(reports.collected, reports.reports, result)
    assert counts["dotenv"] == 1
    assert counts["network_denied"] == 2
    assert counts["process_denied"] == 0
    assert counts["postgres_config"] == 0
    assert all(
        row["dotenv"] == row["network_denied"] == row["postgres_config"] == 0 for row in children
    )
    record_timing("suite_done", exit_code=result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("unit", "postgres", "typecheck", "backup"))
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--identity")
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--target", action="append")
    parser.add_argument("--timings", type=Path)
    args = parser.parse_args()
    if args.timings is not None and args.mode != "unit":
        parser.error("Les mesures de phases sont réservées au mode unit")
    if args.child:
        return (
            backup_child(args.attestation)
            if args.mode == "backup"
            else suite(args.mode, args.identity, args.target, args.timings)
        )
    if args.mode == "backup":
        return backup_parent()
    if args.mode != "postgres":
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                args.mode,
                "--child",
                *[arg for target in (args.target or []) for arg in ("--target", target)],
                *(["--timings", str(args.timings)] if args.timings is not None else []),
            ],
            env={},
            start_new_session=True,
        )
        try:
            return process.wait(timeout=240)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return 124
    return postgres_parent(args.target)


def postgres_parent(targets: list[str] | None = None) -> int:
    import shutil
    import time
    import uuid

    sys.path.insert(0, str(ROOT))
    from scripts.backup_recovery import command
    from scripts.exercise_backup_recovery import DOCKER_SOCKET, IMAGE

    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker absent")
    with tempfile.TemporaryDirectory(prefix="radar-final-pg-", dir="/private/tmp") as directory:
        config = Path(directory) / "docker-config"
        config.mkdir(mode=0o700)
        env = {"DOCKER_HOST": DOCKER_SOCKET, "DOCKER_CONFIG": str(config)}
        nonce = uuid.uuid4().hex
        cid = (
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
            command([docker, "start", cid], environment=env)
            deadline = time.monotonic() + 30
            while True:
                try:
                    identity = (
                        command(
                            [
                                docker,
                                "exec",
                                cid,
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
            metadata = json.loads(command([docker, "inspect", cid], environment=env))[0]
            assert metadata["Config"]["Labels"]["botradar.exercise"] == nonce
            assert metadata["Image"] == IMAGE
            assert not metadata["Mounts"]
            print(
                "FINAL_RESOURCE",
                json.dumps({"id": cid, "system_id": identity, "port": PORT}),
                flush=True,
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__).resolve()),
                    "postgres",
                    "--child",
                    "--identity",
                    identity,
                    *[arg for target in (targets or []) for arg in ("--target", target)],
                ],
                env={},
                start_new_session=True,
            )
            try:
                return process.wait(timeout=240)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                return 124
        finally:
            command([docker, "rm", "--force", "--volumes", cid], environment=env)
            assert not command(
                [docker, "ps", "-a", "--no-trunc", "--filter", f"id={cid}", "--format", "{{.ID}}"],
                environment=env,
            ).strip()
            print("FINAL_RESOURCE_REMOVED", cid, flush=True)


def backup_parent() -> int:
    sys.path.insert(0, str(ROOT))
    from scripts.exercise_backup_recovery import DOCKER_SOCKET, IMAGE

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
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__).resolve()),
                    "backup",
                    "--child",
                    "--attestation",
                    str(attestation),
                ],
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


def backup_child(path: Path) -> int:
    data = json.loads(path.read_bytes())
    sys.path.insert(0, str(ROOT))
    environment("backup")
    os.environ["RADAR_FINAL_ENDPOINTS"] = json.dumps(
        [["127.0.0.1", row["port"]] for row in data["endpoints"]]
    )
    os.environ["RADAR_FINAL_BACKUP_IDS"] = json.dumps(
        [row["container"] for row in data["endpoints"]]
    )
    counts = guard()
    from scripts import backup_recovery as tool
    from scripts.exercise_backup_recovery import exercise

    original_backup, original_restore = tool.backup, tool.restore
    seeded = False
    expected = {}

    async def seed_and_backup(source: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal seeded
        if not seeded:
            from sqlalchemy import select
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            from app.db.models.alert import Alert
            from app.db.repositories.alert_repo import AlertRepository
            from app.db.repositories.pause_action_repo import PauseActionRepository
            from app.domain.entities.alert import PauseActionProgress
            from app.domain.enums import AlertStatus
            from app.use_cases.pause_alert import apply_pause

            url = (
                f"postgresql+asyncpg://{source.user}:{source.password}@127.0.0.1:"
                f"{source.port}/{source.database}?ssl=disable"
            )
            engine = create_async_engine(url)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with maker() as session:
                    alert = (
                        await session.scalars(
                            select(Alert).where(Alert.status == AlertStatus.ACTIVE)
                        )
                    ).first()
                    assert alert is not None
                    repo = PauseActionRepository(session)
                    action = await repo.issue(alert)
                    assert action is not None
                    await session.commit()
                    progress = PauseActionProgress(
                        token=action.id,
                        user_id=alert.user_id,
                        update_id=123,
                        callback_id="synthetic-backup-proof",
                    )
                    assert await apply_pause(session, progress) == "applied"
                    await AlertRepository(session).set_status(alert, AlertStatus.ACTIVE)
                    await session.commit()
                    assert await repo.issue(alert) is not None
                    await session.commit()
                connection = await source.connect()
                try:
                    expected.update(
                        {
                            str(row["id"]): dict(row)
                            for row in await connection.fetch("SELECT * FROM pause_actions")
                        }
                    )
                finally:
                    await connection.close(timeout=5)
            finally:
                await engine.dispose()
            seeded = True
        return await original_backup(source, *args, **kwargs)

    async def restore_and_check(archive: Any, target: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original_restore(archive, target, *args, **kwargs)
        connection = await target.connect()
        try:
            for row in await connection.fetch("SELECT * FROM pause_actions"):
                old = expected[str(row["id"])]
                if old["applied_at"] is not None:
                    assert dict(row) == old
                else:
                    assert row["invalidated"] is True
            assert (
                await connection.fetchval("SELECT count(*) FROM alerts WHERE status='ACTIVE'") == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM alerts WHERE preparation->>'activation_requested'='true'"
                )
                == 0
            )
        finally:
            await connection.close(timeout=5)
        return result

    tool_namespace: Any = tool
    tool_namespace.backup, tool_namespace.restore = seed_and_backup, restore_and_check
    try:
        result = asyncio.run(exercise(data))
        print(
            "FINAL_BACKUP",
            json.dumps(
                {
                    "schema": tool.SCHEMA,
                    "pause_proofs": len(expected),
                    "actions_invalidated": True,
                    "historical_proof_preserved": True,
                    **result,
                },
                sort_keys=True,
            ),
        )
    finally:
        tool_namespace.backup, tool_namespace.restore = original_backup, original_restore
        print("FINAL_BACKUP_ISOLATION", counts)
    assert counts["dotenv"] == 1
    assert counts["network_denied"] == 2
    assert counts["postgres_config"] == counts["process_denied"] == 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

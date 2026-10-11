"""Vérifie le contrat corrigé de pause, avec référence historique explicite.

Exécution isolée, AVANT les imports de conftest/configuration :
    .venv/bin/python -B tests/integration/test_telegram_webhook_transient_failure.py

Pytest découvre le parent, qui exige tous les scénarios dans un sous-processus
isolé. Le lanceur protège aussi les imports du parent : un simple pytest dans le
dépôt charge conftest avant les fixtures et ne garantit pas cette isolation préalable.
SQLite sur fichier permet une connexion de lecture indépendante ; ni les verrous
PostgreSQL ni une panne réelle du serveur ne sont caractérisés ici.
"""

import asyncio
import importlib
import json
import logging
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

HISTORICAL_SCENARIOS = (
    "control", "before_first_commit", "after_flush", "after_business_commit",
    "ambiguous_business_commit",
)
OPEN_BOUNDARIES = (
    "card_edit_after_confirmation", "final_middleware_commit", "inadmissible_before_lock",
)
CONTRACT_SCENARIOS = (
    "cancel_business_commit", "cancel_confirmation", "cancel_card_edit", "cancel_final_commit",
    "repository_default", "repository_strict", "isolated_sequential_progress",
    "isolated_concurrent_progress", "non_pause_callback", "unauthorized", "foreign_alert",
    "pause_preparation",
    "safe_diagnostics",
)
SCENARIOS = HISTORICAL_SCENARIOS + OPEN_BOUNDARIES + CONTRACT_SCENARIOS
# Observations acceptées AVANT correction, pas des attentes du code corrigé.
HISTORICAL_RESULTS = {
    "control": {"status": "ok"},
    **{scenario: {"status": "error", "message": "Erreur interne traitée"}
       for scenario in HISTORICAL_SCENARIOS[1:]},
    "card_edit_after_confirmation": {"status": "error", "message": "Erreur interne traitée"},
    "final_middleware_commit": {"status": "error", "message": "Erreur interne traitée"},
    "inadmissible_before_lock": {"status": "ok"},
}
CHILD_REPORT_PREFIX = "WEBHOOK_CHILD_REPORT "
CHILD_TIMEOUT_SECONDS = 30


def _child_nodeid(scenario: str) -> str:
    test_name = (
        "test_pause_open_boundaries"
        if scenario in OPEN_BOUNDARIES else
        "test_pause_contract_guards" if scenario in CONTRACT_SCENARIOS else
        "test_pause_webhook_and_identical_replay"
    )
    return f"tests/integration/test_telegram_webhook_transient_failure.py::{test_name}[{scenario}]"


def _run_isolated_tests(*, child: bool = False, typecheck: bool = False) -> int:
    """Établit les barrières avant pytest, sans fichier de configuration supplémentaire."""
    root = Path(__file__).resolve().parents[2]
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.clear()
    os.environ.update({
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "BOTRADAR_WEBHOOK_ISOLATED": "1",
        "BOTRADAR_WEBHOOK_ROLE": "child" if child else "parent",
        "APP_ENV": "test",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "TEST_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://example.invalid:6379/0",
        "TELEGRAM_BOT_TOKEN": "123456789:AAG_ci_test_dummy_token_abc123",
        "TELEGRAM_WEBHOOK_SECRET": "synthetic_webhook_secret",
        "INGEST_API_TOKEN": "synthetic_ingest_token",
        "INTERNAL_API_TOKEN": "synthetic_internal_token",
        "TELEGRAM_ALLOWED_USER_IDS": "424242",
        "AI_ENABLED": "false",
        "AI_BASE_URL": "https://example.invalid",
        "AI_MODEL": "synthetic-model",
        "VINTED_POLLER_ENABLED": "false",
    })
    blocked = {"dotenv": 0, "network": 0}

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if (
            event == "open"
            and isinstance(args[0], (str, bytes, os.PathLike))
            and Path(os.fsdecode(args[0])).name.startswith(".env")
        ):
            blocked["dotenv"] += 1
            raise PermissionError("Lecture dotenv interdite par le lanceur")
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            blocked["network"] += 1
            raise PermissionError("Transport réseau réel interdit par le lanceur")

    sys.addaudithook(audit)
    # Prouver que les barrières sont actives, sans lire ni contacter leur cible.
    try:
        with (root / ".env").open():
            raise AssertionError("Barrière dotenv inactive")
    except PermissionError:
        pass
    with socket.socket() as probe:
        try:
            probe.connect(("192.0.2.1", 9))
            raise AssertionError("Barrière réseau inactive")
        except PermissionError:
            pass
    assert blocked == {"dotenv": 1, "network": 1}

    from pydantic_settings import BaseSettings

    original_init = BaseSettings.__init__

    def no_dotenv(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.update(_env_file=None, _secrets_dir=None)
        original_init(self, *args, **kwargs)

    def only_synthetic_sources(
        _cls: Any, _settings_cls: Any, **sources: Any
    ) -> tuple[Any, ...]:
        return sources["init_settings"], sources["env_settings"]

    BaseSettings.__init__ = no_dotenv
    BaseSettings.settings_customise_sources = classmethod(only_synthetic_sources)

    # Le lifespan n'est pas utilisé ; un démarrage accidentel échoue avant de démarrer.
    from apscheduler.schedulers.base import BaseScheduler

    def no_scheduler(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Démarrage scheduler interdit")

    BaseScheduler.start = no_scheduler
    if typecheck:
        from mypy import api

        stdout, stderr, result = api.run(["--no-incremental", "app"])
        print(stdout, end="")
        print(stderr, end="", file=sys.stderr)
        print("ISOLATION", json.dumps(blocked, sort_keys=True))
        return result if blocked == {"dotenv": 1, "network": 1} else 1
    import pytest

    class CompletionReports:
        """Rapports pytest effectifs, y compris setup, skip et teardown en échec."""

        def __init__(self) -> None:
            self.reports: dict[str, dict[str, list[str]]] = {}
            self.evidence: list[dict[str, Any]] = []

        def pytest_runtest_logreport(self, report: Any) -> None:
            phases = self.reports.setdefault(report.nodeid, {})
            phases.setdefault(report.when, []).append(report.outcome)
            if report.when == "call":
                self.evidence.extend(
                    value for key, value in report.user_properties if key == "webhook_evidence"
                )

    completion = CompletionReports()
    targets = (
        [
            "tests/integration/test_telegram_webhook_transient_failure.py"
            "::test_pause_webhook_and_identical_replay",
            "tests/integration/test_telegram_webhook_transient_failure.py"
            "::test_pause_open_boundaries",
            "tests/integration/test_telegram_webhook_transient_failure.py"
            "::test_pause_contract_guards",
        ]
        if child else [
            "tests/integration/test_telegram_webhook_transient_failure.py",
            "tests/integration/test_telegram_webhook.py",
        ]
    )
    result = pytest.main([
        "-p", "pytest_asyncio.plugin", "-v", "-s", "-p", "no:cacheprovider",
        *targets,
    ], plugins=[completion])
    print("ISOLATION", json.dumps(blocked, sort_keys=True))
    if child:
        print(CHILD_REPORT_PREFIX + json.dumps({
            "reports": completion.reports,
            "evidence": completion.evidence,
            "isolation": blocked,
        }, ensure_ascii=False, sort_keys=True))
    # Aucun accès supplémentaire tenté, même si un code applicatif l'avait absorbé.
    return int(result) if blocked == {"dotenv": 1, "network": 1} else 1


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--child"], ["--typecheck"]):
        raise SystemExit("Seuls --child et --typecheck sont acceptés")
    raise SystemExit(_run_isolated_tests(
        child=sys.argv[1:] == ["--child"], typecheck=sys.argv[1:] == ["--typecheck"],
    ))

pytest = importlib.import_module("pytest")


def _corrected_response(scenario: str) -> dict[str, str]:
    """Contrat corrigé : les preuves durables restent vérifiées séparément du JSON."""
    if scenario == "before_first_commit":
        return {"status": "error", "action": "not_started", "message": "Erreur interne traitée"}
    if scenario in {"after_flush", "ambiguous_business_commit"}:
        return {"status": "error", "action": "unknown", "message": "Erreur interne traitée"}
    if scenario in {
        "after_business_commit", "card_edit_after_confirmation", "final_middleware_commit",
    }:
        return {"status": "ok", "action": "committed", "post_action": "error"}
    return {"status": "ok", "action": "not_applied" if scenario == "inadmissible_before_lock"
            else "committed"}


def _require_complete_child(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Un exit zéro sans les preuves de toutes les exécutions n'est pas un succès."""
    assert result.returncode == 0, (
        f"Échec du sous-processus ({result.returncode}) : {result.stdout}"
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith(CHILD_REPORT_PREFIX)]
    assert len(lines) == 1, "Rapport enfant absent ou multiple"
    report = json.loads(lines[0][len(CHILD_REPORT_PREFIX):])
    expected_nodes = {_child_nodeid(scenario) for scenario in SCENARIOS}
    assert set(report["reports"]) == expected_nodes, "Scénarios enfant manquants ou inattendus"
    for phases in report["reports"].values():
        assert phases == {
            "setup": ["passed"], "call": ["passed"], "teardown": ["passed"],
        }, "Scénario enfant échoué, ignoré ou incomplet"
    evidence = report["evidence"]
    assert len(evidence) == len(SCENARIOS), "Preuves de caractérisation incomplètes"
    assert {row["scenario"] for row in evidence} == set(SCENARIOS)
    assert report["isolation"] == {"dotenv": 1, "network": 1}, "Barrières enfant violées"
    return report


def test_webhook_characterization_in_isolated_subprocess() -> None:
    """Parent découvert normalement ; délai borné et preuve d'exécution obligatoires."""
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--child"]
    try:
        result = subprocess.run(
            command, cwd=Path(__file__).resolve().parents[2], env={},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=CHILD_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("Timeout du sous-processus de caractérisation") from exc
    report = _require_complete_child(result)
    print("PARENT_VERIFIED", json.dumps({
        "executed_scenarios": sorted(row["scenario"] for row in report["evidence"]),
        "isolation": report["isolation"],
    }, sort_keys=True))
    print("AMBIGUOUS_COMMIT_VERIFIED", json.dumps(
        next(row for row in report["evidence"] if row["scenario"] == "ambiguous_business_commit"),
        ensure_ascii=False, sort_keys=True,
    ))
    for row in report["evidence"]:
        if row["scenario"] in OPEN_BOUNDARIES:
            print("OPEN_BOUNDARY_VERIFIED", json.dumps(row, ensure_ascii=False, sort_keys=True))


@pytest.mark.parametrize("fault", [
    "exit_failure", "timeout", "no_execution", "missing_ambiguous_commit",
    *[f"missing_{scenario}" for scenario in OPEN_BOUNDARIES],
])
def test_parent_rejects_incomplete_subprocess(monkeypatch: Any, fault: str) -> None:
    """Injecte à la frontière subprocess ; le vrai test parent doit lever."""
    calls = []

    def faulty_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["timeout"] == CHILD_TIMEOUT_SECONDS
        assert kwargs["env"] == {}
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, CHILD_TIMEOUT_SECONDS)
        if fault == "exit_failure":
            return subprocess.CompletedProcess(command, 1, "échec enfant synthétique")
        if fault.startswith("missing_"):
            missing = (
                "ambiguous_business_commit"
                if fault == "missing_ambiguous_commit" else fault.removeprefix("missing_")
            )
            # Tous les autres scénarios réussis ne compensent pas une absence.
            nodes = {
                _child_nodeid(scenario): {
                    "setup": ["passed"], "call": ["passed"], "teardown": ["passed"],
                }
                for scenario in SCENARIOS if scenario != missing
            }
            incomplete = {
                "reports": nodes,
                "evidence": [
                    {"scenario": scenario} for scenario in SCENARIOS if scenario != missing
                ],
                "isolation": {"dotenv": 1, "network": 1},
            }
            return subprocess.CompletedProcess(
                command, 0, CHILD_REPORT_PREFIX + json.dumps(incomplete)
            )
        # Même un rapport présent et un exit zéro échouent sans scénarios exécutés.
        empty = {"reports": {}, "evidence": [], "isolation": {"dotenv": 1, "network": 1}}
        return subprocess.CompletedProcess(command, 0, CHILD_REPORT_PREFIX + json.dumps(empty))

    monkeypatch.setattr(subprocess, "run", faulty_run)
    expected = {
        "exit_failure": "Échec du sous-processus", "timeout": "Timeout du sous-processus",
        "no_execution": "Scénarios enfant manquants",
        "missing_ambiguous_commit": "Scénarios enfant manquants",
        **{f"missing_{scenario}": "Scénarios enfant manquants" for scenario in OPEN_BOUNDARIES},
    }[fault]
    with pytest.raises(AssertionError, match=expected):
        test_webhook_characterization_in_isolated_subprocess()
    assert len(calls) == 1


@pytest.fixture
async def pause_journey(monkeypatch: Any, tmp_path: Path) -> AsyncIterator[Any]:
    """Vrais routeur/dispatcher/middlewares/repos ; seuls les transports sont doublés."""
    if os.environ.get("BOTRADAR_WEBHOOK_ISOLATED") != "1":
        pytest.skip("Caractérisation à exécuter avec le lanceur isolé décrit en tête du module")
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.methods import AnswerCallbackQuery, EditMessageText
    from aiogram.types import Chat, Message
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.routes import telegram
    from app.bot.bot_instance import dp
    from app.core.config import settings
    from app.core.rate_limiter import SlidingWindowRateLimiter
    from app.db.models.alert import Alert
    from app.db.models.category import Category
    from app.db.models.user import User
    from app.db.repositories.alert_repo import AlertRepository
    from app.domain.enums import AlertStatus
    from app.main import app
    from tests.database import postgres_replayed_database

    # Réutiliser le propriétaire du schéma, mais imposer une base locale par scénario.
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pause.sqlite'}")
    async with postgres_replayed_database() as db:
        db.maker.configure(autoflush=False)
        user = User(telegram_user_id=424242, first_name="Avant", is_allowed=True)
        category = Category(slug="tech", label="Tech", emoji="T", is_active=True)
        db.session.add_all([user, category])
        await db.session.flush()
        alert = Alert(
            user_id=user.id, category_id=category.id, name="Console synthétique",
            status=AlertStatus.ACTIVE, include_keywords=["console"], preparation=None,
        )
        db.session.add(alert)
        await db.session.commit()
        alert_id, user_id = alert.id, user.id
        trace = SimpleNamespace(
            failure=None, injected=0, commits=0, rollbacks=0, redis_calls=0,
            attempts=[], delivered=[], boundary=None, local_status=None,
            before_business_commit=None, business_commit_applied=False, business_caller=None,
            final_commit_caller=None, competition_started=False, competing_commits=0,
            before_competition=None, pause_lock_results=[], stale_status=None,
            no_pause_mutation_confirmed=False,
            invocations=[], propagated_frames=[], confirmation_blocked=False,
            confirmation_started=asyncio.Event(), release_confirmation=asyncio.Event(),
        )

        async def durable(writer: AsyncSession | None = None) -> tuple[str, str, int]:
            async with db.maker() as reader:
                assert reader is not writer
                if writer is not None:
                    reader_conn = await reader.connection()
                    writer_conn = await writer.connection()
                    assert reader_conn.sync_connection is not writer_conn.sync_connection
                    assert (
                        reader_conn.sync_connection.connection.dbapi_connection
                        is not writer_conn.sync_connection.connection.dbapi_connection
                    )
                status = await reader.scalar(select(Alert.status).where(Alert.id == alert_id))
                name = await reader.scalar(select(User.first_name).where(User.id == user_id))
                ids = list((await reader.execute(select(Alert.id))).scalars())
                return status.name, name, len(ids)

        original_commit, original_rollback = AsyncSession.commit, AsyncSession.rollback

        async def commit(session: AsyncSession) -> None:
            trace.commits += 1
            caller_name = sys._getframe(1).f_code.co_name
            if (
                trace.failure == "cancel_business_commit" and caller_name == "toggle_pause_alert"
            ) or (
                trace.failure == "cancel_final_commit" and trace.commits == 3
            ):
                trace.boundary = await durable(session)
                trace.injected += 1
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            if trace.failure == "inadmissible_before_lock" and trace.commits == 2:
                assert not session.dirty
                assert not session.new
                assert not session.deleted
                assert await session.scalar(
                    select(Alert.status).where(Alert.id == alert_id)
                ) == AlertStatus.DELETED
                trace.no_pause_mutation_confirmed = True
            if trace.failure == "final_middleware_commit" and trace.commits == 3:
                caller = sys._getframe(1)
                assert caller.f_code.co_name == "__call__"
                assert Path(caller.f_code.co_filename).as_posix().endswith(
                    "/app/bot/middlewares/auth.py"
                )
                assert len(trace.delivered) == 3  # Le handler et sa présentation sont terminés.
                assert not session.dirty
                assert not session.new
                assert not session.deleted
                trace.final_commit_caller = [caller.f_code.co_name, caller.f_lineno]
                trace.boundary = await durable(session)
                trace.injected += 1
                raise SQLAlchemyError("Échec synthétique du commit final après retour du handler")
            fail_here = (
                trace.failure == "before_first_commit" and trace.commits == 1
            ) or (trace.failure == "after_flush" and trace.commits == 2)
            if fail_here:
                trace.local_status = await session.scalar(
                    select(Alert.status).where(Alert.id == alert_id)
                )
                trace.boundary = await durable(session)
                trace.injected += 1
                raise SQLAlchemyError("Panne transitoire SQL synthétique avant commit")
            ambiguous = trace.failure == "ambiguous_business_commit" and trace.commits == 2
            if ambiguous:
                # Identifier l'appel réel, pas seulement son rang après la synchronisation.
                caller = sys._getframe(1)
                assert caller.f_code.co_name == "toggle_pause_alert"
                assert Path(caller.f_code.co_filename).as_posix().endswith(
                    "/app/bot/handlers/alerts_manage.py"
                )
                trace.business_caller = caller.f_code.co_name
                trace.local_status = await session.scalar(
                    select(Alert.status).where(Alert.id == alert_id)
                )
                trace.before_business_commit = await durable(session)
            await original_commit(session)
            if ambiguous:
                # Le véritable commit a rendu la main au harness, mais pas au handler.
                trace.business_commit_applied = True
                trace.boundary = await durable(session)
                trace.injected += 1
                raise SQLAlchemyError("Retour de commit métier échoué après application réelle")

        async def rollback(session: AsyncSession) -> None:
            trace.rollbacks += 1
            await original_rollback(session)

        original_lock = AlertRepository.lock_for_edit

        async def lock_for_edit(repo: Any, current: Any, **kwargs: Any) -> bool:
            caller = sys._getframe(1).f_code.co_name
            if (
                trace.failure == "inadmissible_before_lock"
                and caller == "set_status" and not trace.competition_started
            ):
                # La lecture initiale du handler a eu lieu ; le vrai verrou n'est pas acquis.
                assert current.status == AlertStatus.ACTIVE
                trace.before_competition = await durable(repo.session)
                trace.competition_started = True
                async with db.maker() as competitor:
                    await durable(competitor)  # Connexion distincte aussi pour cet acteur.
                    competing_conn = await competitor.connection()
                    handler_conn = await repo.session.connection()
                    assert (
                        competing_conn.sync_connection.connection.dbapi_connection
                        is not handler_conn.sync_connection.connection.dbapi_connection
                    )
                    other_repo = AlertRepository(competitor)
                    other = await other_repo.get_by_id(alert_id)
                    assert other is not current
                    # Vraie règle et vraie écriture, dans un ordre concurrent contrôlé.
                    await other_repo.soft_delete(other)
                    assert other.status == AlertStatus.DELETED
                    assert other.deleted_at is not None
                    await original_commit(competitor)
                    trace.competing_commits += 1
                trace.injected += 1
            admitted = await original_lock(repo, current, **kwargs)
            if caller == "set_status" and trace.competition_started:
                trace.pause_lock_results.append(admitted)
                trace.stale_status = current.status.name
                trace.boundary = await durable(repo.session)
            return admitted

        async def fake_telegram(_session: Any, _bot: Any, method: Any, **_kwargs: Any) -> Any:
            if isinstance(method, AnswerCallbackQuery):
                effect = ("callback", method.text)
            elif isinstance(method, EditMessageText):
                effect = ("card", method.text)
            else:
                raise AssertionError(f"Transport Telegram inattendu : {type(method).__name__}")
            trace.attempts.append(effect)
            if trace.failure == "safe_diagnostics":
                raise TelegramNetworkError(
                    method=method, message="synthetic-sensitive-response-should-not-be-logged"
                )
            if (
                trace.failure == "concurrent_confirmation"
                and isinstance(method, AnswerCallbackQuery)
                and method.text == "⏸ Alerte mise en pause." and not trace.confirmation_blocked
            ):
                trace.confirmation_blocked = True
                trace.confirmation_started.set()
                await trace.release_confirmation.wait()
                trace.injected += 1
                raise TelegramNetworkError(method=method, message="Présentation A synthétique")
            if (
                trace.failure == "cancel_confirmation" and isinstance(method, AnswerCallbackQuery)
            ) or (
                trace.failure == "cancel_card_edit" and isinstance(method, EditMessageText)
            ):
                trace.boundary = await durable()
                trace.injected += 1
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            if trace.failure == "after_business_commit":
                assert isinstance(method, AnswerCallbackQuery)
                assert method.text == "⏸ Alerte mise en pause."
                trace.boundary = await durable()
                trace.injected += 1
                raise TelegramNetworkError(method=method, message="Panne synthétique Telegram")
            if (
                trace.failure == "card_edit_after_confirmation"
                and isinstance(method, EditMessageText)
            ):
                assert trace.delivered == [
                    ("callback", "⏸ Alerte mise en pause."), ("callback", None),
                ]
                trace.boundary = await durable()
                trace.injected += 1
                raise TelegramNetworkError(method=method, message="Édition synthétique échouée")
            trace.delivered.append(effect)
            if isinstance(method, EditMessageText):
                return Message(
                    message_id=101, date=datetime.now(UTC),
                    chat=Chat(id=424242, type="private"), text=method.text,
                    reply_markup=method.reply_markup,
                )
            return True

        async def redis_execute() -> list[Any]:
            trace.redis_calls += 1
            return [0, 1, 1, True]

        redis_transport = MagicMock()
        redis_transport.pipeline.return_value.execute = AsyncMock(side_effect=redis_execute)
        monkeypatch.setattr(
            SlidingWindowRateLimiter, "_get_redis_client", lambda _self: redis_transport
        )
        memory = MemoryStorage()
        monkeypatch.setattr(dp.fsm, "storage", memory)
        monkeypatch.setattr("app.bot.middlewares.auth.AsyncSessionLocal", db.maker)
        monkeypatch.setattr(AsyncSession, "commit", commit)
        monkeypatch.setattr(AsyncSession, "rollback", rollback)
        monkeypatch.setattr(AlertRepository, "lock_for_edit", lock_for_edit)
        monkeypatch.setattr(AiohttpSession, "make_request", fake_telegram)
        assert telegram.dp is dp  # Aucun faux feed_update/handler/repository.
        original_feed = dp.feed_update

        async def observe_feed(*args: Any, **kwargs: Any) -> Any:
            # Observer le vrai dispatch, sans remplacer ses handlers ni leurs résultats.
            progress = kwargs.get("pause_progress")
            trace.invocations.append(progress)
            try:
                return await original_feed(*args, **kwargs)
            except BaseException as exc:
                trace.propagated_frames = []
                traceback = exc.__traceback__
                while traceback:
                    trace.propagated_frames.append((
                        traceback.tb_frame.f_code.co_name,
                        Path(traceback.tb_frame.f_code.co_filename).as_posix(),
                    ))
                    traceback = traceback.tb_next
                raise

        monkeypatch.setattr(dp, "feed_update", observe_feed)
        payload = {
            "update_id": 77001,
            "callback_query": {
                "id": "synthetic-callback-77001", "chat_instance": "synthetic-chat",
                "from": {"id": 424242, "is_bot": False, "first_name": "Après"},
                "data": f"al_pause:{alert_id}",
                "message": {
                    "message_id": 101, "date": 1725900000,
                    "chat": {"id": 424242, "type": "private"}, "text": "Carte initiale",
                },
            },
        }
        body = json.dumps(payload, ensure_ascii=False).encode()
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.12", 1234)),
            base_url="http://test",
        ) as client:
            async def send(custom_payload: Any = None) -> Any:
                # Mêmes octets, update_id, callback id et message id au rejeu.
                return await client.post(
                    f"/api/v1/telegram/webhook/{settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()}",
                    content=body if custom_payload is None else json.dumps(custom_payload).encode(),
                    headers={"Content-Type": "application/json"},
                )

            async def direct_send() -> Any:
                return await telegram.handle_telegram_webhook(
                    settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value(), json.loads(body),
                )

            yield SimpleNamespace(
                trace=trace, durable=durable, send=send, direct_send=direct_send,
                maker=db.maker, alert_id=alert_id, payload=payload,
            )
        await memory.close()


@pytest.mark.parametrize("failure", [
    pytest.param(None, id="control"),
    *[pytest.param(scenario, id=scenario) for scenario in HISTORICAL_SCENARIOS[1:]],
])
async def _characterize_pause_webhook_and_identical_replay(
    pause_journey: Any, failure: str | None, caplog: Any, request: Any
) -> None:
    """Contrat corrigé ; les réponses historiques restent référencées dans la preuve."""
    journey, trace = pause_journey, pause_journey.trace
    assert await journey.durable() == ("ACTIVE", "Avant", 1)
    trace.failure = failure
    caplog.set_level(logging.ERROR, logger="app.api.routes.telegram")
    response = await journey.send()
    assert response.status_code == 200
    assert response.json() == _corrected_response(failure or "control")
    expected_state = {
        None: ("PAUSED", "Après", 1),
        "before_first_commit": ("ACTIVE", "Avant", 1),
        "after_flush": ("ACTIVE", "Après", 1),
        "after_business_commit": ("PAUSED", "Après", 1),
        "ambiguous_business_commit": ("PAUSED", "Après", 1),
    }[failure]
    assert await journey.durable() == expected_state
    assert trace.injected == int(failure is not None)
    assert trace.commits == {
        None: 3, "before_first_commit": 1, "after_flush": 2, "after_business_commit": 2,
        "ambiguous_business_commit": 2,
    }[failure]
    assert trace.rollbacks == int(failure in {
        "after_flush", "after_business_commit", "ambiguous_business_commit",
    })
    assert trace.redis_calls == (1 if failure == "before_first_commit" else 2)
    if failure:
        assert trace.boundary == expected_state
        if failure == "after_flush":
            assert trace.local_status.name == "PAUSED"  # UPDATE flushé, non durable.
        elif failure == "before_first_commit":
            assert trace.local_status.name == "ACTIVE"
        elif failure == "ambiguous_business_commit":
            assert trace.business_caller == "toggle_pause_alert"
            assert trace.before_business_commit == ("ACTIVE", "Après", 1)
            assert trace.local_status.name == "PAUSED"
            assert trace.business_commit_applied is True
            assert response.json()["action"] == "unknown"
        errors = [record for record in caplog.records if record.name == "app.api.routes.telegram"]
        assert len(errors) == 1
        assert errors[0].exc_info is None  # Les messages d'erreur externes ne sont plus loggés.
        assert errors[0].funcName == "handle_telegram_webhook"
        assert errors[0].action == response.json()["action"]
        frames = [name for name, _path in trace.propagated_frames]
        files = [path for _name, path in trace.propagated_frames]
        assert "feed_update" in frames
        assert "__call__" in frames
        assert any(path.endswith("/app/bot/middlewares/auth.py") for path in files)
        if failure != "before_first_commit":
            assert "toggle_pause_alert" in frames
    assert len(trace.delivered) == (3 if failure is None else 0)
    assert len(trace.attempts) == (
        3 if failure is None else 1 if failure == "after_business_commit" else 0
    )
    first_effects = list(trace.delivered)
    first_attempts = list(trace.attempts)
    first_commits, first_rollbacks, first_redis = trace.commits, trace.rollbacks, trace.redis_calls
    trace.failure = None
    injected_before_replay = trace.injected
    replay = await journey.send()
    assert replay.status_code == 200
    assert replay.json() == _corrected_response("control")
    assert trace.injected == injected_before_replay
    assert trace.commits == first_commits + 3
    assert trace.rollbacks == first_rollbacks
    assert trace.redis_calls == first_redis + 2
    assert await journey.durable() == ("PAUSED", "Après", 1)
    replay_effects = trace.delivered[len(first_effects):]
    assert len(replay_effects) == 3
    assert replay_effects[0] == ("callback", "⏸ Alerte mise en pause.")
    assert replay_effects[1] == ("callback", None)
    assert replay_effects[2][0] == "card"
    assert "pause" in replay_effects[2][1].lower()
    if failure == "ambiguous_business_commit":
        assert trace.before_business_commit == ("ACTIVE", "Après", 1)
        assert trace.boundary == ("PAUSED", "Après", 1)
        assert first_attempts == []
        assert first_effects == []
        assert len(trace.attempts) == 3
    request.node.user_properties.append(("webhook_evidence", {
        "scenario": failure or "control", "http": response.status_code,
        "body_status": response.json()["status"], "durable": expected_state,
        "injections": trace.injected, "delivered_first": len(first_effects),
        "commits_attempted_first": first_commits, "explicit_rollbacks_first": first_rollbacks,
        "redis_calls_first": first_redis, "commits_attempted_replay": 3,
        "http_replay": replay.status_code, "delivered_replay": len(replay_effects),
        "durable_replay": await journey.durable(),
        "response_first": response.json(), "response_replay": replay.json(),
        "historical_response_first": HISTORICAL_RESULTS[failure or "control"],
        "before_business_commit": trace.before_business_commit,
        "business_commit_applied": trace.business_commit_applied,
        "business_caller": trace.business_caller,
        "attempts_first": len(first_attempts),
        "attempts_replay": len(trace.attempts) - len(first_attempts),
    }))


@pytest.mark.parametrize("failure", OPEN_BOUNDARIES)
async def _characterize_pause_open_boundaries(
    pause_journey: Any, failure: str, caplog: Any, request: Any
) -> None:
    """Vérifie les frontières corrigées en préservant leurs preuves de durabilité."""
    journey, trace = pause_journey, pause_journey.trace
    assert await journey.durable() == ("ACTIVE", "Avant", 1)
    caplog.set_level(logging.ERROR, logger="app.api.routes.telegram")
    trace.failure = failure
    response = await journey.send()
    no_mutation = failure == "inadmissible_before_lock"
    expected_state = ("DELETED" if no_mutation else "PAUSED", "Après", 1)
    assert response.status_code == 200
    assert response.json() == _corrected_response(failure)
    assert await journey.durable() == expected_state
    assert trace.boundary == expected_state
    assert trace.injected == 1
    assert trace.commits == (3 if failure == "final_middleware_commit" else 2)
    assert trace.rollbacks == (0 if no_mutation else 1)
    assert trace.redis_calls == 2
    if not no_mutation:
        assert trace.attempts[0] == ("callback", "⏸ Alerte mise en pause.")
        assert trace.attempts[1] == ("callback", None)
        assert len(trace.attempts) == 3
        assert trace.attempts[2][0] == "card"
        assert len(trace.delivered) == (2 if failure == "card_edit_after_confirmation" else 3)
        assert trace.delivered[:2] == trace.attempts[:2]
    errors = [record for record in caplog.records if record.name == "app.api.routes.telegram"]
    if no_mutation:
        assert errors == []
        assert trace.before_competition == ("ACTIVE", "Après", 1)
        assert trace.competing_commits == 1
        assert trace.pause_lock_results == [False]
        assert trace.stale_status == "ACTIVE"
        assert trace.no_pause_mutation_confirmed is True
        assert trace.delivered == [("callback", "Alerte introuvable.")]
        assert trace.attempts == trace.delivered
    else:
        assert len(errors) == 1
        assert errors[0].funcName == "handle_telegram_webhook"
        assert errors[0].exc_info is None
        assert errors[0].action == "committed"
        frames = [name for name, _path in trace.propagated_frames]
        assert "feed_update" in frames
        assert "__call__" in frames
        if failure == "card_edit_after_confirmation":
            assert "view_single_alert" in frames
            assert "toggle_pause_alert" in frames
        else:
            assert "toggle_pause_alert" not in frames  # Handler déjà revenu, commit de middleware.
            assert trace.final_commit_caller[0] == "__call__"

    first_commits, first_rollbacks = trace.commits, trace.rollbacks
    first_attempts, first_delivered = list(trace.attempts), list(trace.delivered)
    trace.failure = None
    replay = await journey.send()
    assert replay.status_code == 200
    assert replay.json() == _corrected_response(
        "inadmissible_before_lock" if no_mutation else "control"
    )
    assert trace.injected == 1
    assert trace.commits == first_commits + (2 if no_mutation else 3)
    assert trace.rollbacks == first_rollbacks
    assert trace.redis_calls == 4
    assert await journey.durable() == expected_state
    replay_attempts = trace.attempts[len(first_attempts):]
    replay_delivered = trace.delivered[len(first_delivered):]
    assert len(replay_attempts) == (1 if no_mutation else 3)
    assert replay_delivered == replay_attempts
    if not no_mutation:
        assert replay_delivered[:2] == [
            ("callback", "⏸ Alerte mise en pause."), ("callback", None),
        ]
        assert replay_delivered[2][0] == "card"
    if no_mutation:
        assert trace.competing_commits == 1
        assert trace.pause_lock_results == [False, False]
        assert replay_delivered == [("callback", "Alerte introuvable.")]
    else:
        assert "pause" in replay_delivered[2][1].lower()
    request.node.user_properties.append(("webhook_evidence", {
        "scenario": failure, "http": response.status_code, "response_first": response.json(),
        "historical_response_first": HISTORICAL_RESULTS[failure],
        "durable": expected_state, "boundary": trace.boundary,
        "injections": trace.injected, "commits_attempted_first": first_commits,
        "explicit_rollbacks_first": first_rollbacks,
        "attempts_first": len(first_attempts), "delivered_first": len(first_delivered),
        "confirmation_first": first_delivered[0][1],
        "card_delivered_first": len(first_delivered) == 3,
        "http_replay": replay.status_code, "response_replay": replay.json(),
        "durable_replay": await journey.durable(),
        "attempts_replay": len(replay_attempts), "delivered_replay": len(replay_delivered),
        "pause_lock_results": trace.pause_lock_results,
        "before_competition": trace.before_competition,
        "competing_commits": trace.competing_commits,
        "final_commit_caller": trace.final_commit_caller,
        "no_pause_mutation_confirmed": trace.no_pause_mutation_confirmed,
    }))


@pytest.mark.parametrize("scenario", CONTRACT_SCENARIOS)
async def _verify_pause_contract_guards(
    pause_journey: Any, scenario: str, request: Any, caplog: Any
) -> None:
    """Compatibilité, annulation et suivi serveur sans contamination entre invocations."""
    from app.core.exceptions import EntityNotFoundError
    from app.db.models.alert import Alert
    from app.db.models.user import User
    from app.db.repositories.alert_repo import AlertRepository
    from app.domain.enums import AlertStatus

    journey, trace = pause_journey, pause_journey.trace
    assert await journey.durable() == ("ACTIVE", "Avant", 1)
    if scenario.startswith("cancel_"):
        trace.failure = scenario
        task = asyncio.create_task(journey.direct_send())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert trace.injected == 1
        expected = ("ACTIVE" if scenario == "cancel_business_commit" else "PAUSED", "Après", 1)
        assert await journey.durable() == expected
        assert trace.invocations[0].effect == (
            "unknown" if scenario == "cancel_business_commit" else "committed"
        )
        assert len(trace.delivered) == {
            "cancel_business_commit": 0, "cancel_confirmation": 0,
            "cancel_card_edit": 2, "cancel_final_commit": 3,
        }[scenario]
        trace.failure = None
        assert await journey.direct_send() == _corrected_response("control")
        assert await journey.durable() == ("PAUSED", "Après", 1)
        assert trace.invocations[0] is not trace.invocations[1]
        assert trace.invocations[0].effect == (
            "unknown" if scenario == "cancel_business_commit" else "committed"
        )
    elif scenario.startswith("repository_"):
        async with journey.maker() as session:
            repo = AlertRepository(session)
            current = await repo.get_by_id(journey.alert_id)
            await repo.soft_delete(current)
            await session.commit()
            if scenario == "repository_default":
                assert await repo.set_status(current, AlertStatus.PAUSED) is current
            else:
                with pytest.raises(EntityNotFoundError):
                    await repo.set_status(current, AlertStatus.PAUSED, require_editable=True)
            assert not session.dirty
        assert await journey.durable() == ("DELETED", "Avant", 1)
        assert trace.attempts == []
    elif scenario == "isolated_sequential_progress":
        trace.failure = "after_flush"
        forged = {**journey.payload, "pause_progress": {"effect": "committed"}}
        first = await journey.send(forged)
        assert first.json() == _corrected_response("after_flush")
        assert await journey.durable() == ("ACTIVE", "Après", 1)
        trace.failure = None
        second = await journey.send(forged)
        assert second.json() == _corrected_response("control")
        assert await journey.durable() == ("PAUSED", "Après", 1)
        assert trace.invocations[0] is not trace.invocations[1]
        assert trace.invocations[0].effect == "unknown"
        assert trace.invocations[1].effect == "committed"
    elif scenario == "isolated_concurrent_progress":
        trace.failure = "concurrent_confirmation"
        first_task = asyncio.create_task(journey.send())
        try:
            await asyncio.wait_for(trace.confirmation_started.wait(), timeout=5)
            assert await journey.durable() == ("PAUSED", "Après", 1)
            second = await journey.send()
            assert second.json() == _corrected_response("control")
            trace.release_confirmation.set()
            first = await first_task
            assert first.json() == _corrected_response("after_business_commit")
            assert await journey.durable() == ("PAUSED", "Après", 1)
            assert trace.invocations[0] is not trace.invocations[1]
            assert trace.invocations[0].stage == "confirmation"
            assert trace.invocations[1].stage == "handler_returned"
        finally:
            trace.release_confirmation.set()
            if not first_task.done():
                first_task.cancel()
            await asyncio.gather(first_task, return_exceptions=True)
    elif scenario == "non_pause_callback":
        trace.failure = "before_first_commit"
        payload = {**journey.payload, "callback_query": {
            **journey.payload["callback_query"], "data": f"al_view:{journey.alert_id}",
        }}
        response = await journey.send(payload)
        assert response.status_code == 200
        assert response.json() == {"status": "error", "message": "Erreur interne traitée"}
        assert trace.invocations == [None]
        assert await journey.durable() == ("ACTIVE", "Avant", 1)
    elif scenario == "unauthorized":
        payload = {**journey.payload, "callback_query": {
            **journey.payload["callback_query"],
            "from": {"id": 999999, "is_bot": False, "first_name": "Interdit"},
        }}
        response = await journey.send(payload)
        assert response.json() == {"status": "ok"}
        assert trace.delivered == [("callback", "⛔ Ce bot est privé.")]
        assert trace.invocations[0].effect == "not_started"
        assert await journey.durable() == ("ACTIVE", "Avant", 1)
    elif scenario == "foreign_alert":
        async with journey.maker() as session:
            other = User(telegram_user_id=666666, first_name="Autre", is_allowed=True)
            session.add(other)
            await session.flush()
            current = await session.get(Alert, journey.alert_id)
            current.user_id = other.id
            await session.commit()
        response = await journey.send()
        assert response.json() == _corrected_response("inadmissible_before_lock")
        assert trace.delivered == [("callback", "Alerte introuvable.")]
        assert await journey.durable() == ("ACTIVE", "Après", 1)
    elif scenario == "safe_diagnostics":
        from app.core.logging import JSONFormatter

        caplog.set_level(logging.ERROR, logger="app.api.routes.telegram")
        trace.failure = scenario
        response = await journey.send()
        assert response.json() == _corrected_response("after_business_commit")
        assert await journey.durable() == ("PAUSED", "Après", 1)
        errors = [record for record in caplog.records if record.name == "app.api.routes.telegram"]
        assert len(errors) == 1
        assert errors[0].error_type == "TelegramNetworkError"
        assert errors[0].stage == "confirmation"
        assert errors[0].exc_info is None
        assert "synthetic-sensitive-response" not in caplog.text
        assert "synthetic-sensitive-response" not in JSONFormatter().format(errors[0])
    else:
        assert scenario == "pause_preparation"
        from app.domain.entities.alert_preparation import PreparationRequest

        pending = PreparationRequest().stored()
        async with journey.maker() as session:
            current = await session.get(Alert, journey.alert_id)
            current.status = AlertStatus.PAUSED
            current.preparation = pending
            await session.commit()
        response = await journey.send()
        assert response.json() == _corrected_response("control")
        async with journey.maker() as reader:
            current = await reader.get(Alert, journey.alert_id)
            assert current.status == AlertStatus.PAUSED
            assert current.preparation["activation_requested"] is False
            assert current.preparation["token"] != pending["token"]
    request.node.user_properties.append(("webhook_evidence", {"scenario": scenario}))


if os.environ.get("BOTRADAR_WEBHOOK_ROLE") == "child":
    test_pause_webhook_and_identical_replay = _characterize_pause_webhook_and_identical_replay
    test_pause_open_boundaries = _characterize_pause_open_boundaries
    test_pause_contract_guards = _verify_pause_contract_guards

"""Le lanceur conserve les verdicts pytest sans inventer une phase call après un skip."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import verify_consolidation as runner


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", ["0014_alert_market_freshness", "0013_pause_actions", None])
async def test_empty_install_reports_only_the_revision_read_from_database(
    monkeypatch, capsys, actual,
):
    from unittest.mock import AsyncMock, MagicMock

    import sqlalchemy.ext.asyncio

    engine = MagicMock()
    engine.dispose = AsyncMock()
    connection = engine.connect.return_value.__aenter__.return_value
    connection.scalar = AsyncMock(return_value=actual)
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", lambda _url: engine)
    if actual == "0014_alert_market_freshness":
        await runner.verify_empty_install(actual)
        assert capsys.readouterr().out == "FINAL_EMPTY_INSTALL 0014_alert_market_freshness\n"
    else:
        with pytest.raises(AssertionError, match="Installation incomplète"):
            await runner.verify_empty_install("0014_alert_market_freshness")
        assert "FINAL_EMPTY_INSTALL" not in capsys.readouterr().out
    engine.dispose.assert_awaited_once()


async def _probe_scheduler_rejection(defect):
    import os

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler_type = type("OtherScheduler", (AsyncIOScheduler,), {})
    scheduler = scheduler_type() if defect == "subclass" else AsyncIOScheduler()

    async def isolated_job():
        pass

    def synchronous_job():
        pass

    job = synchronous_job if defect == "synchronous" else isolated_job
    job.__module__ = "test_scheduler_occurrence" if defect != "origin" else "app.tasks.scheduler"
    scheduler.add_job(
        job, "interval", seconds=60,
        id="wrong" if defect == "job_id" else "refresh_prices_07_00",
        max_instances=1, coalesce=True,
    )
    if defect == "extra_job":
        scheduler.add_job(isolated_job, "interval", seconds=60, id="unrelated")
    if defect == "executor":
        scheduler._executors["default"] = object()
    if defect == "jobstore":
        scheduler._jobstores["default"] = object()
    if defect == "production":
        from app.tasks.scheduler import create_scheduler

        scheduler = create_scheduler()
    node = (
        "tests/integration/test_scheduler_occurrence.py::"
        "test_the_real_scheduler_hands_the_intended_run_time_to_the_job"
    )
    os.environ["RADAR_FINAL_NODE"] = node if defect != "node" else "unauthorized"
    with pytest.raises(AssertionError, match="Scheduler réel interdit"):
        scheduler.start(paused=True)
    assert not scheduler.running


@pytest.mark.parametrize("defect", [
    "node", "origin", "job_id", "extra_job", "executor", "jobstore", "synchronous", "subclass",
    "production",
])
def test_occurrence_permission_rejects_non_isolated_schedulers(defect):
    bootstrap = (
        "import asyncio,json,sys\n"
        f"sys.path.insert(0,{str(runner.ROOT)!r})\n"
        "from scripts import verify_consolidation as runner\n"
        "runner.environment('unit')\n"
        "counts=runner.guard()\n"
        "from tests.unit.test_verify_consolidation import _probe_scheduler_rejection\n"
        f"asyncio.run(_probe_scheduler_rejection({defect!r}))\n"
        "print('H1_REJECTION_REPORT',json.dumps(counts))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", bootstrap], cwd=runner.ROOT, env={},
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(next(
        line.removeprefix("H1_REJECTION_REPORT ") for line in completed.stdout.splitlines()
        if line.startswith("H1_REJECTION_REPORT ")
    ))
    assert report == {
        "dotenv": 1, "network_denied": 2, "postgres_config": 0,
        "allowed": 0, "process_denied": 0, "service_denied": 1,
    }


async def _probe_scheduler_transports():
    import asyncio
    import os
    import socket
    from datetime import UTC, datetime

    import httpx
    from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from redis.asyncio.connection import Connection
    from redis.exceptions import ConnectionError as RedisConnectionError

    scheduler = AsyncIOScheduler()
    finished, stopped = asyncio.Event(), asyncio.Event()
    failures = []

    async def isolated_job():
        try:
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.ConnectError, match="Transport synthétique"):
                    await client.get("https://example.invalid")
            with pytest.raises(RedisConnectionError, match="Redis synthétique"):
                await Connection(host="example.invalid").connect()
            with socket.socket() as connection, pytest.raises(PermissionError):
                connection.connect(("192.0.2.1", 9))
            child = subprocess.run(
                [sys.executable, "-B", "-c", (
                    "import asyncio,socket\n"
                    "from apscheduler.schedulers.asyncio import AsyncIOScheduler\n"
                    "try:\n"
                    "    socket.socket().connect(('192.0.2.1',9))\n"
                    "except PermissionError:\n"
                    "    print('network-denied')\n"
                    "else:\n"
                    "    raise AssertionError('network permitted')\n"
                    "async def check_scheduler():\n"
                    "    try:\n"
                    "        AsyncIOScheduler().start()\n"
                    "    except AssertionError as error:\n"
                    "        assert 'Scheduler réel interdit' in str(error)\n"
                    "        print('scheduler-denied')\n"
                    "    else:\n"
                    "        raise AssertionError('scheduler permitted')\n"
                    "asyncio.run(check_scheduler())\n"
                )], capture_output=True, text=True, timeout=10, check=False,
            )
            assert child.returncode == 0, child.stdout + child.stderr
            assert "network-denied" in child.stdout
            assert "scheduler-denied" in child.stdout
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    isolated_job.__module__ = "test_scheduler_occurrence"
    scheduler.add_job(
        isolated_job, "date", run_date=datetime.now(UTC), id="refresh_prices_07_00",
        max_instances=1, coalesce=True,
    )
    scheduler.add_listener(lambda _event: stopped.set(), EVENT_SCHEDULER_SHUTDOWN)
    os.environ["RADAR_FINAL_NODE"] = (
        "tests/integration/test_scheduler_occurrence.py::"
        "test_the_real_scheduler_hands_the_intended_run_time_to_the_job"
    )
    try:
        scheduler.start()
        await asyncio.wait_for(finished.wait(), timeout=15)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
            await asyncio.wait_for(stopped.wait(), timeout=5)
    assert failures == []


def test_permitted_scheduler_keeps_transport_and_child_guards():
    site = (
        "import sys\n"
        f"sys.path.insert(0,{str(runner.ROOT)!r})\n"
        "from scripts.verify_consolidation import guard\n"
        "guard(site=True)\n"
    )
    bootstrap = (
        "import asyncio,json,os,tempfile\n"
        "from pathlib import Path\n"
        f"import sys;sys.path.insert(0,{str(runner.ROOT)!r})\n"
        "from scripts import verify_consolidation as runner\n"
        "runner.environment('unit')\n"
        "counts=runner.guard()\n"
        "from tests.unit.test_verify_consolidation import _probe_scheduler_transports\n"
        "with tempfile.TemporaryDirectory(prefix='radar-h1-guard-',dir='/tmp') as directory:\n"
        "    workspace=Path(directory)\n"
        f"    (workspace/'sitecustomize.py').write_text({site!r})\n"
        "    child_log=workspace/'children.jsonl'\n"
        "    os.environ['PYTHONPATH']=directory\n"
        "    os.environ['RADAR_FINAL_CHILD_LOG']=str(child_log)\n"
        "    asyncio.run(_probe_scheduler_transports())\n"
        "    children=[json.loads(line) for line in child_log.read_text().splitlines()]\n"
        "    print('H1_TRANSPORT_REPORT',json.dumps({'parent':counts,'children':children}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", bootstrap], cwd=runner.ROOT, env={},
        capture_output=True, text=True, timeout=25, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(next(
        line.removeprefix("H1_TRANSPORT_REPORT ") for line in completed.stdout.splitlines()
        if line.startswith("H1_TRANSPORT_REPORT ")
    ))
    assert report["parent"] == {
        "dotenv": 1, "network_denied": 3, "postgres_config": 0,
        "allowed": 0, "process_denied": 0, "service_denied": 0,
    }
    assert report["children"] == [{
        "dotenv": 0, "network_denied": 1, "postgres_config": 0,
        "allowed": 0, "process_denied": 0, "service_denied": 1,
    }]


@pytest.mark.parametrize(
    ("phases", "code"),
    [
        ({"setup": "passed", "call": "passed", "teardown": "passed"}, 0),
        ({"setup": "skipped", "teardown": "passed"}, 0),
        ({"setup": "passed", "call": "skipped", "teardown": "passed"}, 0),
        ({"setup": "failed", "teardown": "passed"}, 1),
        ({"setup": "passed", "call": "failed", "teardown": "passed"}, 1),
        ({"setup": "passed", "call": "passed", "teardown": "failed"}, 1),
        ({"setup": "skipped", "teardown": "failed"}, 1),
        ({"setup": "passed", "call": "passed", "teardown": "skipped"}, 0),
    ],
)
def test_valid_phases_preserve_pytest_exit_code(phases, code):
    assert runner._validate_test_reports(["sample"], {"sample": phases}, code) == code


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_collection_abort_internal_error_interrupt_and_empty_run_never_become_success(code):
    assert runner._validate_test_reports([], {}, code) == code


@pytest.mark.parametrize(
    "phases",
    [
        {},
        {"setup": "passed"},
        {"setup": "passed", "teardown": "passed"},
        {"setup": "skipped"},
        {"setup": "failed"},
        {"setup": "skipped", "call": "passed", "teardown": "passed"},
        {"setup": "failed", "call": "passed", "teardown": "passed"},
        {"call": "passed", "setup": "passed", "teardown": "passed"},
        {"setup": "passed", "call": "passed", "teardown": "passed", "other": "passed"},
        {"setup": "passed", "call": "unknown", "teardown": "passed"},
        {"setup": "unknown", "teardown": "passed"},
        {"setup": "passed", "call": "passed", "teardown": "unknown"},
    ],
)
def test_incomplete_impossible_and_unknown_reports_are_rejected(phases):
    with pytest.raises(AssertionError, match="pytest|Scénario"):
        runner._validate_test_reports(["sample"], {"sample": phases}, 1)


@pytest.mark.parametrize(
    ("collected", "reports", "code"),
    [
        ([], {}, 0),
        (["sample"], {}, 0),
        ([], {"sample": {"setup": "skipped", "teardown": "passed"}}, 0),
        (["sample", "sample"], {"sample": {"setup": "skipped", "teardown": "passed"}}, 0),
        (["sample"], {"sample": {"setup": "failed", "teardown": "passed"}}, 0),
        ([], {}, 6),
    ],
)
def test_missing_extra_duplicate_reports_and_false_success_are_rejected(collected, reports, code):
    with pytest.raises(AssertionError, match="pytest|Scénario|Collecte|scénario"):
        runner._validate_test_reports(collected, reports, code)


def test_duplicate_phase_cannot_overwrite_a_failure():
    reports = {"sample": {"setup": "passed", "call": "failed"}}
    with pytest.raises(AssertionError, match="dupliqué"):
        runner._record_test_report(reports, "sample", "call", "passed")
    assert reports == {"sample": {"setup": "passed", "call": "failed"}}


@pytest.mark.parametrize(("phase", "outcome"), [("collect", "passed"), ("setup", "unknown")])
def test_unknown_report_is_rejected_before_storage(phase, outcome):
    reports = {}
    with pytest.raises(AssertionError, match="pytest"):
        runner._record_test_report(reports, "sample", phase, outcome)
    assert reports == {}


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("def test_sample():\n    assert True\n", 0),
        (
            "import pytest\n@pytest.mark.skip(reason='synthetic')\n"
            "def test_sample():\n    raise AssertionError('must not run')\n",
            0,
        ),
        ("import pytest\ndef test_sample():\n    pytest.skip('synthetic')\n", 0),
        (
            "import pytest\n@pytest.fixture\ndef resource():\n    raise RuntimeError('setup')\n"
            "def test_sample(resource):\n    assert resource\n",
            1,
        ),
        ("def test_sample():\n    raise AssertionError('call')\n", 1),
        (
            "import pytest\n@pytest.fixture\ndef resource():\n    yield 1\n"
            "    raise RuntimeError('teardown')\n"
            "def test_sample(resource):\n    assert resource == 1\n",
            1,
        ),
        ("raise RuntimeError('collection')\n", 2),
        ("value = 1\n", 5),
    ],
    ids=[
        "passed",
        "skip_setup",
        "skip_call",
        "failed_setup",
        "failed_call",
        "failed_teardown",
        "collection_error",
        "no_tests",
    ],
)
def test_recipe_preserves_pytest_exit_codes(tmp_path, source, code):
    target = tmp_path / "test_synthetic_recipe.py"
    target.write_text(source)
    timings = tmp_path / "timings.jsonl"
    completed = subprocess.run(
        [sys.executable, "-B", str(Path(runner.__file__)), "unit", "--target", str(target),
         "--timings", str(timings)],
        cwd=runner.ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == code, completed.stdout + completed.stderr
    summary = json.loads(
        next(
            line.removeprefix("FINAL_SUITE_REPORT ")
            for line in completed.stdout.splitlines()
            if line.startswith("FINAL_SUITE_REPORT ")
        )
    )
    assert summary["pytest_exit_code"] == code
    measurements = [json.loads(line) for line in timings.read_text().splitlines()]
    assert [entry["elapsed"] for entry in measurements] == sorted(
        entry["elapsed"] for entry in measurements
    )
    assert measurements[0]["event"] == "suite_start"
    assert measurements[-1] == {
        "event": "suite_done", "elapsed": measurements[-1]["elapsed"], "exit_code": code,
    }
    phases = [entry for entry in measurements if entry["event"] == "test_phase"]
    assert len(phases) == sum(len(value) for value in summary["reports"].values())
    for entry in phases:
        assert entry["duration"] >= 0
        assert summary["reports"][entry["nodeid"]][entry["phase"]] == entry["outcome"]
    if "must not run" in source:
        assert list(summary["reports"].values()) == [{"setup": "skipped", "teardown": "passed"}]


def test_relative_timing_path_stays_in_the_invocation_directory(tmp_path):
    target = tmp_path / "test_timing_location.py"
    target.write_text("def test_sample():\n    assert True\n")
    completed = subprocess.run(
        [sys.executable, "-B", str(Path(runner.__file__)), "unit", "--target", str(target),
         "--timings", "timings.jsonl"],
        cwd=tmp_path, env={}, capture_output=True, text=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    measurements = [
        json.loads(line) for line in (tmp_path / "timings.jsonl").read_text().splitlines()
    ]
    assert measurements[0]["event"] == "suite_start"
    assert measurements[-1]["event"] == "suite_done"
    assert measurements[-1]["exit_code"] == 0


def test_recipe_works_without_the_macos_private_tmp_directory(tmp_path):
    target = tmp_path / "test_portable_recipe.py"
    target.write_text("def test_sample():\n    assert True\n")
    bootstrap = (
        "import runpy,sys,tempfile\n"
        "original=tempfile.TemporaryDirectory\n"
        "def portable(*args,**kwargs):\n"
        "    if kwargs.get('dir') == '/private/tmp':\n"
        "        raise FileNotFoundError('synthetic: macOS directory absent')\n"
        "    return original(*args,**kwargs)\n"
        "tempfile.TemporaryDirectory=portable\n"
        f"sys.argv=[{runner.__file__!r},'unit','--child','--target',{str(target)!r}]\n"
        f"runpy.run_path({runner.__file__!r},run_name='__main__')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", bootstrap],
        cwd=runner.ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads(
        next(
            line.removeprefix("FINAL_SUITE_REPORT ")
            for line in completed.stdout.splitlines()
            if line.startswith("FINAL_SUITE_REPORT ")
        )
    )
    assert summary["pytest_exit_code"] == 0
    assert summary["passed"] == 1
    assert summary["isolation"]["dotenv"] == 1
    assert summary["isolation"]["network_denied"] == 2


def _executer_recette_sur(tmp_path: Path, nom: str, source: str) -> dict:
    target = tmp_path / nom
    target.write_text(source)
    completed = subprocess.run(
        [sys.executable, "-B", str(Path(runner.__file__)), "unit", "--target", str(target)],
        cwd=runner.ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "FINAL_SUITE_REPORT " in completed.stdout, completed.stdout + completed.stderr
    return json.loads(
        next(
            line.removeprefix("FINAL_SUITE_REPORT ")
            for line in completed.stdout.splitlines()
            if line.startswith("FINAL_SUITE_REPORT ")
        )
    )


_SCHEDULER_HORS_CONTEXTE = """
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler


@pytest.mark.asyncio
async def test_scheduler_hors_contexte_autorise():
    scheduler = AsyncIOScheduler()
    with pytest.raises(AssertionError, match="Scheduler réel interdit"):
        scheduler.start()
"""


def test_un_scheduler_reel_hors_contexte_autorise_reste_refuse(tmp_path):
    """Le refus par défaut doit survivre à l'autorisation des tests d'occurrence."""
    summary = _executer_recette_sur(
        tmp_path, "test_scheduler_refuse.py", _SCHEDULER_HORS_CONTEXTE
    )

    assert summary["pytest_exit_code"] == 0
    assert summary["passed"] == 1
    assert summary["scheduler_started"] == 0
    assert summary["isolation"]["service_denied"] == 1


def test_un_node_non_autorise_ne_demarre_pas_de_scheduler():
    """Un nom de test voisin, dans le fichier réellement autorisé, n'obtient rien.

    L'allowlist est nominative : un autre test du même fichier ne doit pas hériter de
    l'autorisation. Le fichier autorisé est donc utilisé tel quel, et le node voisin
    est découvert depuis la recette.
    """
    bootstrap = (
        "import asyncio, json, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "from scripts import verify_consolidation as runner\n"
        "runner.environment('unit')\n"
        "os.environ['RADAR_FINAL_NODE'] = (\n"
        "    'tests/integration/test_scheduler_occurrence.py::test_voisin_non_autorise'\n"
        ")\n"
        "counters = runner.guard(site=False)\n"
        "from apscheduler.schedulers.asyncio import AsyncIOScheduler\n"
        "async def essai():\n"
        "    scheduler = AsyncIOScheduler()\n"
        "    try:\n"
        "        scheduler.start()\n"
        "    except AssertionError as exc:\n"
        "        print('REFUS', exc)\n"
        "    else:\n"
        "        print('DEMARRE')\n"
        "asyncio.run(essai())\n"
        "print('COUNTERS', json.dumps(counters))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", bootstrap],
        cwd=runner.ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    sortie = completed.stdout + completed.stderr
    assert "DEMARRE" not in sortie, sortie
    assert "REFUS" in sortie, sortie
    assert "Scheduler réel interdit" in sortie, sortie
    assert '"service_denied": 1' in sortie, sortie


def test_un_job_metier_dans_un_contexte_autorise_reste_refuse(tmp_path):
    """Une opération applicative non substituée est refusée dans un node autorisé.

    Le node id du fichier temporaire ne peut pas correspondre à l'allowlist (elle
    est nominative et liée au chemin réel) : le refus par défaut s'applique donc
    d'abord. On vérifie les **deux** barrières : le refus par défaut hors chemin
    autorisé, puis l'inspection des jobs qui refuse une opération applicative même
    lorsque le nom du test est autorisé.
    """
    source = (
        "import pytest\n"
        "from apscheduler.schedulers.asyncio import AsyncIOScheduler\n"
        "import app.tasks.cleanup as cleanup_mod\n\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_the_real_scheduler_hands_the_intended_run_time_to_the_job():\n"
        "    scheduler = AsyncIOScheduler()\n"
        "    scheduler.add_job(cleanup_mod.cleanup_old_data_task, 'interval',"
        " minutes=5, id='refresh_prices_07_00')\n"
        "    with pytest.raises(AssertionError, match='Scheduler réel interdit'):\n"
        "        scheduler.start()\n"
    )
    summary = _executer_recette_sur(tmp_path, "test_scheduler_occurrence.py", source)

    assert summary["pytest_exit_code"] == 0
    assert summary["passed"] == 1
    assert summary["scheduler_started"] == 0
    assert summary["isolation"]["service_denied"] == 1


def test_un_job_metier_est_refuse_meme_sous_un_node_autorise():
    """L'inspection des jobs refuse une opération applicative sous un node autorisé.

    `RADAR_FINAL_NODE` est fixé au node autorisé, puis la garde est installée dans un
    sous-processus neuf : la barrière exercée est bien celle de l'inspection des jobs,
    pas le refus par défaut. Le job interdit n'est jamais exécuté.
    """
    bootstrap = (
        "import asyncio, json, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "from scripts import verify_consolidation as runner\n"
        "runner.environment('unit')\n"
        "os.environ['RADAR_FINAL_NODE'] = (\n"
        "    'tests/integration/test_scheduler_occurrence.py::'\n"
        "    'test_the_real_scheduler_hands_the_intended_run_time_to_the_job'\n"
        ")\n"
        "counters = runner.guard(site=False)\n"
        "import app.tasks.cleanup as cleanup_mod\n"
        "from app.tasks.scheduler import AsyncIOScheduler\n"
        "async def essai():\n"
        "    scheduler = AsyncIOScheduler()\n"
        "    scheduler.add_job(cleanup_mod.cleanup_old_data_task, 'interval',"
        " minutes=5, id='refresh_prices_07_00')\n"
        "    try:\n"
        "        scheduler.start()\n"
        "    except AssertionError as exc:\n"
        "        print('REFUS', exc)\n"
        "    else:\n"
        "        print('DEMARRE')\n"
        "asyncio.run(essai())\n"
        "print('COUNTERS', json.dumps(counters))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", bootstrap],
        cwd=runner.ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    sortie = completed.stdout + completed.stderr
    assert "DEMARRE" not in sortie, sortie
    assert "REFUS" in sortie, sortie
    assert "opération applicative non" in sortie, sortie
    assert '"service_denied": 0' in sortie, sortie


# Une fermeture autorisée appelle des noms de `app.tasks.scheduler` à l'exécution.
_WRAPPER_AUTORISE = (
    "import asyncio, json, os, sys\n"
    "sys.path.insert(0, os.getcwd())\n"
    "from scripts import verify_consolidation as runner\n"
    "runner.environment('unit')\n"
    "os.environ['RADAR_FINAL_NODE'] = (\n"
    "    'tests/integration/test_scheduler_occurrence.py::'\n"
    "    'test_the_real_scheduler_hands_the_intended_run_time_to_the_job'\n"
    ")\n"
    "counters = runner.guard(site=False)\n"
    "import importlib\n"
    "scheduler_module = importlib.import_module('app.tasks.scheduler')\n"
    "MARQUEUR = []\n"
    "async def essai(substituer):\n"
    "    from apscheduler.schedulers.asyncio import AsyncIOScheduler\n"
    "    if substituer:\n"
    "        async def espion(**kwargs):\n"
    "            MARQUEUR.append('espion')\n"
    "            return {}\n"
    "        scheduler_module.refresh_book_prices_task = espion\n"
    "    async def job_avec_occurrence():\n"
    "        MARQUEUR.append('operation-metier-appelee')\n"
    "        return await scheduler_module.refresh_book_prices_task("
    "job_id='x', scheduled_for=None)\n"
    "    job_avec_occurrence.__module__ = 'app.tasks.scheduler'\n"
    "    scheduler = AsyncIOScheduler()\n"
    "    scheduler.add_job(job_avec_occurrence, 'interval', minutes=5, id='refresh_prices_07_00')\n"
    "    try:\n"
    "        scheduler.start(paused=True)\n"
    "    except AssertionError as exc:\n"
    "        print('REFUS', exc)\n"
    "        return\n"
    "    print('DEMARRE')\n"
    "    scheduler.shutdown(wait=False)\n"
    "asyncio.run(essai(False))\n"
    "print('MARQUEUR_NON_SUBSTITUE', json.dumps(MARQUEUR))\n"
    "MARQUEUR.clear()\n"
    "asyncio.run(essai(True))\n"
    "print('MARQUEUR_SUBSTITUE', json.dumps(MARQUEUR))\n"
)


def test_une_fermeture_autorisee_appelant_une_operation_non_substituee_est_refusee(
):
    """Le nom et le module autorisés ne suffisent pas : ce que la fermeture appelle compte.

    Une fermeture `job_avec_occurrence` du bon module, mais qui résout encore
    `refresh_book_prices_task` vers l'opération applicative, doit être refusée ; la
    même fermeture correctement substituée doit être acceptée. L'opération interdit
    n'est jamais exécutée : le marqueur d'exécution reste absent côté refus.
    """
    completed = subprocess.run(
        [sys.executable, "-B", "-c", _WRAPPER_AUTORISE],
        cwd=runner.ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    sortie = completed.stdout + completed.stderr

    assert "REFUS" in sortie, sortie
    assert "encore liée à l'opération applicative non substituée" in sortie, sortie

    # La fermeture non substituée est refusée : rien n'a été exécuté.
    assert 'MARQUEUR_NON_SUBSTITUE []' in sortie, sortie

    # La fermeture correctement substituée est acceptée, et le scheduler démarre.
    assert sortie.count("DEMARRE") == 1, sortie
    assert 'MARQUEUR_SUBSTITUE []' in sortie, sortie

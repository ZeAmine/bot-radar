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

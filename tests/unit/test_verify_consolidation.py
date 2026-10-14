"""Le lanceur conserve les verdicts pytest sans inventer une phase call après un skip."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import verify_consolidation as runner


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
    completed = subprocess.run(
        [sys.executable, "-B", str(Path(runner.__file__)), "unit", "--target", str(target)],
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
    if "must not run" in source:
        assert list(summary["reports"].values()) == [{"setup": "skipped", "teardown": "passed"}]

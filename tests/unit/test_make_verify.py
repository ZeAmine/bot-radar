import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("ci_exit", [0, 1])
def test_verify_waits_for_ci_without_repeating_local_checks(tmp_path, monkeypatch, ci_exit):
    root = Path(__file__).resolve().parents[2]
    calls = tmp_path / "calls.json"
    local_calls = tmp_path / "local-calls"
    scripts = {
        "git": "print('chore/example')\n",
        "gh": (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['VERIFY_CALLS']).write_text(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(int(os.environ['VERIFY_CI_EXIT']))\n"
        ),
        "local-check": (
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['VERIFY_LOCAL_CALLS']).write_text('unexpected')\n"
            "raise SystemExit(99)\n"
        ),
    }
    for name, source in scripts.items():
        executable = tmp_path / name
        executable.write_text(f"#!{sys.executable}\n" + source)
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("VERIFY_CALLS", str(calls))
    monkeypatch.setenv("VERIFY_LOCAL_CALLS", str(local_calls))
    monkeypatch.setenv("VERIFY_CI_EXIT", str(ci_exit))
    local_check = str(tmp_path / "local-check")
    completed = subprocess.run(
        [
            "make", "--no-print-directory", "verify",
            f"GIT={tmp_path / 'git'}",
            f"RUFF={local_check}", f"MYPY={local_check}", f"PYTEST={local_check}",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert json.loads(calls.read_text()) == [
        "pr", "checks", "chore/example", "--watch", "--fail-fast",
    ]
    assert (completed.returncode == 0) is (ci_exit == 0), completed.stdout + completed.stderr
    assert not local_calls.exists()

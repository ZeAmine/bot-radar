import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def run_cron(tmp_path):
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    fake_bin = tmp_path / "commands"
    fake_bin.mkdir()
    source = Path(__file__).resolve().parents[2] / "scripts/refresh_chasse_cron.sh"
    script = scripts / source.name
    script.write_text(source.read_text().replace(
        'export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"',
        f'export PATH="{fake_bin}:/usr/bin:/bin"',
    ))
    calls = tmp_path / "calls"
    commands = {
        "docker": "#!/bin/sh\nprintf '9999\\n'\n",
        "flock": "#!/bin/sh\nexit 0\n",
        "timeout": "#!/bin/sh\nshift 3\nexec \"$@\"\n",
    }
    for name, content in commands.items():
        path = fake_bin / name
        path.write_text(content)
        path.chmod(0o700)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin/python"
    python.write_text(
        '#!/bin/sh\nprintf "capture\\n" >> "$FAKE_CALLS"\nexit "$FAKE_RESULT"\n',
    )
    python.chmod(0o700)
    home = tmp_path / "home"
    env = {
        "HOME": str(tmp_path), "CHASSE_HOME": str(home), "CHASSE_VENV": str(venv),
        "CHASSE_ACCOUNT_EMAIL": "synthetic@example.invalid",
        "CHASSE_ACCOUNT_PASSWORD": "synthetic", "INTERNAL_API_TOKEN": "synthetic",
        "FAKE_CALLS": str(calls), "PATH": os.defpath,
    }
    def run(code):
        env["FAKE_RESULT"] = str(code)
        result = subprocess.run(
            ["/bin/bash", str(script)], env=env, capture_output=True, text=True, timeout=5,
        )
        return result, (home / "refresh.log").read_text(), calls.read_text().splitlines()

    return run


def test_cron_logs_repeated_regulator_deferrals_without_retry_or_failure(run_cron):
    for _ in range(2):
        result, log, calls = run_cron(3)
        assert result.returncode == 0
        assert result.stdout == result.stderr == ""
    assert log.count("Report CAL (code 3)") == 2
    assert "prochaine échéance prévue" in log
    assert "échec" not in log
    assert "ERREUR" not in log
    assert calls == ["capture", "capture"]


@pytest.mark.parametrize("code", [0, 1, 2, 124])
def test_cron_preserves_other_exit_codes(run_cron, code):
    result, log, calls = run_cron(code)
    assert result.returncode == code
    assert calls == ["capture"]
    assert "Report CAL" not in log
    if code in (1, 2):
        assert "échec" in log

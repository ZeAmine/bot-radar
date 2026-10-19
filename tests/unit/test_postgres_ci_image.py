import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
PRIMARY = "public.ecr.aws/docker/library/postgres:16-alpine"
FALLBACK = "docker.io/library/postgres:16-alpine"
LOCAL_IMAGE = "botradar-postgres-ci:16-alpine"


def step_script(name):
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index(f"      - name: {name}") + 2
    body = []
    for line in lines[start:]:
        if line and not line.startswith("          "):
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


@pytest.mark.parametrize(("success_on", "pulls", "delays"), [
    (1, [PRIMARY], []),
    (2, [PRIMARY, FALLBACK], ["5"]),
    (3, [PRIMARY, FALLBACK, FALLBACK], ["5", "10"]),
    (0, [PRIMARY, FALLBACK, FALLBACK], ["5", "10"]),
])
def test_postgres_image_fallback_is_bounded_and_used_locally(
    tmp_path, success_on, pulls, delays,
):
    log = tmp_path / "calls.jsonl"
    fake = f"#!{sys.executable}\n" + textwrap.dedent('''
        import json
        import os
        import sys
        from pathlib import Path

        log = Path(os.environ["CI_IMAGE_CALL_LOG"])
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        command = [Path(sys.argv[0]).name, *sys.argv[1:]]
        with log.open("a") as stream:
            stream.write(json.dumps(command) + "\\n")
        if command[:2] == ["docker", "pull"]:
            attempt = 1 + sum(call[:2] == ["docker", "pull"] for call in calls)
            sys.exit(0 if attempt == int(os.environ["CI_IMAGE_SUCCESS_ON"]) else 1)
    ''')
    for name in ("docker", "sleep"):
        executable = tmp_path / name
        executable.write_text(fake)
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.defpath}",
        "CI_IMAGE_CALL_LOG": str(log),
        "CI_IMAGE_SUCCESS_ON": str(success_on),
    }
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", step_script("Récupérer l'image PostgreSQL avec relance bornée")],
        env=env, capture_output=True, text=True, timeout=10,
    )
    if success_on:
        assert result.returncode == 0, result.stderr
        started = subprocess.run(
            ["/bin/bash", "-e", "-c", step_script("Démarrer PostgreSQL")],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert started.returncode == 0, started.stderr
    else:
        assert result.returncode != 0
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call[2] for call in calls if call[:2] == ["docker", "pull"]] == pulls
    assert [call[1] for call in calls if call[0] == "sleep"] == delays
    tags = [call for call in calls if call[:2] == ["docker", "tag"]]
    runs = [call for call in calls if call[:2] == ["docker", "run"]]
    if success_on:
        assert tags == [["docker", "tag", pulls[-1], LOCAL_IMAGE]]
        assert len(runs) == 1
        assert "--pull=never" in runs[0]
        assert runs[0][-1] == LOCAL_IMAGE
    else:
        assert tags == []
        assert runs == []

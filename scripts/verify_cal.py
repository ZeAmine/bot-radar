import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def child(port: int, control: Path, targets: list[str]) -> int:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from scripts import verify_consolidation as verification

    verification.environment("unit")
    os.environ.update(
        CAL_TEST_REDIS_URL=f"redis://127.0.0.1:{port}/0",
        CAL_TEST_REDIS_CONTROL_DIR=str(control),
        RADAR_FINAL_ENDPOINTS=json.dumps([["127.0.0.1", port]]),
    )
    from redis.asyncio.connection import Connection

    connect = Connection.connect
    verification.guard()
    Connection.connect = connect

    def deny_children(event, _args):
        if event == "subprocess.Popen":
            raise PermissionError("Sous-processus interdit dans la suite CAL")

    sys.addaudithook(deny_children)
    import pytest

    return int(pytest.main([
        "-p", "pytest_asyncio.plugin", "-q", "-p", "no:cacheprovider", *targets,
    ]))


def parent(targets: list[str]) -> int:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker requis pour le Redis jetable CAL")
    docker_host = os.environ.get("DOCKER_HOST")
    if not docker_host:
        desktop = Path.home() / ".docker/run/docker.sock"
        docker_host = f"unix://{desktop}" if desktop.exists() else "unix:///var/run/docker.sock"
    name = f"botradar-cal-test-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="radar-cal-", dir="/tmp") as directory:
        control = Path(directory)
        command = [docker, "--config", directory, "--host", docker_host]

        def run(*args: str) -> str:
            result = subprocess.run(
                [*command, *args], env={"PATH": os.defpath}, capture_output=True,
                text=True, check=True, timeout=30,
            )
            return result.stdout.strip()

        created = False
        process = None
        try:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            run("run", "--pull=never", "-d", "--name", name,
                "-p", f"127.0.0.1:{port}:6379", "redis:7-alpine",
                "redis-server", "--save", "", "--appendonly", "no")
            created = True
            for _ in range(100):
                if run("exec", name, "redis-cli", "ping") == "PONG":
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("Redis jetable non prêt")
            print(f"Redis CAL jetable : 127.0.0.1:{port} ; conteneur {name}", flush=True)
            process = subprocess.Popen([
                sys.executable, "-B", str(Path(__file__).resolve()), "--child",
                "--port", str(port), "--control", directory,
                *[arg for target in targets for arg in ("--target", target)],
            ], env={}, start_new_session=True)
            deadline = time.monotonic() + 600
            while process.poll() is None:
                request = control / "request"
                if request.exists():
                    action = request.read_text()
                    request.unlink()
                    if action == "stop":
                        run("stop", "--time", "0", name)
                    elif action == "start":
                        run("start", name)
                    else:
                        raise RuntimeError("Commande de test Redis invalide")
                    (control / "ack").write_text(action)
                if time.monotonic() > deadline:
                    raise TimeoutError("Suite CAL au-delà de dix minutes")
                time.sleep(0.05)
            return process.returncode
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if created:
                run("rm", "-f", name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--control", type=Path)
    parser.add_argument("--target", action="append")
    args = parser.parse_args()
    targets = args.target or [
        *[str(path.relative_to(ROOT))
          for path in sorted((ROOT / "tests/unit").glob("test_cal*.py"))],
        "tests/unit/test_refresh_chasse_token.py", "tests/unit/test_sync_chasse_session.py",
        "tests/integration/test_cal_refresh_grid.py",
        "tests/integration/test_cal_storage_recovery.py",
    ]
    if args.child:
        if args.port is None or args.control is None:
            parser.error("Port et contrôle Redis jetable requis")
        return child(args.port, args.control, targets)
    return parent(targets)


if __name__ == "__main__":
    raise SystemExit(main())

"""Recette 0010 exclusivement synthétique, ressources neuves puis destruction exacte."""

from __future__ import annotations

import argparse
import asyncio
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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOCKET = "unix:///Users/aminezegmou/.docker/run/docker.sock"
IMAGE = "sha256:c05eced0bdb41ea9b95a656472a6aa4d50cad0d8a2e33d14eb1c53fd6204f2ae"


def child(data: dict[str, Any]) -> int:
    sys.path.insert(0, str(ROOT))
    from scripts.exercise_backup_recovery import isolation

    counts = isolation([("127.0.0.1", 55530), ("127.0.0.1", 55531)])
    os.environ["DATABASE_URL"] = (
        "postgresql+asyncpg://synthetic:synthetic_password@127.0.0.1:55531/synthetic"
    )
    from alembic.config import Config

    from alembic import command
    from scripts import delivery_prerequisites as tool
    from scripts import verify_postgresql_recipe as recipe
    from scripts.verify_consolidation import migrations

    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "0010_alert_preparation")
    recipe.URL = os.environ["DATABASE_URL"]
    before = asyncio.run(migrations())
    assert any(before.values())
    workspace = Path(data["workspace"])
    databases = [
        tool.Database(
            data["docker"], SOCKET, workspace / "docker-config", row["id"], row["system_id"]
        )
        for row in data["databases"]
    ]
    source, target = databases
    key_home, public_home = workspace / "private", workspace / "public"
    key_home.mkdir(mode=0o700)
    public_home.mkdir(mode=0o700)
    gpg = data["gpg"]
    base = [gpg, "--no-options", "--homedir", str(key_home), "--batch", "--no-tty"]
    tool.run(
        base
        + [
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Synthetic recovery",
            "rsa2048",
            "encr",
            "1d",
        ]
    )
    keys = tool.run(base + ["--with-colons", "--list-keys"]).decode()
    fingerprint = next(line.split(":")[9] for line in keys.splitlines() if line.startswith("fpr:"))
    exported = workspace / "public.asc"
    with exported.open("xb") as stream:
        tool.run(base + ["--armor", "--export", fingerprint], output=stream)
    with exported.open("rb") as stream:
        tool.run(
            [gpg, "--no-options", "--homedir", str(public_home), "--batch", "--import"],
            input_file=stream,
        )
    root = workspace / "code"
    (root / "app").mkdir(parents=True)
    (root / "app/synthetic.py").write_text('"""Synthetic code, never executed."""\n')
    (root / "app/prompts").mkdir()
    (root / "app/prompts/synthetic.txt").write_text("Synthetic runtime prompt\n")
    expected = {
        "revision": "0" * 40,
        "image_id": IMAGE,
        "files": {
            "app/synthetic.py": tool.digest(root / "app/synthetic.py"),
            "app/prompts/synthetic.txt": tool.digest(root / "app/prompts/synthetic.txt"),
        },
    }
    start = time.monotonic()
    receipt = tool.backup(
        source,
        data["stopped_app"],
        expected,
        root,
        workspace / "copy",
        gpg,
        public_home,
        fingerprint,
    )
    backup_seconds = time.monotonic() - start
    cipher = workspace / "copy/backup.tar.gpg"
    tool.verify_copy(cipher, receipt)
    copied = workspace / "independent-simulated.gpg"
    shutil.copyfile(cipher, copied)
    tool.verify_copy(copied, receipt)
    # Corruption détectée avant restauration ; original conservé.
    corrupted = workspace / "corrupted.gpg"
    corrupted.write_bytes(b"truncated")
    try:
        tool.verify_copy(corrupted, receipt)
    except ValueError:
        pass
    else:
        raise AssertionError("Corruption acceptée")
    try:
        tool.backup(
            source,
            data["stopped_app"],
            expected,
            root,
            workspace / "copy",
            gpg,
            public_home,
            fingerprint,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Copie existante écrasée")
    try:
        tool.backup(
            source,
            source.container,
            expected,
            root,
            workspace / "unsafe-copy",
            gpg,
            public_home,
            fingerprint,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Application active acceptée")
    for wrong, target_check in [
        (
            tool.Database(
                data["docker"], SOCKET, workspace / "docker-config", source.container, "0"
            ),
            False,
        ),
        (source, True),
    ]:
        try:
            wrong.attest(target=target_check)
        except ValueError:
            pass
        else:
            raise AssertionError("Identité ou point de connexion incorrect accepté")
    with copied.open("rb") as incoming:
        try:
            tool.run(
                [
                    gpg,
                    "--no-options",
                    "--homedir",
                    str(public_home),
                    "--batch",
                    "--no-tty",
                    "--decrypt",
                ],
                input_file=incoming,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Déchiffrement sans clé privée accepté")

    bundle = workspace / "decrypted.tar"
    with copied.open("rb") as incoming, bundle.open("xb") as outgoing:
        tool.run(base + ["--decrypt"], input_file=incoming, output=outgoing)
    start = time.monotonic()
    report = tool.restore_check(target, bundle, workspace)
    restore_seconds = time.monotonic() - start
    assert report["data_verified"]
    assert not report["application_started"]
    assert source.fingerprints(workspace) == target.fingerprints(workspace)
    try:
        tool.restore_check(target, bundle, workspace)
    except ValueError:
        pass
    else:
        raise AssertionError("Cible non vide acceptée")
    assert not list(workspace.glob("restore-plain-*"))
    print(
        "DELIVERY_0010_RECIPE",
        json.dumps(
            {
                "schema": tool.SCHEMA,
                "postgres_version_num": source.sql("SHOW server_version_num"),
                "source": data["databases"][0],
                "target": data["databases"][1],
                "data_verified": True,
                "application_started": False,
                "independent_verified": False,
                "negative_controls": [
                    "corrupt_copy",
                    "existing_copy",
                    "active_app",
                    "nonempty_target",
                    "wrong_identity",
                    "wrong_target",
                    "missing_private_key",
                ],
                "backup_seconds": backup_seconds,
                "restore_seconds": restore_seconds,
                "isolation": counts,
            }
        ),
        flush=True,
    )
    assert counts["dotenv"] == counts["local_probe"] == counts["external_probe"] == 1
    assert counts["pg_config"] == 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--typecheck", action="store_true")
    args = parser.parse_args()
    if args.typecheck:
        sys.path.insert(0, str(ROOT))
        from scripts.exercise_backup_recovery import isolation

        counts = isolation([])
        from mypy import api

        out, err, code = api.run(
            [
                "--no-incremental",
                "--explicit-package-bases",
                "--follow-imports=silent",
                "scripts/delivery_prerequisites.py",
                "scripts/exercise_delivery_0010.py",
            ]
        )
        print(out, end="")
        print(err, end="", file=sys.stderr)
        print("DELIVERY_ISOLATION", json.dumps(counts))
        return int(code)
    if args.child:
        return child(json.loads(args.child.read_text()))
    sys.path.insert(0, str(ROOT))
    from scripts.delivery_prerequisites import run

    docker, gpg = shutil.which("docker"), shutil.which("gpg")
    if docker is None or gpg is None:
        raise RuntimeError("Prérequis absent : aucune installation")
    for port in (55530, 55531):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    with tempfile.TemporaryDirectory(prefix="radar-delivery-0010-", dir="/private/tmp") as d:
        workspace = Path(d)
        config = workspace / "docker-config"
        config.mkdir(mode=0o700)
        argv = [docker, "--host", SOCKET, "--config", str(config)]
        ids = []
        nonce = uuid.uuid4().hex
        try:
            databases = []
            for port in (55531, 55530):
                cid = (
                    run(
                        argv
                        + [
                            "create",
                            "--pull=never",
                            "--label",
                            "botradar.exercise=" + nonce,
                            "--label",
                            "botradar.recovery=0010-isolated",
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
                        ]
                    )
                    .decode()
                    .strip()
                )
                ids.append(cid)
                run(argv + ["start", cid])
                deadline = time.monotonic() + 30
                while True:
                    try:
                        identity = (
                            run(
                                argv
                                + [
                                    "exec",
                                    cid,
                                    "psql",
                                    "-U",
                                    "synthetic",
                                    "-d",
                                    "synthetic",
                                    "-XAt",
                                    "-c",
                                    "SELECT system_identifier FROM pg_control_system()",
                                ],
                                timeout=5,
                            )
                            .decode()
                            .strip()
                        )
                        break
                    except RuntimeError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.25)
                row = {"id": cid, "system_id": identity, "port": port}
                databases.append(row)
                print("DELIVERY_RESOURCE", json.dumps(row), flush=True)
            stopped = (
                run(
                    argv
                    + [
                        "create",
                        "--pull=never",
                        "--label",
                        "botradar.exercise=" + nonce,
                        "--tmpfs",
                        "/var/lib/postgresql/data:rw",
                        "--entrypoint",
                        "/bin/true",
                        IMAGE,
                    ]
                )
                .decode()
                .strip()
            )
            ids.append(stopped)  # Jamais démarré, matérialise uniquement la barrière d'arrêt.
            attestation = workspace / "attestation.json"
            attestation.write_text(
                json.dumps(
                    {
                        "workspace": d,
                        "databases": databases,
                        "stopped_app": stopped,
                        "docker": docker,
                        "gpg": gpg,
                    }
                )
            )
            process = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--child", str(attestation)],
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
            for home in ("private", "public"):
                if (workspace / home).exists():
                    gpgconf = shutil.which("gpgconf")
                    if gpgconf:
                        run([gpgconf, "--homedir", str(workspace / home), "--kill", "gpg-agent"])
            for cid in reversed(ids):
                run(argv + ["rm", "-f", "-v", cid])
                assert not run(
                    argv
                    + ["ps", "-a", "--no-trunc", "--filter", "id=" + cid, "--format", "{{.ID}}"]
                )
                print("DELIVERY_RESOURCE_REMOVED", cid, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

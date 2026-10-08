"""Le retour conserve le code parent et le graphe cible, et ferme réellement Telegram."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from scripts import prepare_f04_rollback as builder
from scripts.serve_f04_rollback import TelegramMaintenance


async def test_maintenance_blocks_updates_before_dispatch_and_preserves_health():
    app = FastAPI()
    dispatched = []

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/v1/telegram/webhook/{secret}")
    async def webhook(secret: str):
        dispatched.append(secret)
        return {"status": "ok"}

    async with AsyncClient(
        transport=ASGITransport(app=TelegramMaintenance(app)), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        for secret in ("synthetic", "other-synthetic"):
            assert (
                await client.post(f"/api/v1/telegram/webhook/{secret}", json={"update_id": 1})
            ).status_code == 503
    assert dispatched == []


def test_bundle_executes_parent_code_and_preserves_target_graph(tmp_path, monkeypatch):
    original = Path(builder.__file__).resolve().parent
    repo = tmp_path / "source"
    for directory in ("app", "scripts", "alembic/versions"):
        (repo / directory).mkdir(parents=True, exist_ok=True)
    (repo / "app/__init__.py").write_text("")
    (repo / "scripts/__init__.py").write_text("")
    (repo / "app/value.py").write_text("answer = 1\n")
    (repo / "alembic.ini").write_text("[alembic]\n")
    (repo / "alembic/versions/0009.py").write_text("revision = '0009'\n")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "parent")
    parent = git("rev-parse", "HEAD")
    (repo / "app/value.py").write_text("answer = 2\n")
    (repo / "alembic/versions/0010.py").write_text("revision = '0010'\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "target")
    target = git("rev-parse", "HEAD")
    for name in ("freeze_bd_preparations.py", "serve_f04_rollback.py"):
        (repo / "scripts" / name).write_bytes((original / name).read_bytes())
    monkeypatch.setattr(builder, "__file__", str(repo / "scripts/prepare_f04_rollback.py"))
    output = tmp_path / "return"
    manifest = builder.prepare(parent, target, output)
    import sys

    value = subprocess.check_output(
        [sys.executable, "-c", "from app.value import answer; print(answer)"], cwd=output, text=True
    )
    assert value.strip() == "1"
    assert manifest["base_commit"] == parent
    assert manifest["migration_commit"] == target
    for name, digest in json.loads((output / "manifest.json").read_text())["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert (output / "alembic/versions/0010.py").is_file()
    assert not (output / ".env").exists()
    subprocess.run(["bash", "-n", str(output / "rollback.sh")], check=True)
    assert (
        subprocess.run(["bash", str(output / "rollback.sh")], capture_output=True).returncode == 2
    )
    with pytest.raises(FileExistsError):
        builder.prepare(parent, target, output)

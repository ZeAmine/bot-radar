"""Sauvegarde et restauration synthétiques, sans destination implicite ni libération métier.

Les opérations de données exigent une instance portant le label de l'exercice.
La publication locale est une simulation ; aucun transport distant n'est implémenté.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = "0013_pause_actions"
FORMAT = 1


def command(
    args: list[str], *, data: bytes | None = None, environment: dict[str, str] | None = None
) -> bytes:
    """Délai borné et diagnostic sans corps externe ni argument sensible."""
    env = {
        "PATH": os.defpath,
        "PGSSLMODE": "disable",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
    }
    env.update(environment or {})
    try:
        result = subprocess.run(args, input=data, capture_output=True, env=env, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Outil indisponible ou délai dépassé") from exc
    if result.returncode:
        raise RuntimeError("Échec de l’outil ; aucune réussite annoncée")
    return result.stdout


def utc(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Horodatage illisible") from exc
    if result.tzinfo is None:
        raise ValueError("Horodatage sans fuseau")
    return result.astimezone(UTC)


def freshness(manifest: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    if manifest is None:
        return {"stale": True, "missing": True, "independent_verified": False}
    bound = utc(manifest["snapshot_bound"])
    if bound > now:
        raise ValueError("Instantané futur")
    return {
        "stale": now - bound > timedelta(hours=18),
        "missing": False,
        "age_seconds": (now - bound).total_seconds(),
        "independent_verified": False,
        "publication": manifest.get("publication", "local_archive"),
        "operational_ok": False,
    }


def retention_plan(records: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Échéances UTC ; aucune suppression. Huit semaines calendaires maximum."""
    now = now.astimezone(UTC)
    current_week = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    weekly: dict[datetime, dict[str, Any]] = {}
    keep, deadlines = set(), {}
    for row in records:
        bound = utc(row["snapshot_bound"])
        if bound > now:
            raise ValueError("Instantané futur")
        week = (bound - timedelta(days=bound.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        deadlines[row["id"]] = (bound + timedelta(days=14)).isoformat()
        if now < bound + timedelta(days=14):
            keep.add(row["id"])
        if current_week - timedelta(weeks=7) <= week <= current_week:
            previous = weekly.get(week)
            if previous is None or (bound, row["id"]) > (
                utc(previous["snapshot_bound"]),
                previous["id"],
            ):
                weekly[week] = row
    for week, row in weekly.items():
        keep.add(row["id"])
        deadlines[row["id"]] = max(utc(deadlines[row["id"]]), week + timedelta(weeks=8)).isoformat()
    return {
        "keep": sorted(keep),
        "weekly": sorted(row["id"] for row in weekly.values()),
        "expires_utc": deadlines,
        "deletion_executed": False,
    }


@dataclass(frozen=True)
class Endpoint:
    container: str
    system_id: str
    host: str
    port: int
    database: str
    user: str
    password: str
    exercise: str
    docker: str
    docker_socket: str
    docker_config: str

    def docker_environment(self) -> dict[str, str]:
        return {"DOCKER_HOST": self.docker_socket, "DOCKER_CONFIG": self.docker_config}

    def metadata(self) -> dict[str, Any]:
        if not re.fullmatch("[a-f0-9]{64}", self.container) or self.host != "127.0.0.1":
            raise ValueError("Identité de ressource invalide")
        # Format JSON unique, sans lecture des variables d'environnement du conteneur.
        template = (
            '{"id":{{json .Id}},"labels":{{json .Config.Labels}},'
            '"ports":{{json .HostConfig.PortBindings}},"tmpfs":{{json .HostConfig.Tmpfs}},'
            '"mounts":{{json .Mounts}}}'
        )
        row: dict[str, Any] = json.loads(
            command(
                [self.docker, "inspect", "--format", template, self.container],
                environment=self.docker_environment(),
            )
        )
        if (
            row["id"] != self.container
            or row["labels"].get("botradar.exercise") != self.exercise
            or row["ports"] != {"5432/tcp": [{"HostIp": self.host, "HostPort": str(self.port)}]}
            or "/var/lib/postgresql/data" not in row["tmpfs"]
            or row["mounts"]
        ):
            raise ValueError("Ressource différente de la cible synthétique attestée")
        return row

    async def connect(self, *, empty: bool = False) -> Any:
        import asyncpg

        self.metadata()
        connection = await asyncpg.connect(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
            ssl=False,
            timeout=5,
        )
        try:
            row = await connection.fetchrow(
                "SELECT current_database() AS db, current_user AS usr, "
                "current_setting('server_version_num') AS version, "
                "system_identifier::text AS identity "
                "FROM pg_control_system()"
            )
            if (
                row["db"] != self.database
                or row["usr"] != self.user
                or row["identity"] != self.system_id
                or not 160000 <= int(row["version"]) < 170000
            ):
                raise ValueError("Identité SQL différente")
            if empty:
                unwanted = await connection.fetchval(
                    "SELECT "
                    "(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%') + "
                    "(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid "
                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema')) + "
                    "(SELECT count(*) FROM pg_type t JOIN pg_namespace n ON t.typnamespace=n.oid "
                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%') + "
                    "(SELECT count(*) FROM pg_extension WHERE extname <> 'plpgsql')"
                )
                if unwanted:
                    raise ValueError("Cible non vide")
            return connection
        except BaseException:
            await connection.close(timeout=5)
            raise

    def pg(self, tool: str, *args: str, data: bytes | None = None) -> bytes:
        self.metadata()
        return command(
            [self.docker, "exec", "-i", self.container, tool, *args],
            data=data,
            environment=self.docker_environment(),
        )


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def fingerprints(endpoint: Endpoint) -> dict[str, Any]:
    connection = await endpoint.connect()
    try:
        return await fingerprint_connection(connection)
    finally:
        await connection.close(timeout=5)


async def fingerprint_connection(connection: Any) -> dict[str, Any]:
    tables = await connection.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    )
    result = {}
    for row in tables:
        name = row["tablename"]
        if not re.fullmatch("[a-z_][a-z0-9_]*", name):
            raise ValueError("Table non prise en charge")
        values = await connection.fetch(f'SELECT row_to_json(t)::text AS data FROM "{name}" t')
        canonical = sorted(
            json.dumps(json.loads(value["data"]), sort_keys=True) for value in values
        )
        result[name] = {"count": len(canonical), "sha256": sha("\n".join(canonical).encode())}
    return result


def kit(root: Path) -> tuple[dict[str, bytes], str]:
    """Liste blanche de code versionné ; aucun parcours aveugle du poste."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("Git requis pour attester le kit")
    revision = command([git, "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
    names = (
        command([git, "-C", str(root), "ls-tree", "-r", "--name-only", "HEAD"])
        .decode()
        .splitlines()
    )
    fixed = {"Dockerfile", "compose.yml", "alembic.ini", "pyproject.toml", "requirements.lock"}
    selected = [
        name
        for name in names
        if name in fixed
        or (name.startswith(("app/", "alembic/")) and name.endswith(".py"))
        or (name.startswith("scripts/") and name.endswith((".py", ".sh")))
    ]
    files = {}
    for name in selected:
        data = command([git, "-C", str(root), "show", f"HEAD:{name}"])
        # Le noyau et les migrations doivent correspondre exactement à la révision.
        candidate = root / name
        if candidate.is_symlink() or candidate.resolve().is_relative_to(root.resolve()) is False:
            raise ValueError("Lien hors kit interdit")
        if any(parent.is_symlink() for parent in candidate.parents if parent != root.parent):
            raise ValueError("Répertoire lié interdit")
        if candidate.stat().st_nlink != 1:
            raise ValueError("Fichier lié interdit")
        if name.startswith(("app/", "alembic/")) and candidate.read_bytes() != data:
            raise ValueError("Code applicatif différent de la révision attestée")
        files["kit/" + name] = data
    # L'outillage courant est attesté séparément du code compatible sauvegardé.
    for name in ("scripts/backup_recovery.py", "scripts/backup_db.sh"):
        files["tools/" + name] = (root / name).read_bytes()
    files["config.synthetic.json"] = json.dumps(
        {
            "APP_ENV": "test",
            "DATABASE_URL": "__EXPLICIT_SYNTHETIC_TARGET__",
            "REDIS_URL": "redis://example.invalid:6379/0",
            "TELEGRAM_BOT_TOKEN": "synthetic-only",
            "TELEGRAM_WEBHOOK_SECRET": "synthetic-only",
            "INGEST_API_TOKEN": "synthetic-only",
            "INTERNAL_API_TOKEN": "synthetic-only",
            "TELEGRAM_ALLOWED_USER_IDS": [424242],
            "AI_ENABLED": False,
            "VINTED_POLLER_ENABLED": False,
        },
        sort_keys=True,
    ).encode()
    return files, revision


def gpg(binary: str, home: Path, *args: str, data: bytes) -> bytes:
    if not home.is_dir() or home.is_symlink():
        raise ValueError("Répertoire de clés explicite requis")
    return command(
        [
            binary,
            "--no-options",
            "--homedir",
            str(home),
            "--batch",
            "--no-tty",
            "--no-auto-key-retrieve",
            "--auto-key-locate",
            "clear",
            *args,
        ],
        data=data,
    )


async def backup(
    source: Endpoint, root: Path, work: Path, binary: str, key_home: Path, recipient: str
) -> Path:
    start = datetime.now(UTC)  # Borne antérieure au début de pg_dump, pas fin de transfert.
    stamp = time.monotonic()
    connection = await source.connect()
    attestation_seconds = time.monotonic() - stamp
    transaction = connection.transaction(isolation="repeatable_read", readonly=True)
    await transaction.start()
    try:
        if await connection.fetchval("SELECT version_num FROM alembic_version") != SCHEMA:
            raise ValueError("Schéma non pris en charge")
        snapshot = await connection.fetchval("SELECT pg_export_snapshot()")
        server_version = await connection.fetchval("SHOW server_version")
        identifier = uuid.uuid4().hex
        folder = work / identifier
        folder.mkdir(mode=0o700)
        durations = {"source_attestation_seconds": attestation_seconds}
        stamp = time.monotonic()
        dump = source.pg(
            "pg_dump",
            "-U",
            source.user,
            "-d",
            source.database,
            "-Fc",
            "--no-owner",
            "--snapshot",
            snapshot,
        )
        durations["dump_seconds"] = time.monotonic() - stamp
        stamp = time.monotonic()
        source.pg("pg_restore", "--list", data=dump)
        durations["archive_check_seconds"] = time.monotonic() - stamp
        stamp = time.monotonic()
        evidence = await fingerprint_connection(connection)
        durations["snapshot_data_check_seconds"] = time.monotonic() - stamp
    finally:
        await transaction.rollback()
        await connection.close(timeout=5)
    stamp = time.monotonic()
    files, revision = kit(root)
    durations["kit_seconds"] = time.monotonic() - stamp
    files["database.dump"] = dump
    manifest = {
        "format": FORMAT,
        "id": identifier,
        "schema": SCHEMA,
        "postgres_major": 16,
        "postgres_version": server_version,
        "source": {
            "container": source.container,
            "system_id": source.system_id,
            "database": source.database,
            "host": source.host,
            "port": source.port,
        },
        "application_revision": revision,
        "snapshot_bound": start.isoformat(),
        "publication": "local_archive",
        "encryption_recipient": recipient,
        "encryption_format": "OpenPGP",
        "independent_verified": False,
        "files": {
            name: {"sha256": sha(value), "size": len(value)} for name, value in files.items()
        },
        "durations": durations,
        "data": evidence,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    files["manifest.json"] = manifest_bytes
    with io.BytesIO() as stream:
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o600
                archive.addfile(info, io.BytesIO(data))
        stamp = time.monotonic()
        encrypted = gpg(
            binary,
            key_home,
            "--trust-model",
            "always",
            "--recipient",
            recipient,
            "--encrypt",
            data=stream.getvalue(),
        )
    durations["encryption_seconds"] = time.monotonic() - stamp
    (folder / "bundle.gpg").write_bytes(encrypted)
    manifest["cipher_sha256"], manifest["cipher_size"] = sha(encrypted), len(encrypted)
    (folder / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    (folder / "COMPLETE").write_text(sha((folder / "manifest.json").read_bytes()))
    return folder


def verify(folder: Path) -> dict[str, Any]:
    if folder.is_symlink():
        raise ValueError("Copie non prise en charge")
    manifest_path = folder / "manifest.json"
    if (folder / "COMPLETE").read_text() != sha(manifest_path.read_bytes()):
        raise ValueError("Copie incomplète ou manifeste altéré")
    row: dict[str, Any] = json.loads(manifest_path.read_bytes())
    if (
        type(row["format"]) is not int
        or row["format"] != FORMAT
        or row["schema"] != SCHEMA
        or row["postgres_major"] != 16
        or not re.fullmatch("[a-f0-9]{32}", row["id"])
    ):
        raise ValueError("Version incompatible")
    if (
        row.get("publication") not in {"local_archive", "simulated_verified"}
        or row.get("independent_verified") is not False
    ):
        raise ValueError("Publication non prise en charge")
    for name in ("manifest.json", "COMPLETE", "bundle.gpg"):
        if (folder / name).is_symlink():
            raise ValueError("Lien interdit")
    data = (folder / "bundle.gpg").read_bytes()
    if sha(data) != row["cipher_sha256"] or len(data) != row["cipher_size"]:
        raise ValueError("Archive corrompue ou tronquée")
    return row


def publish_simulated(folder: Path, destination: Path) -> Path:
    row = verify(folder)
    if (
        not destination.is_dir()
        or destination.is_symlink()
        or destination.resolve() == folder.parent.resolve()
    ):
        raise ValueError("Destination simulée distincte et configurée requise")
    target: Path = destination / str(row["id"])
    target.mkdir(mode=0o700)  # Exclusif ; aucune copie existante supprimée ou remplacée.
    try:
        for name in ("bundle.gpg", "manifest.json"):
            shutil.copyfile(folder / name, target / name)
        published = json.loads((target / "manifest.json").read_bytes())
        published["publication"] = "simulated_verified"
        published["publication_finished"] = datetime.now(UTC).isoformat()
        (target / "manifest.json").write_text(json.dumps(published, sort_keys=True))
        (target / "COMPLETE").write_text(sha((target / "manifest.json").read_bytes()))
        verify(target)
    except BaseException:
        # La copie incomplète reste identifiable ; aucune réussite ni suppression des anciennes.
        (target / "COMPLETE").unlink(missing_ok=True)
        raise
    return target


async def restore(
    folder: Path, target: Endpoint, binary: str, key_home: Path, expected_revision: str
) -> dict[str, Any]:
    outer = verify(folder)
    connection = await target.connect(empty=True)
    await connection.close(timeout=5)
    stamp = time.monotonic()
    plain = gpg(binary, key_home, "--decrypt", data=(folder / "bundle.gpg").read_bytes())
    decrypt_seconds = time.monotonic() - stamp
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as archive:
        members = archive.getmembers()
        if any(
            not item.isfile() or item.name.startswith("/") or ".." in Path(item.name).parts
            for item in members
        ) or len({item.name for item in members}) != len(members):
            raise ValueError("Archive non sûre")
        contents = {}
        for item in members:
            file = archive.extractfile(item)
            if file is None:
                raise ValueError("Archive incomplète")
            contents[item.name] = file.read()
    inner = json.loads(contents.pop("manifest.json"))
    if (
        inner["application_revision"] != expected_revision
        or inner["schema"] != SCHEMA
        or inner["files"] != outer["files"]
        or inner["snapshot_bound"] != outer["snapshot_bound"]
        or inner["id"] != outer["id"]
    ):
        raise ValueError("Kit ou version incompatibles")
    if set(contents) != set(inner["files"]):
        raise ValueError("Kit incomplet")
    for name, data in contents.items():
        if sha(data) != inner["files"][name]["sha256"] or len(data) != inner["files"][name]["size"]:
            raise ValueError("Composant du kit corrompu")
    connection = await target.connect(empty=True)  # Refus répété juste avant l'écriture.
    await connection.close(timeout=5)
    stamp = time.monotonic()
    target.pg(
        "pg_restore",
        "-U",
        target.user,
        "-d",
        target.database,
        "--single-transaction",
        "--exit-on-error",
        "--no-owner",
        "--no-privileges",
        data=contents["database.dump"],
    )
    restore_seconds = time.monotonic() - stamp
    stamp = time.monotonic()
    if await fingerprints(target) != inner["data"]:
        raise ValueError("Données restaurées différentes ; cible maintenue arrêtée")
    checks_seconds = time.monotonic() - stamp
    stamp = time.monotonic()
    connection = await target.connect()
    try:
        await quarantine_connection(connection)
    finally:
        await connection.close(timeout=5)
    return {
        "restored_verified": True,
        "quarantined": True,
        "business_released": False,
        "decryption_seconds": decrypt_seconds,
        "restore_seconds": restore_seconds,
        "data_check_seconds": checks_seconds,
        "quarantine_seconds": time.monotonic() - stamp,
    }


async def quarantine_connection(connection: Any) -> None:
    """Schéma explicitement pris en charge ; aucune libération métier."""
    if await connection.fetchval("SELECT version_num FROM alembic_version") != SCHEMA:
        raise ValueError("Schéma non pris en charge, aucune quarantaine appliquée")
    known = await connection.fetch(
        "SELECT id, notified_at, telegram_message_id, "
        "telegram_chat_id, telegram_bot_id FROM listing_matches WHERE notified_at IS NOT NULL "
        "ORDER BY id"
    )
    async with connection.transaction():
        await connection.execute(
            "UPDATE listing_matches SET notification_state='unknown', "
            "notification_reason='restore_boundary_unknown' WHERE notified_at IS NULL "
            "AND notification_state IN ('ready','deferred','sending')"
        )
        await connection.execute("UPDATE alerts SET decision_version=decision_version+1")
        await connection.execute(
            "UPDATE pause_actions SET invalidated=true WHERE applied_at IS NULL"
        )
        await connection.execute("UPDATE alerts SET status='PAUSED' WHERE status='ACTIVE'")
        await connection.execute(
            "UPDATE alerts SET preparation=jsonb_set(jsonb_set(preparation, "
            "'{activation_requested}', 'false'::jsonb), "
            "'{token}', to_jsonb(gen_random_uuid()::text)) "
            "WHERE preparation IS NOT NULL"
        )
    preserved = await connection.fetch(
        "SELECT id, notified_at, telegram_message_id, "
        "telegram_chat_id, telegram_bot_id FROM listing_matches WHERE notified_at IS NOT NULL "
        "ORDER BY id"
    )
    if known != preserved:
        raise ValueError("Marqueurs durables modifiés")
    if (
        await connection.fetchval("SELECT count(*) FROM alerts WHERE status='ACTIVE'")
        or await connection.fetchval(
            "SELECT count(*) FROM listing_matches "
            "WHERE notified_at IS NULL AND notification_state IN ('ready','deferred','sending')"
        )
        or await connection.fetchval(
            "SELECT count(*) FROM alerts WHERE preparation->>'activation_requested'='true'"
        )
    ):
        raise ValueError("Quarantaine non établie ; reprise interdite")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("verify", "freshness", "retention-plan"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.operation == "retention-plan":
        result = retention_plan(json.loads(args.path.read_bytes()), datetime.now(UTC))
    else:
        row = verify(args.path)
        result = (
            freshness(row, datetime.now(UTC))
            if args.operation == "freshness"
            else {
                "verified_bytes": True,
                "publication": row["publication"],
                "independent_verified": False,
            }
        )
    print(json.dumps(result, sort_keys=True))
    return 1 if args.operation == "freshness" and not result.get("operational_ok", False) else 0


if __name__ == "__main__":
    raise SystemExit(main())

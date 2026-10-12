"""Procédure ponctuelle 0010 : fichiers chiffrés, identités explicites, aucun démarrage.

Les modes de données réelles exigent leur autorisation opérationnelle séparée.
Aucun lanceur synthétique existant n'est modifié ou contourné.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

SCHEMA = "0010_alert_preparation"


def require_schema(value: str) -> None:
    if value != SCHEMA:
        raise ValueError("Source différente du schéma 0010 explicitement pris en charge")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_copy(path: Path, expected: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Copie absente ou lien symbolique")
    if path.stat().st_size != expected["size"] or digest(path) != expected["sha256"]:
        raise ValueError("Taille ou empreinte incorrecte")


def maintenance_config(source: str) -> str:
    """Remplace un site unique ; conserve les autres octets, y compris Stream."""
    starts = list(re.finditer(r"(?m)^radar\.aminezegmou\.fr\s*\{", source))
    if len(starts) != 1:
        raise ValueError("Site Radar absent ou ambigu")
    start = starts[0].start()
    depth, quote, comment, escape = 0, False, False, False
    for i in range(starts[0].end() - 1, len(source)):
        c = source[i]
        if comment:
            comment = c != "\n"
            continue
        if escape:
            escape = False
            continue
        if c == "\\" and quote:
            escape = True
            continue
        if c == '"':
            quote = not quote
        elif not quote:
            if c == "#":
                comment = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return (
                        source[:start] + "radar.aminezegmou.fr {\n"
                        '    respond "Radar en maintenance" 503\n}' + source[i + 1 :]
                    )
    raise ValueError("Bloc Radar incomplet")


def cron_candidate(source: str) -> str:
    """Désactive un appel unique ; préserve toutes les autres entrées sans les afficher."""
    lines = source.splitlines(keepends=True)
    selected = [
        i
        for i, line in enumerate(lines)
        if not line.lstrip().startswith("#")
        and "/home/ubuntu/bot-radar/scripts/backup_db.sh" in line
    ]
    if len(selected) != 1:
        raise ValueError("Appel cron absent ou ambigu")
    index = selected[0]
    lines[index] = "# INACTIVE_BACKUP_INDEPENDENT_UNCONFIGURED: " + lines[index]
    return "".join(lines)


def run(
    argv: list[str],
    *,
    output: BinaryIO | None = None,
    input_file: BinaryIO | None = None,
    timeout: int = 600,
) -> bytes:
    """Jamais de corps externe/arguments dans les erreurs ; sorties binaires sur fichier."""
    try:
        p = subprocess.run(
            argv,
            stdin=input_file,
            stdout=output or subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env={"PATH": os.defpath, "PGSSLMODE": "disable"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Outil absent ou échéance dépassée") from exc
    if p.returncode:
        raise RuntimeError("Outil en échec ; aucun succès annoncé")
    return p.stdout or b""


class Database:
    """Docker local explicite ; aucune destination issue de l'environnement habituel."""

    def __init__(
        self, docker: str, socket: str, config: Path, container: str, system_id: str
    ) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", container) or not system_id.isdecimal():
            raise ValueError("Identités explicites requises")
        if not socket.startswith("unix:///") or not config.is_dir() or config.is_symlink():
            raise ValueError("Socket Unix et configuration Docker vide requis")
        if any(config.iterdir()):
            raise ValueError("Configuration Docker non vide")
        self.argv = [docker, "--host", socket, "--config", str(config)]
        self.container, self.system_id = container, system_id

    def inspect(self, cid: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(run(self.argv + ["inspect", cid or self.container]))[0]
        return result

    def pg(
        self,
        tool: str,
        options: list[str],
        *,
        output: BinaryIO | None = None,
        input_file: BinaryIO | None = None,
    ) -> bytes:
        # Identifiants internes non affichés ; aucun mot de passe dans argv ou stdout.
        script = 'exec "$1" -U "$POSTGRES_USER" -d "$POSTGRES_DB" "${@:2}"'
        return run(
            self.argv
            + ["exec", "-i", self.container, "bash", "-c", script, "tool", tool, *options],
            output=output,
            input_file=input_file,
        )

    def sql(self, query: str, *, output: BinaryIO | None = None) -> str:
        return (
            self.pg("psql", ["-XAt", "-v", "ON_ERROR_STOP=1", "-c", query], output=output)
            .decode()
            .strip()
        )

    def attest(self, *, target: bool = False) -> dict[str, str]:
        row = self.inspect()
        if row["Id"] != self.container or not row["State"]["Running"]:
            raise ValueError("Conteneur différent ou arrêté")
        fields = self.sql(
            "SELECT system_identifier::text || '|' || "
            "current_setting('server_version_num') FROM pg_control_system()"
        )
        identity, version = fields.split("|")
        if identity != self.system_id or not 160000 <= int(version) < 170000:
            raise ValueError("Identité SQL ou version PostgreSQL différente")
        if target:
            ports = row["HostConfig"]["PortBindings"]
            if ports != {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55530"}]}:
                raise ValueError("Cible hors point local autorisé")
            if row["Mounts"] or "/var/lib/postgresql/data" not in row["HostConfig"]["Tmpfs"]:
                raise ValueError("Cible utilisant du stockage existant")
            if row["Config"]["Labels"].get("botradar.recovery") != "0010-isolated":
                raise ValueError("Label de cible absent")
            if (
                self.sql(
                    "SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE n.nspname NOT IN "
                    "('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%') "
                    "+ (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema')) "
                    "+ (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%') "
                    "+ (SELECT count(*) FROM pg_extension WHERE extname<>'plpgsql')"
                )
                != "0"
            ):
                raise ValueError("Cible non vide")
        else:
            require_schema(self.sql("SELECT version_num FROM alembic_version"))
        return {"container": self.container, "system_id": identity, "version_num": version}

    def schema_fingerprint(self) -> str:
        query = """SELECT evidence FROM (
          SELECT 'column|' || table_name || '|' || column_name || '|' || ordinal_position
            || '|' || udt_name || '|' || is_nullable || '|' || coalesce(column_default,'')
            AS evidence FROM information_schema.columns WHERE table_schema='public'
          UNION ALL SELECT 'constraint|' || c.conname || '|' || pg_get_constraintdef(c.oid)
            FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
            WHERE n.nspname='public'
          UNION ALL SELECT 'index|' || indexname || '|' || indexdef
            FROM pg_indexes WHERE schemaname='public'
          UNION ALL SELECT 'enum|' || t.typname || '|' || e.enumsortorder || '|' || e.enumlabel
            FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid
            JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public'
          UNION ALL SELECT 'sequence|' || sequencename || '|' || coalesce(last_value::text,'')
            FROM pg_sequences WHERE schemaname='public'
          ) q ORDER BY evidence COLLATE "C"
        """
        return hashlib.sha256(self.sql(query).encode()).hexdigest()

    def fingerprints(self, work: Path) -> dict[str, Any]:
        """Tri SQL puis flux sur fichier ; aucune table complète en mémoire Python."""
        names = self.sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).splitlines()
        evidence = {}
        for name in names:
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
                raise ValueError("Nom de table non pris en charge")
            path = work / "table.rows"
            query = (
                "SET TIME ZONE 'UTC'; COPY (SELECT row_to_json(t)::text FROM "
                f'public."{name}" t ORDER BY row_to_json(t)::text COLLATE "C") '
                "TO STDOUT"
            )
            with path.open("wb") as stream:
                self.sql(query, output=stream)
            evidence[name] = {
                "sha256": digest(path),
                "count": int(self.sql(f'SELECT count(*) FROM public."{name}"')),
            }
            path.unlink()
        return evidence


def encrypt(binary: str, home: Path, fingerprint: str, source: Path, output: Path) -> None:
    if not re.fullmatch(r"[A-F0-9]{40,64}", fingerprint):
        raise ValueError("Empreinte publique exacte requise")
    if home.is_symlink() or not home.is_dir():
        raise ValueError("Trousseau public explicite requis")
    base = [
        binary,
        "--no-options",
        "--homedir",
        str(home),
        "--batch",
        "--no-tty",
        "--no-auto-key-retrieve",
        "--auto-key-locate",
        "clear",
    ]
    private = run(base + ["--with-colons", "--list-secret-keys"])
    if any(line.startswith(b"sec:") for line in private.splitlines()):
        raise ValueError("Clé privée interdite sur la source")
    public = run(base + ["--with-colons", "--list-keys", fingerprint])
    if fingerprint.encode() not in public.split(b"\n")[1:-1] and (
        b":" + fingerprint.encode() + b":" not in public
    ):
        raise ValueError("Clé publique non retrouvée")
    with source.open("rb") as incoming, output.open("xb") as outgoing:
        run(
            base + ["--trust-model", "always", "--recipient", fingerprint, "--encrypt"],
            input_file=incoming,
            output=outgoing,
        )


def backup(
    db: Database,
    app: str,
    expected: dict[str, Any],
    root: Path,
    destination: Path,
    gpg: str,
    key_home: Path,
    fingerprint: str,
) -> dict[str, Any]:
    """Source arrêtée ; contenu source/restauré attestable avant toute migration."""
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination existante : aucun écrasement")
    if not re.fullmatch(r"[a-f0-9]{64}", app):
        raise ValueError("Identité complète de l’application requise")
    current = db.inspect(app)
    if current["Id"] != app:
        raise ValueError("Application différente")
    if current["State"]["Running"] or current["Image"] != expected["image_id"]:
        raise ValueError("Application non arrêtée ou image discordante")
    if not expected.get("files") or not re.fullmatch(r"[a-f0-9]{40}", expected.get("revision", "")):
        raise ValueError("Référence et kit explicites requis")
    if current["State"].get("ExitCode") != 0 or current["State"].get("OOMKilled"):
        raise ValueError("Arrêt gracieux non établi")
    identity = db.attest()
    if db.sql("SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database()") != "0":
        raise ValueError("Transaction préparée non résolue")
    if (
        db.sql(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND pid<>pg_backend_pid() AND backend_type='client backend' "
            "AND state<>'idle'"
        )
        != "0"
    ):
        raise ValueError("Traitement SQL encore actif")
    schema_hash = db.schema_fingerprint()
    for name, sha in expected["files"].items():
        if not re.fullmatch(
            r"(?:app|alembic|scripts)/[a-zA-Z0-9_./-]+|"
            r"Dockerfile|compose.yml|alembic.ini|pyproject.toml|requirements.lock",
            name,
        ):
            raise ValueError("Fichier hors kit autorisé")
        if ".." in Path(name).parts or any(part.startswith(".") for part in Path(name).parts):
            raise ValueError("Chemin privé ou relatif interdit")
        path = root / name
        if root.resolve() not in path.resolve().parents:
            raise ValueError("Chemin hors racine attestée")
        if path.is_symlink() or not path.is_file() or digest(path) != sha:
            raise ValueError("Code source différent de la référence attestée")
    destination.mkdir(mode=0o700)
    started = datetime.now(UTC).isoformat()
    try:
        with tempfile.TemporaryDirectory(prefix="plain-", dir=destination) as temporary:
            work = Path(temporary)
            before = db.fingerprints(work)
            dump = work / "database.dump"
            with dump.open("xb") as stream:
                db.pg("pg_dump", ["-Fc", "--no-owner"], output=stream)
            with dump.open("rb") as stream:
                db.pg("pg_restore", ["--list"], input_file=stream)
            if before != db.fingerprints(work) or schema_hash != db.schema_fingerprint():
                raise ValueError("Données modifiées pendant la sauvegarde arrêtée")
            manifest = {
                "format": "radar-0010-1",
                "schema": SCHEMA,
                "source": identity,
                "snapshot_bound": started,
                "application": expected,
                "data": before,
                "schema_sha256": schema_hash,
                "dump_sha256": digest(dump),
                "encryption_recipient": fingerprint,
                "procedure_sha256": digest(Path(__file__).resolve()),
            }
            manifest_path = work / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True))
            bundle = work / "bundle.tar"
            with tarfile.open(bundle, "w") as archive:
                archive.add(dump, arcname="database.dump", recursive=False)
                archive.add(manifest_path, arcname="manifest.json", recursive=False)
                archive.add(
                    Path(__file__).resolve(),
                    arcname="tools/delivery_prerequisites.py",
                    recursive=False,
                )
                for name in expected["files"]:
                    archive.add(root / name, arcname="kit/" + name, recursive=False)
            cipher = destination / "backup.tar.gpg"
            encrypt(gpg, key_home, fingerprint, bundle, cipher)
            receipt = {
                "format": "radar-0010-copy-1",
                "snapshot_bound": started,
                "size": cipher.stat().st_size,
                "sha256": digest(cipher),
                "independent_verified": False,
            }
            (destination / "copy.json").write_text(json.dumps(receipt, sort_keys=True))
            (destination / "COMPLETE").write_text("complete\n")
            return receipt
    except BaseException:
        # Une copie échouée reste visible sans COMPLETE ; aucune ancienne copie touchée.
        raise


def _restore_check(db: Database, bundle: Path, work: Path) -> dict[str, Any]:
    """Archive déchiffrée dans un espace privé, cible neuve et vide, sans quarantaine."""
    identity = db.attest(target=True)
    with tarfile.open(bundle, "r:") as archive:
        names = archive.getnames()
        if names.count("manifest.json") != 1 or names.count("database.dump") != 1:
            raise ValueError("Archive incomplète ou ambiguë")
        member = archive.getmember("manifest.json")
        if not member.isfile() or member.size > 1024 * 1024:
            raise ValueError("Manifeste non admissible")
        stream = archive.extractfile(member)
        assert stream is not None
        manifest = json.load(stream)
        require_schema(manifest["schema"])
        if manifest.get("format") != "radar-0010-1":
            raise ValueError("Format non compatible")
        allowed = {"manifest.json", "database.dump", "tools/delivery_prerequisites.py"} | {
            "kit/" + name for name in manifest["application"]["files"]
        }
        if set(names) != allowed or len(names) != len(allowed):
            raise ValueError("Kit incomplet ou membre inattendu")
        checked_members = {
            "kit/" + name: value for name, value in manifest["application"]["files"].items()
        }
        checked_members["tools/delivery_prerequisites.py"] = manifest["procedure_sha256"]
        for name, expected_hash in checked_members.items():
            member = archive.getmember(name)
            if not member.isfile():
                raise ValueError("Membre du kit non admissible")
            incoming = archive.extractfile(member)
            assert incoming is not None
            checksum = hashlib.sha256()
            while block := incoming.read(1024 * 1024):
                checksum.update(block)
            if checksum.hexdigest() != expected_hash:
                raise ValueError("Kit incompatible ou corrompu")
        member = archive.getmember("database.dump")
        if not member.isfile():
            raise ValueError("Dump non admissible")
        stream = archive.extractfile(member)
        assert stream is not None
        dump = work / "database.dump"
        with dump.open("xb") as output:
            while block := stream.read(1024 * 1024):
                output.write(block)
    if digest(dump) != manifest["dump_sha256"]:
        raise ValueError("Dump corrompu")
    with dump.open("rb") as source:
        db.pg("pg_restore", ["--exit-on-error", "--no-owner", "--no-acl"], input_file=source)
    require_schema(db.sql("SELECT version_num FROM alembic_version"))
    if db.schema_fingerprint() != manifest["schema_sha256"]:
        raise ValueError("Schéma restauré différent")
    if db.fingerprints(work) != manifest["data"]:
        raise ValueError("Données restaurées différentes")
    dump.unlink()
    return {
        "schema": SCHEMA,
        "target": identity,
        "data_verified": True,
        "application_started": False,
        "quarantine_applied": False,
    }


def restore_check(db: Database, bundle: Path, work: Path) -> dict[str, Any]:
    """Supprime les fichiers en clair créés, aussi en cas d'échec ; jamais l'archive."""
    if work.is_symlink() or not work.is_dir() or work.stat().st_mode & 0o077:
        raise ValueError("Espace de restauration privé requis")
    with tempfile.TemporaryDirectory(prefix="restore-plain-", dir=work) as temporary:
        return _restore_check(db, bundle, Path(temporary))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["backup", "restore-check", "verify-copy", "maintenance", "cron-candidate"]
    )
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--socket")
    parser.add_argument("--docker-config", type=Path)
    parser.add_argument("--container")
    parser.add_argument("--system-id")
    parser.add_argument("--app")
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--gpg", default="gpg")
    parser.add_argument("--key-home", type=Path)
    parser.add_argument("--fingerprint")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.mode == "verify-copy":
            if args.file.parent != args.receipt.parent or (
                (args.file.parent / "COMPLETE").read_text() != "complete\n"
            ):
                raise ValueError("Publication incomplète")
            verify_copy(args.file, json.loads(args.receipt.read_text()))
            result = {"cipher_integrity": True, "restoration_verified": False}
        elif args.mode in {"maintenance", "cron-candidate"}:
            # Préparation sur fichier exclusif, jamais reload ou modification du gateway.
            with args.destination.open("x") as output:
                output.write(
                    (maintenance_config if args.mode == "maintenance" else cron_candidate)(
                        args.file.read_text()
                    )
                )
            result = {"candidate_prepared": True, "operation_applied": False}
        else:
            db = Database(
                args.docker, args.socket, args.docker_config, args.container, args.system_id
            )
            if args.mode == "backup":
                result = backup(
                    db,
                    args.app,
                    json.loads(args.expected.read_text()),
                    args.root,
                    args.destination,
                    args.gpg,
                    args.key_home,
                    args.fingerprint,
                )
            else:
                result = restore_check(db, args.bundle, args.work)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

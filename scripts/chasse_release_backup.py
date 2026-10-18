import argparse
import asyncio
import io
import json
import os
import re
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backup_recovery import (  # noqa: E402
    Endpoint,
    command,
    fingerprint_connection,
    fingerprints,
    gpg,
    sha,
)

FORMAT = 'botradar-chasse-stopped-backup-v1'
MAX_BYTES = 512 * 1024 * 1024
SCHEMAS = {'0014_alert_market_freshness', '0015_chasse_weekly_prices'}


def validate_schema(value: str) -> str:
    if value not in SCHEMAS:
        raise ValueError('Schéma de sauvegarde non pris en charge')
    return value


class StoppedDatabase:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        for name in ('app_container', 'postgres_container'):
            if not re.fullmatch('[a-f0-9]{64}', config[name]):
                raise ValueError('Identité de conteneur requise')
        parsed = urlsplit(config['database_url'].replace('postgresql+asyncpg:', 'postgresql:'))
        if (parsed.hostname != config['database_host']
                or parsed.port != config['database_port']):
            raise ValueError('Destination SQL différente')

    def _inspect(self, identifier: str) -> dict[str, Any]:
        data = command([self.config['docker'], 'inspect', '--format',
                        '{"id":{{json .Id}},"running":{{json .State.Running}},'
                        '"image":{{json .Image}}}', identifier],
                       environment=self.config['docker_environment'])
        row: dict[str, Any] = json.loads(data)
        return row

    async def assert_stopped(self) -> None:
        app = await asyncio.to_thread(self._inspect, self.config['app_container'])
        postgres = await asyncio.to_thread(self._inspect, self.config['postgres_container'])
        if (app['id'] != self.config['app_container'] or app['running'] is not False
                or app['image'] != self.config['app_image']
                or postgres['id'] != self.config['postgres_container']
                or postgres['running'] is not True
                or postgres['image'] != self.config['postgres_image']):
            raise ValueError('Application non arrêtée ou dépendance différente')

    async def connect(self) -> Any:
        import asyncpg

        parsed = urlsplit(self.config['database_url'].replace('postgresql+asyncpg:', 'postgresql:'))
        query = [("sslmode" if key == "ssl" else key, value)
                 for key, value in parse_qsl(parsed.query)]
        connection = await asyncpg.connect(
            urlunsplit(parsed._replace(query=urlencode(query))), timeout=5,
        )
        try:
            row = await connection.fetchrow(
                "SELECT current_database() AS db, current_user AS usr, "
                "current_setting('server_version_num') AS version, "
                "system_identifier::text AS identity FROM pg_control_system()"
            )
            if (row['identity'] != self.config['system_id']
                    or row['db'] != self.config['database'] or row['usr'] != self.config['user']
                    or not 160000 <= int(row['version']) < 170000):
                raise ValueError('Identité SQL différente')
            return connection
        except BaseException:
            await connection.close(timeout=5)
            raise

    def pg(self, tool: str, *args: str, data: bytes | None = None) -> bytes:
        if tool not in {'pg_dump', 'pg_restore'}:
            raise ValueError('Outil PostgreSQL interdit')
        return command([self.config['docker'], 'exec', '-i', self.config['postgres_container'],
                        tool, *args], data=data, environment=self.config['docker_environment'])


async def backup_stopped(
    source: Any, *, output: Path, expected_schema: str, application_revision: str,
    binary: str, public_home: Path, recipient: str,
) -> Path:
    await source.assert_stopped()
    validate_schema(expected_schema)
    if not re.fullmatch('[a-f0-9]{40}', application_revision):
        raise ValueError('Révision applicative exacte requise')
    if not re.fullmatch('[A-Fa-f0-9]{40}', recipient):
        raise ValueError('Empreinte publique exacte requise')
    if output.exists() or output.is_symlink():
        raise ValueError('Destination déjà présente')
    if b'sec:' in gpg(binary, public_home, '--with-colons', '--list-secret-keys', data=b''):
        raise ValueError('Le répertoire de sauvegarde doit contenir les clés publiques seules')
    started = datetime.now(UTC)
    connection = await source.connect()
    transaction = connection.transaction(isolation='repeatable_read', readonly=True)
    await transaction.start()
    try:
        schema = await connection.fetchval('SELECT version_num FROM alembic_version')
        if schema != expected_schema:
            raise ValueError('Schéma différent de la révision attendue')
        snapshot = await connection.fetchval('SELECT pg_export_snapshot()')
        dump = await asyncio.to_thread(source.pg, 'pg_dump', '-U', source.config['user'],
                                      '-d', source.config['database'], '-Fc', '--no-owner',
                                      '--snapshot', snapshot)
        if len(dump) > MAX_BYTES:
            raise ValueError('Archive trop volumineuse pour cette procédure')
        await asyncio.to_thread(source.pg, 'pg_restore', '--list', data=dump)
        evidence = await fingerprint_connection(connection)
    finally:
        await transaction.rollback()
        await connection.close(timeout=5)
    await source.assert_stopped()
    manifest = {'format': FORMAT, 'schema': schema, 'snapshot_bound': started.isoformat(),
                'application_revision': application_revision,
                'app_image': source.config['app_image'],
                'app_container': source.config['app_container'],
                'postgres_container': source.config['postgres_container'],
                'postgres_image': source.config['postgres_image'],
                'source_system_id': source.config['system_id'],
                'source_database': source.config['database'],
                'recipient': recipient, 'dump_sha256': sha(dump), 'data': evidence}
    with io.BytesIO() as stream:
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name, value in {'manifest.json': json.dumps(manifest, sort_keys=True).encode(),
                                'database.dump': dump}.items():
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(value), 0o600
                archive.addfile(item, io.BytesIO(value))
        cipher = gpg(binary, public_home, '--trust-model', 'always', '--recipient', recipient,
                     '--encrypt', data=stream.getvalue())
    output.mkdir(mode=0o700)
    (output/'bundle.gpg').write_bytes(cipher)
    manifest['cipher_sha256'] = sha(cipher)
    (output/'manifest.json').write_text(json.dumps(manifest, sort_keys=True))
    for name in ('bundle.gpg', 'manifest.json'):
        os.chmod(output/name, 0o600)
    (output/'COMPLETE').write_text(sha((output/'manifest.json').read_bytes()))
    os.chmod(output/'COMPLETE', 0o600)
    return output


def verify_archive(folder: Path) -> dict[str, Any]:
    if folder.is_symlink() or any((folder/name).is_symlink()
                                 for name in ('manifest.json', 'bundle.gpg', 'COMPLETE')):
        raise ValueError('Lien interdit')
    manifest_bytes = (folder/'manifest.json').read_bytes()
    if (folder/'COMPLETE').read_text() != sha(manifest_bytes):
        raise ValueError('Archive incomplète')
    manifest: dict[str, Any] = json.loads(manifest_bytes)
    if manifest.get('format') != FORMAT:
        raise ValueError('Format incompatible')
    validate_schema(manifest['schema'])
    if (folder/'bundle.gpg').stat().st_size > MAX_BYTES:
        raise ValueError('Archive trop volumineuse')
    if sha((folder/'bundle.gpg').read_bytes()) != manifest['cipher_sha256']:
        raise ValueError('Archive corrompue')
    return manifest


async def restore_check(folder: Path, target: Endpoint, *, binary: str,
                        private_home: Path, expected_revision: str) -> dict[str, Any]:
    if target.port != 55514:
        raise ValueError('Seul le port de recette autorisé est accepté')
    if private_home.is_symlink() or private_home.stat().st_mode & 0o077:
        raise ValueError("Répertoire de clé privée 0700 requis")
    outer = verify_archive(folder)
    if (target.system_id == outer['source_system_id']
            and target.database == outer['source_database']):
        raise ValueError('La source ne peut pas être la cible de restauration')
    if outer['application_revision'] != expected_revision:
        raise ValueError('Révision différente')
    connection = await target.connect(empty=True)
    await connection.close(timeout=5)
    plain = gpg(binary, private_home, '--decrypt', data=(folder/'bundle.gpg').read_bytes())
    with tarfile.open(fileobj=io.BytesIO(plain), mode='r:gz') as archive:
        members = archive.getmembers()
        if (sorted(item.name for item in members) != ['database.dump', 'manifest.json']
                or any(not item.isfile() or item.size > MAX_BYTES for item in members)):
            raise ValueError('Archive non sûre')
        files = {}
        for item in members:
            value = archive.extractfile(item)
            if value is None:
                raise ValueError('Archive incomplète')
            files[item.name] = value.read()
    inner = json.loads(files['manifest.json'])
    if inner != {key: value for key, value in outer.items() if key != 'cipher_sha256'}:
        raise ValueError('Manifeste interne différent')
    if sha(files['database.dump']) != outer['dump_sha256']:
        raise ValueError('Dump altéré')
    connection = await target.connect(empty=True)
    await connection.close(timeout=5)
    await asyncio.to_thread(target.pg, 'pg_restore', '-U', target.user, '-d', target.database,
                            '--single-transaction', '--exit-on-error', '--no-owner',
                            '--no-privileges', data=files['database.dump'])
    if await fingerprints(target) != outer['data']:
        raise ValueError('Restauration différente')
    connection = await target.connect()
    try:
        if await connection.fetchval('SELECT version_num FROM alembic_version') != outer['schema']:
            raise ValueError('Schéma restauré différent')
    finally:
        await connection.close(timeout=5)
    receipt = {'restore_verified': True, 'business_released': False,
               'cipher_sha256': outer['cipher_sha256'], 'schema': outer['schema'],
               'application_revision': expected_revision, 'app_image': outer['app_image'],
               'app_container': outer['app_container'], 'postgres_image': outer['postgres_image'],
               'postgres_container': outer['postgres_container'],
               'source_system_id': outer['source_system_id'],
               'target_container': target.container, 'target_system_id': target.system_id,
               'verified_at': datetime.now(UTC).isoformat()}
    path = folder/'RESTORE_CHECK.json'
    with path.open('x') as output:
        json.dump(receipt, output, sort_keys=True)
    os.chmod(path, 0o600)
    return receipt


def preflight_keys(config: dict[str, Any]) -> dict[str, Any]:
    import secrets

    source = StoppedDatabase(config["source"])
    validate_schema(config["expected_schema"])
    if not re.fullmatch("[a-f0-9]{40}", config["application_revision"]):
        raise ValueError("Révision exacte requise")
    if not re.fullmatch("[A-Fa-f0-9]{40}", config["recipient"]):
        raise ValueError("Destinataire exact requis")
    if Path(config["output"]).exists():
        raise ValueError("Destination de sauvegarde déjà présente")
    private, public = Path(config["private_home"]), Path(config["public_home"])
    if private.is_symlink() or private.stat().st_mode & 0o077:
        raise ValueError("Répertoire de clé privée 0700 requis")
    if b"sec:" in gpg(config["gpg"], public, "--with-colons", "--list-secret-keys", data=b""):
        raise ValueError("Le répertoire public contient une clé secrète")
    challenge = secrets.token_bytes(64)
    encrypted = gpg(config["gpg"], public, "--trust-model", "always", "--recipient",
                    config["recipient"], "--encrypt", data=challenge)
    if gpg(config["gpg"], private, "--decrypt", data=encrypted) != challenge:
        raise ValueError("Clé de restauration non vérifiée")
    return {"keys_verified": True, "source_configuration_validated": bool(source.config)}


async def deployment_backup(config: dict[str, Any], *, source: Any = None) -> dict[str, Any]:
    source = source if source is not None else StoppedDatabase(config["source"])
    folder = Path(config["output"])
    await backup_stopped(
        source, output=folder, expected_schema=config["expected_schema"],
        application_revision=config["application_revision"], binary=config["gpg"],
        public_home=Path(config["public_home"]), recipient=config["recipient"],
    )
    plan = folder / "restore-plan.json"
    plan.write_text(json.dumps({"folder": str(folder.resolve()), "gpg": config["gpg"],
                                "private_home": config["private_home"],
                                "revision": config["application_revision"]}))
    os.chmod(plan, 0o600)
    from scripts.verify_consolidation import postgres_parent

    code = await asyncio.to_thread(postgres_parent, recovery_plan=plan)
    if code:
        raise ValueError("Restauration de contrôle non réussie")
    result: dict[str, Any] = json.loads((folder / "RESTORE_CHECK.json").read_text())
    await source.assert_stopped()
    connection = await source.connect()
    transaction = connection.transaction(isolation="repeatable_read", readonly=True)
    await transaction.start()
    try:
        if await fingerprint_connection(connection) != verify_archive(folder)["data"]:
            raise ValueError("La source a changé après la sauvegarde")
    finally:
        await transaction.rollback()
        await connection.close(timeout=5)
    return result


async def restore_attested(plan: dict[str, Any], resource: dict[str, Any]) -> dict[str, Any]:
    from scripts import verify_postgresql_recipe as recipe

    target = Endpoint(
        resource["container"], resource["system_id"], "127.0.0.1", 55514,
        resource.get("database", "radar_pause_recipe"), recipe.USER, recipe.PASSWORD,
        resource["exercise"],
        resource["docker"], resource["docker_socket"], resource["docker_config"],
    )
    return await restore_check(Path(plan["folder"]), target, binary=plan["gpg"],
                               private_home=Path(plan["private_home"]),
                               expected_revision=plan["revision"])


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode == 'verify':
        manifest = verify_archive(args.folder)
        return {'archive_verified': True, 'schema': manifest['schema'],
                'restore_verified': False}
    if args.config.stat().st_mode & 0o077 or args.config.is_symlink():
        raise ValueError('Configuration privée 0600 requise')
    config = json.loads(args.config.read_text())
    if args.mode == 'preflight':
        return preflight_keys(config)
    if args.mode == 'deployment-backup':
        return await deployment_backup(config)
    if args.mode == 'backup-stopped':
        await backup_stopped(StoppedDatabase(config), output=args.folder,
                             expected_schema=args.schema, application_revision=args.revision,
                             binary=args.gpg, public_home=args.key_home, recipient=args.recipient)
        return {'archive_verified': True, 'restore_verified': False}
    return await restore_check(args.folder, Endpoint(**config), binary=args.gpg,
                               private_home=args.key_home, expected_revision=args.revision)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=[
        'backup-stopped', 'restore-check', 'verify',
        'deployment-backup', 'restore-attested', 'preflight',
    ])
    parser.add_argument('--folder', type=Path)
    parser.add_argument('--resource', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--schema', choices=sorted(SCHEMAS))
    parser.add_argument('--revision')
    parser.add_argument('--gpg')
    parser.add_argument('--key-home', type=Path)
    parser.add_argument('--recipient')
    args = parser.parse_args()
    if args.mode in {'backup-stopped', 'restore-check'} and not all((
        args.config, args.revision, args.gpg, args.key_home, args.folder,
    )):
        parser.error('Configuration, révision et clés explicites requises')
    if args.mode == 'backup-stopped' and not all((args.schema, args.recipient)):
        parser.error('Schéma et destinataire exacts requis')
    try:
        if args.mode == "restore-attested":
            if args.resource is None or args.config is None:
                parser.error("Ressource de recette et plan explicites requis")
            resource = json.loads(args.resource.read_text())
            plan = json.loads(args.config.read_text())
            from scripts.verify_consolidation import environment, guard

            environment("postgres")
            os.environ["RADAR_FINAL_BACKUP_IDS"] = json.dumps([resource["container"]])
            guard()
            result = asyncio.run(restore_attested(plan, resource))
        else:
            if args.mode == "verify" and args.folder is None:
                parser.error("Archive requise")
            if args.mode in {"deployment-backup", "preflight"} and args.config is None:
                parser.error("Configuration privée requise")
            result = asyncio.run(run(args))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

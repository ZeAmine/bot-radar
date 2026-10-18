import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def exercise(data: dict[str, Any]) -> dict[str, Any]:
    from alembic.config import Config

    from alembic import command as alembic_command
    from scripts import verify_postgresql_recipe as recipe
    from scripts.backup_recovery import Endpoint, command, gpg
    from scripts.chasse_release_backup import (
        StoppedDatabase,
        backup_stopped,
        deployment_backup,
        preflight_keys,
        restore_attested,
        restore_check,
        verify_archive,
    )
    from scripts.verify_consolidation import migrations

    await recipe.verify_identity(data['system_id'])
    config = Config(str(ROOT/'alembic.ini'))
    await asyncio.to_thread(alembic_command.upgrade, config, '0010_alert_preparation')
    await migrations()
    await asyncio.to_thread(alembic_command.upgrade, config, '0013_pause_actions')
    await recipe.seed_market_freshness_migration()
    await asyncio.to_thread(alembic_command.upgrade, config, '0014_alert_market_freshness')
    workspace = Path(data['workspace'])
    homes = [workspace/name for name in ('public-key', 'private-key', 'wrong-key')]
    binary = data['gpg']
    try:
        for home in homes:
            home.mkdir(mode=0o700)
        for home in homes[1:]:
            gpg(binary, home, '--pinentry-mode', 'loopback', '--passphrase', '',
                '--quick-generate-key', 'Synthetic Chasse <synthetic@example.invalid>',
                'rsa2048', 'encr', '1d', data=b'')
        keys = gpg(binary, homes[1], '--with-colons', '--list-keys', data=b'').decode()
        recipient = next(row.split(':')[9] for row in keys.splitlines() if row.startswith('fpr:'))
        gpg(binary, homes[0], '--import',
            data=gpg(binary, homes[1], '--export', recipient, data=b''))
        environment = {'DOCKER_HOST': data['docker_socket'], 'DOCKER_CONFIG': data['docker_config']}
        source_config = {
            'app_container': 'a'*64, 'app_image': 'sha256:'+'b'*64,
            'postgres_container': data['container'], 'postgres_image': data['image'],
            'docker': data['docker'], 'docker_environment': environment,
            'database_url': recipe.URL, 'database_host': '127.0.0.1', 'database_port': 55514,
            'database': recipe.DATABASE, 'user': recipe.USER, 'system_id': data['system_id'],
        }

        class SyntheticStoppedDatabase(StoppedDatabase):
            def _inspect(self, identifier: str) -> dict[str, Any]:
                if identifier == 'a'*64:
                    return {'id': identifier, 'running': False, 'image': 'sha256:'+'b'*64}
                return super()._inspect(identifier)

        source = SyntheticStoppedDatabase(source_config)
        base = Endpoint(data['container'], data['system_id'], '127.0.0.1', 55514,
                        recipe.DATABASE, recipe.USER, recipe.PASSWORD, data['exercise'],
                        data['docker'], data['docker_socket'], data['docker_config'])
        reports = []
        schemas = ('0014_alert_market_freshness', '0015_chasse_weekly_prices')
        for index, schema in enumerate(schemas):
            if index:
                await asyncio.to_thread(alembic_command.upgrade, config, 'head')
                connection = await source.connect()
                try:
                    await connection.execute(
                        "UPDATE book_market_cache SET source_history=$1::json, "
                        "refresh_state=$2::json, next_refresh_at='2026-10-25T12:00:00Z' "
                        "WHERE id=(SELECT id FROM book_market_cache ORDER BY id LIMIT 1)",
                        json.dumps({'price_cents': 921, 'synthetic': True}),
                        json.dumps({'history': {'due_at': '2026-10-25T12:00:00+00:00'}}),
                    )
                finally:
                    await connection.close(timeout=5)
            folder = workspace/f'backup-{index}'
            await backup_stopped(source, output=folder, expected_schema=schema,
                                 application_revision='c'*40, binary=binary,
                                 public_home=homes[0], recipient=recipient)
            database = f'chasse_restore_{index}'
            connection = await source.connect()
            try:
                await connection.execute(f'CREATE DATABASE {database}')
            finally:
                await connection.close(timeout=5)
            target = replace(base, database=database)
            report = await restore_check(folder, target, binary=binary, private_home=homes[1],
                                         expected_revision='c'*40)
            assert report['restore_verified']
            assert not report['business_released']
            connection = await target.connect()
            try:
                assert await connection.fetchval('SELECT count(*) FROM book_market_cache') >= 30
                assert await connection.fetchval('SELECT count(*) FROM alerts') >= 1
            finally:
                await connection.close(timeout=5)
            try:
                await restore_check(folder, target, binary=binary, private_home=homes[1],
                                    expected_revision='c'*40)
            except ValueError:
                pass
            else:
                raise AssertionError('Restauration acceptée sur une cible non vide')
            try:
                gpg(binary, homes[2], '--decrypt', data=(folder/'bundle.gpg').read_bytes())
            except RuntimeError:
                pass
            else:
                raise AssertionError('Mauvaise clé acceptée')
            reports.append({'schema': schema, 'all_table_fingerprints_match': True,
                            'nonempty_target_refused': True, 'wrong_key_refused': True})
        import shutil

        damaged = workspace / "damaged"
        shutil.copytree(workspace / "backup-1", damaged)
        with (damaged / "bundle.gpg").open("ab") as output:
            output.write(b"corruption")
        try:
            verify_archive(damaged)
        except ValueError:
            pass
        else:
            raise AssertionError("Archive corrompue acceptée")
        plan_config = {"source": source_config, "output": str(workspace / "deployment-backup"),
                       "expected_schema": "0015_chasse_weekly_prices",
                       "application_revision": "c"*40, "gpg": binary,
                       "public_home": str(homes[0]), "private_home": str(homes[1]),
                       "recipient": recipient}
        assert preflight_keys(plan_config)["keys_verified"]
        from scripts import verify_consolidation as runner

        original_parent = runner.postgres_parent

        def restore_in_owned_container(
            targets: list[str] | None = None, *, chasse_backup: bool = False,
            recovery_plan: Path | None = None,
        ) -> int:
            assert recovery_plan is not None
            assert targets is None
            assert not chasse_backup
            async def check() -> None:
                connection = await source.connect()
                try:
                    await connection.execute("CREATE DATABASE chasse_restore_deployment")
                finally:
                    await connection.close(timeout=5)
                resource = dict(data, database="chasse_restore_deployment")
                result = await restore_attested(json.loads(recovery_plan.read_text()), resource)
                assert result["restore_verified"]
            asyncio.run(check())
            return 0

        runner.postgres_parent = restore_in_owned_container
        try:
            assert (await deployment_backup(plan_config, source=source))["restore_verified"]
        finally:
            runner.postgres_parent = original_parent
        return {'schemas': reports, 'application_stop_check': 'simulated',
                'postgres_dump_restore': 'real_disposable', 'production_touched': False,
                'corruption_refused': True, 'deployment_orchestration_verified': True}
    finally:
        for home in homes:
            command([data['gpgconf'], '--homedir', str(home), '--kill', 'all'])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', type=Path)
    args = parser.parse_args()
    from scripts.verify_consolidation import environment, guard, postgres_parent

    if args.child is None:
        return postgres_parent(chasse_backup=True)
    data = json.loads(args.child.read_text())
    os.chdir(ROOT)
    environment('postgres')
    os.environ['RADAR_FINAL_BACKUP_IDS'] = json.dumps([data['container']])
    counts = guard()
    result = asyncio.run(exercise(data))
    assert counts['process_denied'] == counts['postgres_config'] == 0
    result['isolation'] = counts
    print('CHASSE_BACKUP_RECIPE', json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.chasse_release_backup import backup_stopped, validate_schema, verify_archive


@pytest.mark.parametrize('schema', ['0014_alert_market_freshness', '0015_chasse_weekly_prices'])
def test_pre_migration_revision_is_preserved_instead_of_forced_to_head(schema):
    assert validate_schema(schema) == schema


def test_unknown_schema_is_refused():
    with pytest.raises(ValueError, match='Schéma'):
        validate_schema('unknown')


async def test_running_application_stops_backup_before_database_or_dump(tmp_path):
    from types import SimpleNamespace

    source = SimpleNamespace(assert_stopped=AsyncMock(side_effect=ValueError('Application active')),
                             connect=AsyncMock())
    with pytest.raises(ValueError, match='Application'):
        await backup_stopped(source, output=tmp_path/'backup',
                             expected_schema='0014_alert_market_freshness',
                             application_revision='a'*40, binary='/fake/gpg',
                             public_home=tmp_path, recipient='A'*40)
    source.connect.assert_not_called()
    assert not (tmp_path/'backup').exists()


def test_incomplete_archive_cannot_be_a_deployment_gate(tmp_path):
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_archive(tmp_path)
    (tmp_path/'manifest.json').write_text(json.dumps({'schema': '0014_alert_market_freshness'}))
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_archive(tmp_path)
    assert not list(tmp_path.glob('*.dump'))


def test_private_inputs_are_never_in_the_public_manifest_schema():
    source = Path('scripts/chasse_release_backup.py').read_text()
    assert 'get_secret_value' not in source
    assert 'quarantine_connection' not in source


def test_cli_failure_output_never_contains_private_configuration(tmp_path, monkeypatch, capsys):
    import sys

    from scripts import chasse_release_backup as tool

    config = {'app_container': 'a'*64, 'postgres_container': 'b'*64,
              'app_image': 'sha256:'+'c'*64, 'postgres_image': 'sha256:'+'d'*64,
              'database_url': 'postgresql://user:private-secret-marker@127.0.0.1:55514/test',
              'database_host': '127.0.0.1', 'database_port': 55514,
              'docker': '/fake/docker', 'docker_environment': {}}
    path = tmp_path/'private.json'
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setattr(sys, 'argv', ['backup', 'backup-stopped', '--folder', str(tmp_path/'out'),
                                    '--config', str(path),
                                    '--schema', '0014_alert_market_freshness',
                                    '--revision', 'a'*40, '--gpg', '/fake/gpg',
                                    '--key-home', str(tmp_path), '--recipient', 'A'*40])
    def refuse(*_args):
        raise ValueError('private-secret-marker')
    monkeypatch.setattr(tool.StoppedDatabase, '_inspect', refuse)
    assert tool.main() == 1
    assert 'private-secret-marker' not in capsys.readouterr().out


def test_busy_recipe_port_stops_before_any_docker_operation(monkeypatch):
    from scripts import verify_consolidation as recipe

    class BusySocket:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def bind(self, address):
            assert address == ('127.0.0.1', 55514)
            raise OSError('Port occupé')
    monkeypatch.setattr(recipe.socket, 'socket', BusySocket)
    with pytest.raises(RuntimeError, match='occupé'):
        recipe.postgres_parent()

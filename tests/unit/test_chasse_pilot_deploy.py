import json
import sys
from unittest.mock import Mock

import pytest

from scripts import deploy


def context(tmp_path, **overrides):
    values = {'host': 'ubuntu@example', 'key': '/private/key', 'remote_dir': '~/bot-radar',
              'repo_root': tmp_path, 'pilot': True, 'pilot_digest': 'd'*64,
              'pilot_manifest': tmp_path/'manifest.json'}
    values.update(overrides)
    return deploy.Context(**values)


def prepare_manifest(ctx):
    from app.utils.pilot_manifest import digest

    value = {'version': 1, 'scope': 'pilot_only', 'maximum_unique_collections': 7,
             'collection_isbn13': ['9782756079332', '9782205049657', '9782205051995',
                                  '9782205059250', '9782808500487', '9782413091424',
                                  '9791026856405'],
             'books': [], 'alerts': [], 'catalogue_repairs': [], 'isbn_aliases': []}
    value['digest'] = digest(value)
    ctx.pilot_manifest.write_text(json.dumps(value))
    ctx.pilot_digest = value['digest']


def test_pilot_preflight_only_needs_the_approved_manifest(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    prepare_manifest(ctx)
    runner = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(deploy.subprocess, 'run', runner)
    local = Mock(side_effect=AssertionError('Aucun outil de sauvegarde requis'))
    monkeypatch.setattr(deploy, '_run_local', local)
    assert deploy.step_preflight(ctx) == 0
    runner.assert_called_once()
    assert runner.call_args.args[0][1] == str(tmp_path/'scripts/preflight_deploy.py')
    local.assert_not_called()


def test_pilot_cli_accepts_manifest_and_digest_without_backup_config(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    prepare_manifest(ctx)
    steps = Mock(return_value=0)
    monkeypatch.setattr(deploy, 'run_steps', steps)
    monkeypatch.setattr(sys, 'argv', ['deploy', '--dry-run', '--pilot',
                                    '--pilot-manifest', str(ctx.pilot_manifest),
                                    '--pilot-digest', ctx.pilot_digest])
    assert deploy.main() == 0
    deployed = steps.call_args.args[1]
    assert deployed.pilot is True
    assert deployed.pilot_manifest == ctx.pilot_manifest
    assert deployed.pilot_digest == ctx.pilot_digest


def test_pilot_deployment_does_not_send_telegram_notification(tmp_path, monkeypatch):
    remote = Mock()
    remote.run.return_value = (0, '', '')
    ctx = context(tmp_path, remote=remote)
    monkeypatch.setattr(deploy, 'get_deployed_commits', lambda _: ('a'*40, ['Chasse']))
    assert deploy.step_notify(ctx) == 0
    for call in remote.run.call_args_list:
        assert 'notify_deploy.py' not in call.args[0]


def test_missing_pilot_manifest_refuses_before_remote_preflight(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    runner = Mock()
    monkeypatch.setattr(deploy.subprocess, 'run', runner)
    assert deploy.step_preflight(ctx) != 0
    runner.assert_not_called()


def test_pilot_state_is_installed_before_migration_and_start():
    labels = [label for label, _ in deploy.STEPS]
    assert labels.index('Activation du pilote') < labels.index('Migration')
    assert labels.index('Migration') < labels.index('Démarrage et santé')


@pytest.mark.parametrize('alteration', ['digest', 'scope'])
def test_invalid_pilot_manifest_still_refuses_before_remote_preflight(
    tmp_path, monkeypatch, alteration,
):
    ctx = context(tmp_path)
    prepare_manifest(ctx)
    if alteration == 'digest':
        ctx.pilot_digest = '0'*64
    else:
        data = json.loads(ctx.pilot_manifest.read_text())
        data['collection_isbn13'].append('9782205055641')
        ctx.pilot_manifest.write_text(json.dumps(data))
    runner = Mock()
    monkeypatch.setattr(deploy.subprocess, 'run', runner)
    assert deploy.step_preflight(ctx) != 0
    runner.assert_not_called()


def test_pilot_activation_keeps_the_persistent_guard_and_app_only_scope(tmp_path):
    remote = Mock()
    remote.run.return_value = (0, '', '')
    ctx = context(tmp_path, remote=remote)
    prepare_manifest(ctx)
    assert deploy.step_pilot_activate(ctx) == 0
    script = remote.run.call_args.args[0]
    assert 'pilot_mode.enable' in script
    assert 'CHASSE_PILOT_STATE_PATH' in script
    assert '--no-deps --pull never' in script
    assert ctx.pilot_digest in script


@pytest.mark.parametrize('missing', ['manifest', 'digest'])
def test_pilot_cli_still_requires_both_scope_arguments(tmp_path, monkeypatch, missing):
    ctx = context(tmp_path)
    prepare_manifest(ctx)
    arguments = ['deploy', '--dry-run', '--pilot']
    if missing != 'manifest':
        arguments += ['--pilot-manifest', str(ctx.pilot_manifest)]
    if missing != 'digest':
        arguments += ['--pilot-digest', ctx.pilot_digest]
    steps = Mock()
    monkeypatch.setattr(deploy, 'run_steps', steps)
    monkeypatch.setattr(sys, 'argv', arguments)
    with pytest.raises(SystemExit) as error:
        deploy.main()
    assert error.value.code == 2
    steps.assert_not_called()

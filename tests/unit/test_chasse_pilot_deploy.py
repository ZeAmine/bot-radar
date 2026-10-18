from unittest.mock import Mock

from scripts import deploy


def context(tmp_path, **overrides):
    values = {'host': 'ubuntu@example', 'key': '/private/key', 'remote_dir': '~/bot-radar',
              'repo_root': tmp_path, 'pilot': True, 'pilot_digest': 'd'*64,
              'pilot_manifest': tmp_path/'manifest.json',
              'pilot_backup_config': tmp_path/'backup-config.json'}
    values.update(overrides)
    return deploy.Context(**values)


def test_pilot_deployment_requires_explicit_backup_before_any_migration(tmp_path):
    ctx = context(tmp_path)
    steps = [label for label, _ in deploy.STEPS]
    assert steps.index('Sauvegarde pilote vérifiée') < steps.index('Migration')
    assert deploy.step_pilot_backup(ctx) != 0


def test_pilot_deployment_does_not_send_telegram_notification(tmp_path, monkeypatch):
    remote = Mock()
    remote.run.return_value = (0, '', '')
    ctx = context(tmp_path, remote=remote)
    monkeypatch.setattr(deploy, 'get_deployed_commits', lambda _: ('a'*40, ['Chasse']))
    assert deploy.step_notify(ctx) == 0
    for call in remote.run.call_args_list:
        assert 'notify_deploy.py' not in call.args[0]


def test_missing_pilot_backup_configuration_refuses_before_remote_preflight(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    runner = Mock()
    monkeypatch.setattr(deploy.subprocess, 'run', runner)
    assert deploy.step_preflight(ctx) != 0
    runner.assert_not_called()


def test_pilot_state_is_installed_before_migration_and_start():
    labels = [label for label, _ in deploy.STEPS]
    assert labels.index('Activation du pilote') < labels.index('Migration')
    assert labels.index('Migration') < labels.index('Démarrage et santé')

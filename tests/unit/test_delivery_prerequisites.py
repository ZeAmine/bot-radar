"""Garde-fous des seuls prérequis de livraison ; aucun service réel."""

import hashlib

import pytest

from scripts.delivery_prerequisites import maintenance_config, require_schema, verify_copy


def test_backup_refuses_a_different_schema():
    require_schema("0010_alert_preparation")
    with pytest.raises(ValueError, match="schéma"):
        require_schema("0013_pause_actions")


def test_maintenance_preserves_every_other_site():
    stream = "stream.example {\n reverse_proxy botstream_app:8080\n}\n"
    radar = (
        "radar.aminezegmou.fr {\n reverse_proxy radar_app:8000 {\n"
        ' header_up X-Test "{host}"\n }\n}\n'
    )
    before = "# gateway\n" + stream + radar + "# tail\n"
    after = maintenance_config(before)
    assert after.startswith("# gateway\n" + stream)
    assert after.endswith("# tail\n")
    assert 'respond "Radar en maintenance" 503' in after
    assert "radar_app:8000" not in after


@pytest.mark.parametrize(
    "source",
    [
        "",
        "other.example {\n}\n",
        "radar.aminezegmou.fr {\n",
        "radar.aminezegmou.fr {\n}\nradar.aminezegmou.fr {\n}\n",
    ],
)
def test_maintenance_refuses_missing_ambiguous_or_incomplete_site(source):
    with pytest.raises(ValueError, match="Site Radar|Bloc Radar"):
        maintenance_config(source)


def test_copy_integrity_is_not_a_decryption_claim(tmp_path):
    path = tmp_path / "archive.gpg"
    path.write_bytes(b"synthetic encrypted copy")
    expected = {
        "size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    verify_copy(path, expected)
    path.write_bytes(b"truncated")
    with pytest.raises(ValueError, match="Taille ou empreinte"):
        verify_copy(path, expected)


@pytest.mark.parametrize("problem", ["port", "volume", "label", "nonempty", "identity"])
def test_restore_attestation_refuses_every_wrong_target(monkeypatch, tmp_path, problem):
    from scripts.delivery_prerequisites import Database

    config = tmp_path / "docker"
    config.mkdir()
    db = Database("docker", "unix:///synthetic.sock", config, "a" * 64, "123")
    row = {
        "Id": "a" * 64,
        "State": {"Running": True},
        "Mounts": [],
        "HostConfig": {
            "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55530"}]},
            "Tmpfs": {"/var/lib/postgresql/data": "rw"},
        },
        "Config": {"Labels": {"botradar.recovery": "0010-isolated"}},
    }
    if problem == "port":
        row["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
    if problem == "volume":
        row["Mounts"] = [{"Type": "volume"}]
    if problem == "label":
        row["Config"]["Labels"] = {}
    monkeypatch.setattr(db, "inspect", lambda: row)

    def sql(query):
        if "pg_control_system" in query:
            return "999|160015" if problem == "identity" else "123|160015"
        return "1" if problem == "nonempty" else "0"

    monkeypatch.setattr(db, "sql", sql)
    with pytest.raises(
        ValueError, match="Identité SQL|point local|stockage existant|Label|non vide"
    ):
        db.attest(target=True)


def test_cron_candidate_disables_only_the_identified_backup():
    from scripts.delivery_prerequisites import cron_candidate

    source = (
        "MAILTO=synthetic@example.invalid\n"
        "0 4 * * * /home/ubuntu/bot-radar/scripts/backup_db.sh >> /tmp/fake.log 2>&1\n"
        "5 6 * * * /other/task\n"
    )
    changed = cron_candidate(source)
    assert changed.startswith("MAILTO=synthetic@example.invalid\n")
    assert changed.endswith("5 6 * * * /other/task\n")
    assert "# INACTIVE_BACKUP_INDEPENDENT_UNCONFIGURED: 0 4 * * *" in changed
    with pytest.raises(ValueError, match="Appel cron absent ou ambigu"):
        cron_candidate("5 6 * * * /other/task\n")
    with pytest.raises(ValueError, match="Appel cron absent ou ambigu"):
        cron_candidate(source + "1 4 * * * /home/ubuntu/bot-radar/scripts/backup_db.sh\n")

"""Contrats hors ligne du manifeste et de la rétention, sans données réelles."""

from datetime import UTC, datetime, timedelta

import pytest

from scripts.backup_recovery import freshness, retention_plan

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


def test_freshness_is_based_on_snapshot_start_not_transfer_end():
    manifest = {
        "snapshot_bound": (NOW - timedelta(hours=19)).isoformat(),
        "publication_finished": NOW.isoformat(),
        "publication": "simulated_verified",
    }
    result = freshness(manifest, NOW)
    assert result["stale"]
    assert not result["independent_verified"]


def test_retention_has_at_most_eight_utc_weekly_representatives():
    records = [
        {"id": str(day), "snapshot_bound": (NOW - timedelta(days=day)).isoformat()}
        for day in range(80)
    ]
    result = retention_plan(records, NOW)
    assert len(result["weekly"]) <= 8
    assert "0" in result["keep"]
    assert "79" not in result["keep"]
    assert result["deletion_executed"] is False


@pytest.mark.parametrize("value", ["invalid", "2026-10-12T12:00:00", "2027-01-01T00:00:00+00:00"])
def test_unknown_or_future_snapshot_time_is_rejected(value):
    with pytest.raises(ValueError, match="Horodatage|Instantané"):
        freshness({"snapshot_bound": value, "publication": "simulated_verified"}, NOW)


def test_no_archive_is_not_a_healthy_backup():
    assert freshness(None, NOW)["missing"]
    assert freshness(None, NOW)["stale"]


@pytest.mark.parametrize(
    ("delta", "stale"), [(timedelta(hours=18), False), (timedelta(hours=18, microseconds=1), True)]
)
def test_eighteen_hour_boundary(delta, stale):
    report = freshness(
        {"snapshot_bound": (NOW - delta).isoformat(), "publication": "simulated_verified"}, NOW
    )
    assert report["stale"] is stale
    assert not report["operational_ok"]


def bundle(path, identifier="a" * 32):
    """Fichier opaque artificiel : prouve les contrôles d'octets, pas le chiffrement."""
    import json

    from scripts.backup_recovery import SCHEMA, sha

    path.mkdir()
    data = b"synthetic opaque artifact"
    manifest = {
        "format": 1,
        "schema": SCHEMA,
        "postgres_major": 16,
        "id": identifier,
        "publication": "local_archive",
        "independent_verified": False,
        "snapshot_bound": NOW.isoformat(),
        "cipher_sha256": sha(data),
        "cipher_size": len(data),
    }
    (path / "bundle.gpg").write_bytes(data)
    (path / "manifest.json").write_text(json.dumps(manifest))
    (path / "COMPLETE").write_text(sha((path / "manifest.json").read_bytes()))
    return path


@pytest.mark.parametrize("failure", ["corrupt", "truncated", "incomplete", "version"])
def test_invalid_copies_are_refused(tmp_path, failure):
    import json

    from scripts.backup_recovery import sha, verify

    path = bundle(tmp_path / "copy")
    if failure == "corrupt":
        (path / "bundle.gpg").write_bytes(b"corrupt")
    elif failure == "truncated":
        (path / "bundle.gpg").write_bytes(b"synthetic")
    elif failure == "incomplete":
        (path / "COMPLETE").write_text("partial")
    else:
        row = json.loads((path / "manifest.json").read_bytes())
        row["postgres_major"] = 15
        (path / "manifest.json").write_text(json.dumps(row))
        (path / "COMPLETE").write_text(sha((path / "manifest.json").read_bytes()))
    with pytest.raises(ValueError, match="corrompue|tronquée|incomplète|incompatible"):
        verify(path)


def test_simulated_publication_never_overwrites_a_copy(tmp_path):
    from scripts.backup_recovery import publish_simulated, verify

    source = bundle(tmp_path / "source")
    destination = tmp_path / "simulation"
    destination.mkdir()
    published = publish_simulated(source, destination)
    before = (published / "bundle.gpg").read_bytes()
    with pytest.raises(FileExistsError):
        publish_simulated(source, destination)
    assert (published / "bundle.gpg").read_bytes() == before
    assert verify(published)["publication"] == "simulated_verified"
    assert not verify(published)["independent_verified"]


def test_failed_publication_preserves_an_earlier_valid_copy(tmp_path, monkeypatch):
    from scripts import backup_recovery as recovery

    destination = tmp_path / "simulation"
    destination.mkdir()
    earlier = recovery.publish_simulated(bundle(tmp_path / "first"), destination)
    subsequent = bundle(tmp_path / "second", identifier="b" * 32)

    def fail(*_args, **_kwargs):
        raise OSError("synthetic copy failure")

    monkeypatch.setattr(recovery.shutil, "copyfile", fail)
    with pytest.raises(OSError, match="synthetic copy failure"):
        recovery.publish_simulated(subsequent, destination)
    assert recovery.verify(earlier)["id"] == "a" * 32
    assert not (destination / ("b" * 32) / "COMPLETE").exists()


def test_absent_destination_is_not_independent_success(tmp_path):
    from scripts.backup_recovery import publish_simulated

    with pytest.raises(ValueError, match="Destination"):
        publish_simulated(bundle(tmp_path / "source"), tmp_path / "missing")


def test_retention_deadlines_have_explicit_utc_and_never_delete():
    records = [{"id": "one", "snapshot_bound": "2026-10-12T14:00:00+02:00"}]
    plan = retention_plan(records, NOW)
    assert plan["expires_utc"]["one"].endswith("+00:00")
    assert plan["deletion_executed"] is False

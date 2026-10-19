"""Fixtures partagées du prototype de correction locale."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.services.alert_fix import core

BUG_FILE = "app/utils/text.py"
TEST_PATH = "tests/unit/test_alert_fix_rehearsal_repro.py"

REPRO_TEST = '''"""Reproduction synthétique de la normalisation."""

from app.utils.text import normalize_text


def test_normalize_text_keeps_apostrophe_separator():
    assert normalize_text("l'été") == "l ete"
'''

SYNTHETIC_BUG_PATCH = """diff --git a/app/utils/text.py b/app/utils/text.py
--- a/app/utils/text.py
+++ b/app/utils/text.py
@@ -28,2 +28,2 @@ def normalize_text(text: str | None) -> str:
-    cleaned = re.sub(r"['’‘`ʼ]", " ", lowered)
+    cleaned = re.sub(r"['’‘`ʼ]", "", lowered)
     cleaned = re.sub(r"[-–—_]", " ", cleaned)
"""

SYNTHETIC_FIX_PATCH = """diff --git a/app/utils/text.py b/app/utils/text.py
--- a/app/utils/text.py
+++ b/app/utils/text.py
@@ -28,2 +28,2 @@ def normalize_text(text: str | None) -> str:
-    cleaned = re.sub(r"['’‘`ʼ]", "", lowered)
+    cleaned = re.sub(r"['’‘`ʼ]", " ", lowered)
     cleaned = re.sub(r"[-–—_]", " ", cleaned)
"""


def build_ticket(
    *,
    base_sha: str,
    allowed_files: tuple[str, ...] = (BUG_FILE, TEST_PATH),
    test_file: str = TEST_PATH,
    evidence_files: tuple[str, ...] = (BUG_FILE,),
    ticket_id: str | None = None,
    alert_id: str = "alert-rehearsal",
    now: datetime | None = None,
    ttl_seconds: int = 600,
    trace: dict[str, Any] | None = None,
    evidence_hashes: dict[str, str] | None = None,
) -> core.Ticket:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    if evidence_hashes is None:
        evidence_hashes = {
            path: core.sha256_bytes(
                b"def value() -> int:" + bytes([10]) + b"    return 1" + bytes([10])
            )
            if path == "app/example.py"
            else core.sha256_bytes((Path(__file__).resolve().parents[1] / path).read_bytes())
            for path in evidence_files
        }
    ticket = core.Ticket(
        schema_version=core.SCHEMA_VERSION,
        ticket_id=ticket_id or f"ticket-{uuid4().hex}",
        ticket_hash="",
        alert_id=alert_id,
        incident=core.Incident(
            status="ouvert",
            opened_at=moment - timedelta(minutes=10),
            commit=base_sha,
            severity="critical",
            reason="correspondances actionnables en retard",
        ),
        decision=core.Decision(
            kind="approuvé",
            decided_at=moment - timedelta(minutes=5),
            user_id=42,
        ),
        issued_at=moment,
        expires_at=moment + timedelta(seconds=ttl_seconds),
        base_sha=base_sha,
        allowed_files=allowed_files,
        test_file=test_file,
        evidence_files=evidence_files,
        evidence_hashes=evidence_hashes,
        provenance="synthetic_fixture",
        trace=trace
        or {
            "ingested": 12,
            "matches_created": 3,
            "matches_notified": 0,
            "blocked_actionable_due": 1,
            "non_terminal_due": 1,
            "timestamp": moment.isoformat(),
            "sample": [
                {
                    "match_id": "match-1",
                    "listing_id": "listing-1",
                    "source_name": "vinted",
                    "notification_state": "ready",
                    "attempt_count": 2,
                }
            ],
        },
        limits=core.Limits(),
    )
    return replace(ticket, ticket_hash=ticket.compute_hash())


def write_ticket(path: Path, ticket: core.Ticket) -> None:
    core.write_json_atomic(path, ticket.to_dict())


def recorded_payload() -> dict[str, Any]:
    return {
        "schema_version": core.SCHEMA_VERSION,
        "test": {
            "path": TEST_PATH,
            "content": REPRO_TEST,
            "cause": "séparateur apostrophe supprimé",
            "expected_failure": "normalize_text('l'été') renvoie 'lete'",
        },
        "fix": {
            "unified_diff": SYNTHETIC_FIX_PATCH,
            "files": [BUG_FILE],
            "cause": "séparateur apostrophe supprimé",
            "explanation": "rétablit le séparateur apostrophe",
        },
    }


def write_recorded(path: Path) -> None:
    core.write_json_atomic(path, recorded_payload())

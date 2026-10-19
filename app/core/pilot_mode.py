import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.utils.isbn import normalize_listing_isbn
from app.utils.pilot_manifest import validate_manifest

_source_isbn: ContextVar[str | None] = ContextVar("pilot_source_isbn", default=None)


class PilotBlockedError(RuntimeError):
    pass


def state_path() -> Path:
    return Path(settings.CHASSE_PILOT_STATE_PATH or "/var/lib/botradar/chasse-pilot/state.json")


def read_state(path: Path | None = None) -> dict[str, Any]:
    target = path or state_path()
    try:
        if target.is_symlink():
            raise ValueError
        value: dict[str, Any] = json.loads(target.read_text())
        if value.get("version") != 1 or value.get("mode") not in {"pilot", "released"}:
            raise ValueError
        validate_manifest(value["manifest"], value["digest"])
        if not isinstance(value.get("attempts"), dict):
            raise ValueError
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PilotBlockedError("État pilote absent ou invalide") from exc


def active() -> bool:
    path = state_path()
    if not settings.CHASSE_PILOT_STATE_PATH and not path.exists():
        return False
    try:
        return bool(read_state(path)["mode"] != "released")
    except PilotBlockedError:
        return True


def _save(path: Path, state: dict[str, Any]) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".pilot-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as exc:
        raise PilotBlockedError("Pilote déjà utilisé par un autre processus") from exc
    finally:
        os.close(descriptor)


def enable(path: Path, manifest: dict[str, Any], expected: str) -> None:
    validate_manifest(manifest, expected)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _locked(path):
        if path.exists() or path.is_symlink():
            existing = read_state(path)
            if existing["mode"] == "pilot" and existing["digest"] == expected:
                return
            raise PilotBlockedError("État pilote déjà présent ou différent")
        _save(path, {"version": 1, "mode": "pilot", "manifest": manifest, "digest": expected,
                     "attempts": {}, "halted": False, "consecutive_failures": 0})


def release(path: Path, expected: str, *, authorize: bool) -> None:
    with _locked(path):
        state = read_state(path)
        if not authorize or state["digest"] != expected:
            raise PilotBlockedError("Sortie de pilote non autorisée")
        if any(item.get("status") == "started" for item in state["attempts"].values()):
            raise PilotBlockedError("Collecte interrompue à examiner avant sortie")
        state["mode"] = "released"
        _save(path, state)


def resume(
    path: Path, expected: str, *, isbn: str, authorize: bool, not_before: datetime,
) -> None:
    with _locked(path):
        state = read_state(path)
        code = normalize_listing_isbn(isbn)
        attempt = state["attempts"].get(code)
        now = datetime.now(UTC)
        if (not authorize or state["digest"] != expected or state["mode"] != "pilot"
                or not state["halted"] or state.get("halt_reason") != "source_failure"
                or code not in state["manifest"]["collection_isbn13"]
                or not isinstance(attempt, dict) or attempt.get("status") != "failed"
                or state.get("recovery") or not_before.tzinfo is None or not_before > now
                or any(a.get("status") == "started" for a in state["attempts"].values())):
            raise PilotBlockedError("Reprise pilote non autorisée ou prématurée")
        state["recovery"] = {"isbn13": code, "authorized_at": now.isoformat(),
                             "not_before": not_before.isoformat(),
                             "previous_halt_reason": state["halt_reason"], "pending": True}
        state.update(halted=False, consecutive_failures=0, last_failure=[])
        state.pop("halt_reason", None)
        _save(path, state)


def require_isbn(raw: str | None) -> str | None:
    if not active():
        return normalize_listing_isbn(raw)
    code = normalize_listing_isbn(raw)
    path = state_path()
    with _locked(path):
        state = read_state(path)
        if code not in state["manifest"]["collection_isbn13"]:
            state.update(halted=True, halt_reason="scope_violation")
            _save(path, state)
            raise PilotBlockedError("Édition hors périmètre")
        if state["halted"]:
            raise PilotBlockedError("Pilote arrêté")
    return code


def require_source(raw: str | None) -> None:
    if active():
        code = require_isbn(raw)
        if code is None or _source_isbn.get() != code:
            raise PilotBlockedError("Collecte manuelle autorisée requise")


def was_attempted(raw: str | None) -> bool:
    if not active():
        return False
    state = read_state()
    code = normalize_listing_isbn(raw)
    recovery = state.get("recovery", {})
    return code in state["attempts"] and not (
        recovery.get("pending") and recovery.get("isbn13") == code
    )


@contextmanager
def collection_scope(raw: str | None) -> Iterator[None]:
    if not active():
        yield
        return
    code = require_isbn(raw)
    path = state_path()
    with _locked(path):
        state = read_state(path)
        previous = state["attempts"].get(code)
        recovery = state.get("recovery", {})
        retry = (previous is not None and previous.get("status") == "failed"
                 and recovery.get("pending") and recovery.get("isbn13") == code)
        if (previous is not None and not retry) or (
            previous is None and len(state["attempts"]) >= 7
        ):
            raise PilotBlockedError("Édition déjà tentée ou budget épuisé")
        if any(item.get("status") == "started" for item in state["attempts"].values()):
            raise PilotBlockedError("Une collecte est active ou interrompue : examen requis")
        state["attempts"][code] = {"status": "started"}
        if retry:
            state["attempts"][code]["previous_attempts"] = [previous]
            state["recovery"]["pending"] = False
        _save(path, state)
    token = _source_isbn.set(code)
    try:
        yield
    except BaseException:
        with _locked(path):
            state = read_state(path)
            state.update(halted=True, halt_reason="interrupted")
            _save(path, state)
        raise
    finally:
        _source_isbn.reset(token)


def finish_collection(
    status: str, failures: list[str], *, diagnostics: list[dict[str, Any]] | None = None,
) -> None:
    if not active():
        return
    code = _source_isbn.get()
    with _locked(state_path()):
        state = read_state()
        if code not in state["attempts"]:
            raise PilotBlockedError("Collecte non réservée")
        signature = sorted(set(failures))
        state["attempts"][code].update(status=status, failures=signature)
        if diagnostics is not None:
            state["attempts"][code]["diagnostics"] = diagnostics
        state["consecutive_failures"] = (
            state["consecutive_failures"] + 1
            if signature and signature == state.get("last_failure")
            else 1 if signature else 0
        )
        state["last_failure"] = signature
        if (set(signature) & {"rate_limited", "bot_challenge", "session_unavailable"}
                or state["consecutive_failures"] >= 2):
            state.update(halted=True, halt_reason="source_failure")
        _save(state_path(), state)


@contextmanager
def session_probe_scope(raw: str) -> Iterator[None]:
    code = require_isbn(raw)
    if not active():
        raise PilotBlockedError("Sonde réservée au pilote activé")
    with _locked(state_path()):
        state = read_state()
        count = state.get("session_probes", 0)
        if count >= 2:
            raise PilotBlockedError("Budget des sondes de session épuisé")
        state["session_probes"] = count + 1
        _save(state_path(), state)
    token = _source_isbn.set(code)
    try:
        yield
    except BaseException:
        halt("session_probe_failed")
        raise
    finally:
        _source_isbn.reset(token)


def halt(reason: str) -> None:
    with _locked(state_path()):
        state = read_state()
        state.update(halted=True, halt_reason=reason)
        _save(state_path(), state)

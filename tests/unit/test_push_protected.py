"""Tests du push protégé : ce que la fenêtre de protection ne doit jamais laisser derrière.

Une branche nue est un état qu'aucun test d'intégration ne rattrape : rien ne
signale qu'un `DELETE` a réussi et qu'aucun `PUT` n'a suivi. Ce qui est vérifié ici
est donc structurel — l'ordre des appels, le `finally`, et le refus de ce que le
script ne sait pas restaurer.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import push_protected  # noqa: E402
from push_protected import (  # noqa: E402
    OK,
    PUSH_FAILED,
    REFUSED,
    UNVERIFIED,
    GhProtection,
    check_local,
    diff_states,
    push_window,
    restore_payload,
    unrestorable,
)

# L'état tel que l'API le renvoie sur ce dépôt : URL de sous-ressources comprises.
CAPTURED = {
    "url": "https://api.github.com/repos/ZeAmine/bot-radar/branches/main/protection",
    "required_status_checks": {
        "url": "https://api.github.com/.../required_status_checks",
        "strict": False,
        "contexts": ["check"],
        "contexts_url": "https://api.github.com/.../contexts/check",
        "checks": [{"context": "check", "app_id": 15368}],
    },
    "enforce_admins": {"url": "https://api.github.com/.../enforce_admins", "enabled": True},
    "required_linear_history": {"enabled": False},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
    "block_creations": {"enabled": False},
    "required_conversation_resolution": {"enabled": False},
    "lock_branch": {"enabled": False},
    "allow_fork_syncing": {"enabled": False},
}


def _copy(state: dict) -> dict:
    return json.loads(json.dumps(state))


_UNSET = object()


class _FakeClient:
    """Enregistre l'ordre des appels et sert l'état capturé, sans toucher GitHub."""

    branch = "main"

    def __init__(self, state=_UNSET, push_code: int = 0, restore_code: int = 0) -> None:
        self.state_value = CAPTURED if state is _UNSET else state
        self.push_code = push_code
        self.restore_code = restore_code
        self.calls: list[str] = []
        self.restored_payload: dict | None = None

    def state(self):
        self.calls.append("state")
        return (0, self.state_value) if self.state_value is not None else (1, None)

    def remove(self) -> int:
        self.calls.append("remove")
        return 0

    def restore(self, payload) -> int:
        self.calls.append("restore")
        self.restored_payload = payload
        return self.restore_code


def test_successful_window_deletes_pushes_and_restores_in_that_order():
    """La séquence est celle de la procédure manuelle, et la branche se referme."""
    client = _FakeClient()
    calls: list[str] = []

    code = push_window(client, lambda: calls.append("push") or 0)

    assert code == OK
    assert calls == ["push"]
    assert client.calls == ["state", "remove", "restore", "state"]


def test_protection_is_restored_even_when_the_push_is_refused():
    """Un push rejeté laisse la branche exactement comme le script l'a trouvée."""
    client = _FakeClient(push_code=1)

    assert push_window(client, lambda: 1) == PUSH_FAILED
    assert "restore" in client.calls


def test_protection_is_restored_when_the_push_raises():
    """Un Ctrl-C ou une exception ne laisse pas la branche nue, et n'est pas avalé."""
    client = _FakeClient()

    def _interrupted() -> int:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        push_window(client, _interrupted)

    assert "restore" in client.calls


def test_a_failed_restore_is_reported_rather_than_hidden():
    """Un PUT en échec s'affiche bruyamment : c'est le seul cas à reprendre à la main."""
    client = _FakeClient(restore_code=1)

    assert push_window(client, lambda: 0) == UNVERIFIED


def test_a_restored_state_that_differs_is_reported(capsys):
    """Le PUT répond 200 même quand `allow_fork_syncing` n'a pas pris : seul le GET le voit."""
    diverged = _copy(CAPTURED)
    diverged["allow_fork_syncing"] = {"enabled": True}
    client = _FakeClient()
    states = [CAPTURED, diverged]
    client.state = lambda: (0, states.pop(0))  # type: ignore[method-assign]

    assert push_window(client, lambda: 0) == UNVERIFIED
    assert "allow_fork_syncing" in capsys.readouterr().out


def test_unprotected_branch_pushes_without_touching_anything():
    """Sans protection à lever, le script ne fait que pousser."""
    client = _FakeClient(state=None)
    calls: list[str] = []

    assert push_window(client, lambda: calls.append("push") or 0) == OK
    assert calls == ["push"]
    assert client.calls == ["state"]


def test_dry_run_never_deletes_nor_pushes(capsys):
    """La simulation relit le plan, y compris le corps du PUT à rejouer à la main."""
    client = _FakeClient()

    assert push_window(client, lambda: 1, dry_run=True) == OK
    assert client.calls == ["state"]
    assert '"check"' in capsys.readouterr().out


def test_a_state_the_script_cannot_restore_is_refused_before_any_deletion():
    """`required_signatures` ne se règle pas par ce PUT : on refuse, on ne tente rien."""
    signed = _copy(CAPTURED)
    signed["required_signatures"] = {"enabled": True}
    client = _FakeClient(state=signed)

    assert push_window(client, lambda: 0) == REFUSED
    assert client.calls == ["state"]


def test_payload_keeps_the_status_check_and_drops_the_urls():
    """Le PUT ne supporte pas les URL du GET : elles feraient échouer la restauration."""
    payload = restore_payload(CAPTURED)

    assert payload["required_status_checks"] == {
        "strict": False,
        "contexts": ["check"],
        "checks": [{"context": "check", "app_id": 15368}],
    }
    assert payload["enforce_admins"] == {"enabled": True}
    assert payload["allow_force_pushes"] == {"enabled": False}
    assert "url" not in json.dumps(payload)


def test_payload_round_trips_to_an_identical_state():
    """Aller-retour : l'état remis est celui qu'on a lu, pas une version dégradée."""
    round_tripped = json.loads(json.dumps(restore_payload(CAPTURED)))

    assert diff_states(CAPTURED, round_tripped) == []


def test_diff_names_the_setting_that_drifted():
    """Un écart doit être nommé, pas seulement compté."""
    after = _copy(CAPTURED)
    after["enforce_admins"] = {"enabled": False}

    assert diff_states(CAPTURED, after) == [
        "enforce_admins : attendu {'enabled': True}, obtenu {'enabled': False}"
    ]


def test_unrestorable_ignores_a_disabled_signature_requirement():
    """Un réglage présent mais désactivé se restaite très bien."""
    assert unrestorable(CAPTURED) == []
    assert unrestorable({}) == []


def test_local_check_refuses_a_dirty_tree(monkeypatch):
    """Pousser un arbre sale deployingait autre chose que GitHub : le pré-vol le refuse."""
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return 0, " M app/bot/handlers/status.py", ""

    monkeypatch.setattr(push_protected, "run", fake_run)
    code, reason = check_local()

    assert code == REFUSED
    assert "non commité" in reason
    # `gh` n'est même pas interrogé : rien n'a été touché.
    assert [call[0] for call in calls] == ["git"]


def test_local_check_passes_on_a_clean_tree(monkeypatch):
    def fake_run(*_args, **_kwargs):
        return 0, "", ""

    monkeypatch.setattr(push_protected, "run", fake_run)

    assert check_local() == (OK, "")


def test_client_hits_the_branch_protection_endpoint():
    """L'endpoint porte le dépôt et la branche : une faute de frappe resterait invisible."""
    client = GhProtection(repo="o/r", branch="trunk")

    assert client.path == "repos/o/r/branches/trunk/protection"

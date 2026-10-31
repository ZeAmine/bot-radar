"""La notification de mise en ligne, seule étape non bloquante de la chaîne.

Elle est envoyée depuis le conteneur qui vient d'être recréé, avec sa
configuration : aucun secret ne traverse la chaîne de déploiement pour cela.
Ces tests tiennent les deux propriétés qui comptent — rien n'est envoyé quand
Telegram n'est pas configuré, et un échec d'envoi se voit au lieu d'être avalé.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from scripts import notify_deploy


def _settings(monkeypatch, token: str = "jeton-factice", ids: list[int] | None = None):
    fake = SimpleNamespace(
        TELEGRAM_BOT_TOKEN=token,
        TELEGRAM_ALLOWED_USER_IDS=[7] if ids is None else ids,
    )
    monkeypatch.setattr(notify_deploy, "settings", fake)
    return fake


def _client(monkeypatch, status_code: int = 200) -> Mock:
    """Un client HTTP factice : aucune requête ne sort de la machine."""
    post = Mock(return_value=SimpleNamespace(status_code=status_code, text=""))
    builder = MagicMock()
    builder.return_value.__enter__.return_value = SimpleNamespace(post=post)
    monkeypatch.setattr(notify_deploy.httpx, "Client", builder)
    return post


def test_unconfigured_notification_sends_nothing_and_says_so(monkeypatch) -> None:
    _settings(monkeypatch, token="")
    post = _client(monkeypatch)

    assert notify_deploy.send_telegram_notification("mise en ligne") is False
    post.assert_not_called()


def test_dry_run_shows_the_message_without_posting(monkeypatch) -> None:
    _settings(monkeypatch)
    post = _client(monkeypatch)

    assert notify_deploy.send_telegram_notification("mise en ligne", dry_run=True) is True
    post.assert_not_called()


def test_the_message_reaches_each_allowed_user(monkeypatch) -> None:
    _settings(monkeypatch, ids=[7, 8])
    post = _client(monkeypatch)

    assert notify_deploy.send_telegram_notification("<b>265c80d → 23092c0</b>") is True
    assert [call.kwargs["json"]["chat_id"] for call in post.call_args_list] == [7, 8]
    assert post.call_args_list[0].kwargs["json"]["parse_mode"] == "HTML"


def test_a_refused_delivery_is_reported_not_swallowed(monkeypatch) -> None:
    _settings(monkeypatch, ids=[7, 8])
    post = _client(monkeypatch, status_code=403)

    assert notify_deploy.send_telegram_notification("mise en ligne") is False
    assert post.call_count == 2


def test_empty_message_is_not_sent(monkeypatch) -> None:
    _settings(monkeypatch)
    post = _client(monkeypatch)

    assert notify_deploy.send_telegram_notification("   ") is True
    post.assert_not_called()


@pytest.mark.parametrize("chat_id", [7])
def test_a_transport_error_on_one_chat_does_not_hide_the_other(monkeypatch, chat_id) -> None:
    _settings(monkeypatch, ids=[chat_id, 8])
    builder = MagicMock()
    client = builder.return_value.__enter__.return_value
    client.post.side_effect = [
        RuntimeError("réseau indisponible"),
        SimpleNamespace(status_code=200, text=""),
    ]
    monkeypatch.setattr(notify_deploy.httpx, "Client", builder)

    assert notify_deploy.send_telegram_notification("mise en ligne") is False
    assert client.post.call_count == 2

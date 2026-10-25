"""Contrat commun à tout moteur IA, exécuté pour chaque adaptateur enregistré.

C'est ce fichier qui rend vérifiable la promesse « changer de modèle ne demande
aucun changement applicatif ». Tout adaptateur ajouté à `LLM_ADAPTERS` passe ici
sans modification : même repli, même forme de santé, même cache court. Le test de
vision compare le corps de requête réellement émis, donc un adaptateur qui
n'enverrait pas les images serait refusé ici, pas en production.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.logging import JSONFormatter
from app.providers.llm import base as llm_base
from app.providers.llm.base import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_UNCONFIGURED,
    LLMHealth,
    clean_and_parse_json,
)
from app.providers.llm.registry import LLM_ADAPTERS, build_llm_client

ADAPTER_NAMES = sorted(LLM_ADAPTERS)
NETWORK_HEALTH_NAMES = [name for name in ADAPTER_NAMES if name != "river"]

# Réponse brute de chaque adaptateur, dans son propre format filaire.
RAW_RESPONSES: dict[str, dict[str, Any]] = {
    "river": {"choices": [{"message": {"content": '{"prix": 12}'}}]},
    "openai": {"choices": [{"message": {"content": '{"prix": 12}'}}]},
    "deepseek": {"choices": [{"message": {"content": '{"prix": 12}'}}]},
    "openai-compatible": {"choices": [{"message": {"content": '{"prix": 12}'}}]},
    "anthropic": {"content": [{"type": "text", "text": '{"prix": 12}'}]},
}


class FakeNetwork:
    """Transport HTTP simulé, partagé par tous les appels d'un test."""

    def __init__(self) -> None:
        self.captured: list[httpx.Request] = []
        self.status_code = 200
        self.text: str | None = None

    def answer(self, adapter: str) -> dict[str, Any] | str:
        if self.text is not None:
            if adapter in RAW_RESPONSES:
                content = self.text
                if adapter == "anthropic":
                    return {"content": [{"type": "text", "text": content}]}
                return {"choices": [{"message": {"content": content}}]}
            return self.text
        return RAW_RESPONSES[adapter]

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.captured.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, text="échec simulé")
        return httpx.Response(200, json=self.answer(self.adapter_name))

    def get_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: self.handler(request))
        )

    adapter_name: str = "openai-compatible"


@pytest.fixture
async def network(monkeypatch: pytest.MonkeyPatch):
    """Remplace le client HTTP partagé par un transport simulé, quel que soit l'appelant."""
    fake = FakeNetwork()
    # Les adaptateurs lient la fonction à l'import : c'est là qu'il faut substituer.
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_API_KEY", SecretStr("test-key"))
    async with fake.get_client() as shared:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: shared)
        yield fake


def make_client(
    network: FakeNetwork,
    name: str,
    *,
    api_key: str = "test-key",
    model: str = "test-model",
) -> Any:
    """Construit un adaptateur du registre, indépendant du singleton global."""
    network.adapter_name = name
    client = build_llm_client(name)
    client.model = model
    client.api_key = api_key
    return client


@pytest.mark.parametrize("name", ADAPTER_NAMES)
def test_adapter_honours_the_protocol(name: str, network: FakeNetwork) -> None:
    """Chaque adaptateur expose le contrat attendu du projet."""
    client = make_client(network, name)
    assert client.vendor == name
    assert client.model == "test-model"
    assert client.is_configured is True


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.asyncio
async def test_complete_returns_text(name: str, network: FakeNetwork) -> None:
    """`complete` isole le texte produit, quel que soit le format de réponse."""
    client = make_client(network, name)

    text = await client.complete(system_prompt="système", user_prompt="utilisateur")

    assert text == '{"prix": 12}'
    assert network.captured[0].method == "POST"
    headers = network.captured[0].headers
    assert headers.get("authorization") or headers.get("x-api-key")


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.asyncio
async def test_generate_json_parses_object(name: str, network: FakeNetwork) -> None:
    """`generate_json` rend l'objet, y compris quand le modèle l'entoure d'un bloc."""
    network.text = '```json\n{"prix": 12}\n```'
    client = make_client(network, name)

    assert await client.generate_json("système", "utilisateur") == {"prix": 12}


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.asyncio
async def test_http_error_falls_back_to_none(name: str, network: FakeNetwork) -> None:
    """Une erreur HTTP rend `None` : l'IA ne fait jamais échouer le bot."""
    network.status_code = 500
    client = make_client(network, name)

    assert await client.complete(system_prompt="s", user_prompt="u") is None
    assert await client.generate_json("s", "u") is None


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.parametrize("body", [
    "<html>synthetic-private-marker</html>",
    '{"choices": 4, "content": 4}',
    '{"choices": {"wrong": 1}, "content": [{"type": "text", "text": 42}]}',
    '{"choices": [], "content": [], "echo": "synthetic-private-marker"}',
])
async def test_invalid_wire_response_is_absorbed_without_logging_body(name, body, network, caplog):
    client = make_client(network, name)

    def respond(request):
        network.captured.append(request)
        return httpx.Response(200, text=body)

    network.handler = respond
    assert await client.complete(system_prompt="s", user_prompt="u") is None
    assert await client.generate_json("s", "u") is None
    assert len(network.captured) == 2  # Un POST par appel, aucune tentative supplémentaire.
    assert "synthetic-private-marker" not in caplog.text


@pytest.mark.parametrize("name", ADAPTER_NAMES)
async def test_http_error_never_logs_external_body(name, network, caplog):
    client = make_client(network, name)

    def respond(request):
        network.captured.append(request)
        return httpx.Response(403, text="synthetic-private-body")

    network.handler = respond
    assert await client.complete(system_prompt="s", user_prompt="u") is None
    assert len(network.captured) == 1
    assert "synthetic-private-body" not in caplog.text


@pytest.mark.parametrize("name", NETWORK_HEALTH_NAMES)
async def test_health_message_only_attests_the_http_probe(name, network):
    health = await make_client(network, name).check_health(force=True)
    assert health.status == "active"
    assert health.message.startswith("Sonde HTTP accessible")


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.asyncio
async def test_unusable_response_falls_back_to_none(name: str, network: FakeNetwork) -> None:
    """Une réponse sans contenu exploitable rend `None` sans lever."""
    network.text = "   "
    client = make_client(network, name)

    assert await client.complete(system_prompt="s", user_prompt="u") is None


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.asyncio
async def test_unconfigured_client_never_calls_network(
    name: str, network: FakeNetwork
) -> None:
    """Sans clé API, aucun appel réseau n'est tenté."""
    client = make_client(network, name, api_key="  ")

    assert client.is_configured is False
    assert await client.generate_json("s", "u") is None
    assert network.captured == []


@pytest.mark.parametrize("name", NETWORK_HEALTH_NAMES)
@pytest.mark.asyncio
async def test_health_reports_active_then_caches(name: str, network: FakeNetwork) -> None:
    """La sonde répond, puis se met en cache pour ne pas frapper à chaque `/status`."""
    client = make_client(network, name)

    first = await client.check_health()
    assert isinstance(first, LLMHealth)
    assert first.status == STATUS_ACTIVE
    assert first.model == "test-model"
    assert first.latency_ms is not None
    assert first.as_dict() == {
        "status": STATUS_ACTIVE,
        "model": "test-model",
        "latency_ms": first.latency_ms,
        "message": first.message,
    }

    second = await client.check_health()
    assert second is first
    assert len(network.captured) == 1, "la sonde ne doit pas être rejouée dans les 30 secondes"


@pytest.mark.parametrize("name", NETWORK_HEALTH_NAMES)
@pytest.mark.asyncio
async def test_health_states(
    name: str, network: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Les quatre états de santé sont exposés : désactivé, sans clé, actif, en erreur."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    assert (await make_client(network, name).check_health()).status == STATUS_DISABLED

    monkeypatch.setattr(settings, "AI_ENABLED", True)
    without_key = make_client(network, name, api_key="")
    assert (await without_key.check_health()).status == STATUS_UNCONFIGURED

    network.status_code = 200
    active = make_client(network, name)
    assert (await active.check_health()).status == STATUS_ACTIVE

    network.status_code = 401
    assert (await make_client(network, name).check_health()).status == STATUS_ERROR

    network.status_code = 503
    assert (await make_client(network, name).check_health()).status == STATUS_ERROR


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.asyncio
async def test_vision_is_translated_in_the_wire_format(name: str, network: FakeNetwork) -> None:
    """Chaque adaptateur envoie les images dans sa propre forme filaire.

    L'adaptateur traduit, pas l'appelant : sans cette assertion, un fournisseur non
    compatible OpenAI renverrait silencieusement une analyse aveugle.
    """
    client = make_client(network, name)

    await client.complete(
        system_prompt="système",
        user_prompt="que vois-tu ?",
        image_urls=["https://images.example.test/1.jpg", "ftp://local/2.jpg"],
    )

    body = json.loads(network.captured[0].content)
    if name == "anthropic":
        assert body["messages"][0]["content"][1] == {
            "type": "image",
            "source": {"type": "url", "url": "https://images.example.test/1.jpg"},
        }
        # L'API Messages n'a pas de mode JSON : la contrainte vient du prompt système.
        assert "response_format" not in body
        assert body["max_tokens"] > 0
    else:
        assert body["messages"][1]["content"][1] == {
            "type": "image_url",
            "image_url": {"url": "https://images.example.test/1.jpg"},
        }
        assert body["response_format"] == {"type": "json_object"}
    # Le filtrage des URL non HTTP est commun : une source locale ne part jamais.
    assert "ftp://" not in json.dumps(body)


def test_clean_and_parse_json_rejects_non_objects() -> None:
    """Le nettoyage est commun : une liste ou du texte ne devient jamais un résultat."""
    assert clean_and_parse_json("[1, 2]") is None
    assert clean_and_parse_json("pas du json") is None
    assert clean_and_parse_json("") is None
    assert clean_and_parse_json('{"a": 1}') == {"a": 1}


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.parametrize("content", [
    "not JSON synthetic-private-marker",
    '["synthetic-private-marker"]',
])
async def test_invalid_generated_json_never_logs_content(name, content, network, caplog):
    network.text = content
    client = make_client(network, name)
    assert await client.generate_json("s", "u") is None
    assert len(network.captured) == 1
    assert "synthetic-private-marker" not in caplog.text
    assert "synthetic-private-marker" not in "".join(
        JSONFormatter().format(record) for record in caplog.records
    )
    assert any(getattr(record, "event", None) == "llm_json" for record in caplog.records)


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.parametrize(("error", "outcome"), [
    (httpx.ReadTimeout, "timeout"),
    (httpx.ConnectError, "transport_error"),
    (RuntimeError, "unexpected_error"),
])
async def test_transport_failures_are_classified_without_retry(
    name, error, outcome, network, caplog,
):
    client = make_client(network, name)
    client.timeout = 12.0

    def respond(request):
        network.captured.append(request)
        raise error("synthetic-private-marker")

    network.handler = respond
    with caplog.at_level(logging.INFO):
        assert await client.complete(system_prompt="s", user_prompt="u", timeout=0.75) is None
    assert len(network.captured) == 1
    assert network.captured[0].extensions["timeout"] == {
        "connect": 0.75, "read": 0.75, "write": 0.75, "pool": 0.75,
    }
    records = [r for r in caplog.records if getattr(r, "event", None) == "llm_request"]
    assert len(records) == 1
    record = records[0]
    assert record.outcome == outcome
    assert record.error_type == error.__name__
    assert record.timeout_seconds == 0.75
    assert record.duration_ms >= 0
    assert "synthetic-private-marker" not in JSONFormatter().format(record)


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.parametrize("status", [400, 401, 403, 408, 429, 500, 503])
async def test_http_status_has_one_attempt_and_safe_diagnostics(name, status, network, caplog):
    network.status_code = status
    client = make_client(network, name)
    assert await client.generate_json("s", "u") is None
    assert len(network.captured) == 1
    records = [r for r in caplog.records if getattr(r, "event", None) == "llm_request"]
    assert len(records) == 1
    assert records[0].outcome == "http_error"
    assert records[0].http_status == status
    assert records[0].duration_ms >= 0


@pytest.mark.parametrize("reason", [
    "length", "content_filter", "insufficient_system_resource", "aborted",
])
async def test_openai_compatible_rejects_interrupted_generation(reason, network, caplog):
    client = make_client(network, "openai-compatible")

    def respond(request):
        network.captured.append(request)
        return httpx.Response(200, json={"choices": [{
            "finish_reason": reason,
            "message": {"content": '{"prix":12}'},
        }]})

    network.handler = respond
    assert await client.generate_json("s", "u") is None
    assert len(network.captured) == 1
    assert any(getattr(r, "outcome", None) == "interrupted" for r in caplog.records)


@pytest.mark.parametrize("reason", [None, "stop"])
async def test_openai_compatible_accepts_normal_or_legacy_generation(reason, network):
    client = make_client(network, "openai-compatible")
    choice = {"message": {"content": '{"prix":12}'}}
    if reason is not None:
        choice["finish_reason"] = reason

    def respond(request):
        network.captured.append(request)
        return httpx.Response(200, json={"choices": [choice]})

    network.handler = respond
    assert await client.generate_json("s", "u") == {"prix": 12}
    assert len(network.captured) == 1


async def test_openai_compatible_request_preserves_current_settings(network, caplog):
    client = make_client(network, "openai-compatible")
    client.base_url = "https://example.invalid/v1"
    client.timeout = 3.5
    with caplog.at_level(logging.INFO):
        assert await client.generate_json("JSON système", "utilisateur", 0.3) == {"prix": 12}
    assert len(network.captured) == 1
    request = network.captured[0]
    assert str(request.url) == "https://example.invalid/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "JSON système"},
            {"role": "user", "content": "utilisateur"},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": settings.AI_MAX_TOKENS,
    }
    assert request.extensions["timeout"]["read"] == 3.5
    records = [r for r in caplog.records if getattr(r, "event", None) == "llm_request"]
    assert len(records) == 1
    assert records[0].outcome == "text_received"
    assert records[0].duration_ms >= 0
    assert "test-key" not in "".join(JSONFormatter().format(r) for r in caplog.records)


@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.parametrize("malformed", [False, True])
async def test_wire_shape_and_empty_content_have_distinct_outcomes(
    name, malformed, network, caplog,
):
    client = make_client(network, name)
    network.text = "   "
    if malformed:
        def respond(request):
            network.captured.append(request)
            return httpx.Response(200, json={"choices": 42, "content": 42})
        network.handler = respond
    assert await client.complete(system_prompt="s", user_prompt="u") is None
    assert len(network.captured) == 1
    expected = "invalid_envelope" if malformed else "empty_content"
    assert any(getattr(r, "outcome", None) == expected for r in caplog.records)


@pytest.mark.parametrize("name", ADAPTER_NAMES)
async def test_cancellation_is_propagated_without_retry(name, network, caplog):
    client = make_client(network, name)

    def respond(request):
        network.captured.append(request)
        raise asyncio.CancelledError

    network.handler = respond
    with pytest.raises(asyncio.CancelledError):
        await client.complete(system_prompt="s", user_prompt="u")
    assert len(network.captured) == 1
    assert not any(getattr(r, "outcome", None) == "text_received" for r in caplog.records)

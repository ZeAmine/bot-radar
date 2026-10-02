"""Contrat commun à tout moteur IA, exécuté pour chaque adaptateur enregistré.

C'est ce fichier qui rend vérifiable la promesse « changer de modèle ne demande
aucun changement applicatif ». Tout adaptateur ajouté à `LLM_ADAPTERS` passe ici
sans modification : même repli, même forme de santé, même cache court. Le test de
vision compare le corps de requête réellement émis, donc un adaptateur qui
n'enverrait pas les images serait refusé ici, pas en production.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
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

# Réponse brute de chaque adaptateur, dans son propre format filaire.
RAW_RESPONSES: dict[str, dict[str, Any]] = {
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
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    adapter_name: str = "openai-compatible"


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> FakeNetwork:
    """Remplace le client HTTP partagé par un transport simulé, quel que soit l'appelant."""
    fake = FakeNetwork()
    # Les adaptateurs lient la fonction à l'import : c'est là qu'il faut substituer.
    monkeypatch.setattr(llm_base, "get_shared_http_client", fake.get_client)
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_API_KEY", SecretStr("test-key"))
    return fake


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


@pytest.mark.parametrize("name", ADAPTER_NAMES)
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


@pytest.mark.parametrize("name", ADAPTER_NAMES)
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

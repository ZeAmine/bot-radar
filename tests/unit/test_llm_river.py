import json
import logging

import httpx
import pytest

from app.core.config import Settings, settings
from app.core.logging import JSONFormatter
from app.providers.llm import base as llm_base
from app.providers.llm.registry import build_llm_client
from app.services.ai.modules.negotiation import NegotiationModule
from app.services.ai.schemas import KeywordVariantsPayload
from tests.fakes_llm import FakeLLM

RIVER_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"


@pytest.fixture
def river(monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_REASONING_EFFORT", "none")
    monkeypatch.setattr(settings, "AI_MAX_TOKENS", 2048)
    monkeypatch.setattr(settings, "AI_MODEL", RIVER_MODEL)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.river.ai/v1")
    client = build_llm_client("river")
    client.api_key = "synthetic-river-key"
    return client


def test_river_defaults_choose_one_inference_provider():
    assert Settings.model_fields["AI_PROVIDER"].default == "river"
    assert Settings.model_fields["AI_MODEL"].default == RIVER_MODEL
    assert Settings.model_fields["AI_BASE_URL"].default == "https://api.river.ai/v1"


async def test_river_request_matches_documented_chat_contract(river, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"variants":[],"exclusions":null,"rationale":null}',
        }}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        result = await river.generate_json("JSON système", "utilisateur", 0.3,
                                           response_model=KeywordVariantsPayload,
                                           ai_module="keyword_variants")
    assert result == {"variants": [], "exclusions": None, "rationale": None}
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://api.river.ai/v1/chat/completions"
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer synthetic-river-key"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert set(body) == {
        "model", "messages", "temperature", "response_format", "max_tokens", "reasoning_effort",
    }
    assert body["model"] == RIVER_MODEL
    assert body["messages"] == [
        {"role": "system", "content": "JSON système"},
        {"role": "user", "content": "utilisateur"},
    ]
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 2048
    assert body["reasoning_effort"] == "none"
    assert body["response_format"]["type"] == "json_schema"
    contract = body["response_format"]["json_schema"]
    assert contract["name"] == "KeywordVariantsPayload"
    assert contract["strict"] is True
    assert contract["schema"]["additionalProperties"] is False
    assert set(contract["schema"]["required"]) == {"variants", "exclusions", "rationale"}
    assert "thinking" not in body
    assert "max_completion_tokens" not in body
    assert "reasoning" not in body


async def test_river_usage_keeps_subsets_without_adding_them_twice(river, monkeypatch, caplog):
    envelope = {
        "choices": [{"message": {"content": '{"variants":[]}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40,
                  "prompt_tokens_details": {"cached_tokens": 80},
                  "completion_tokens_details": {"reasoning_tokens": 10}},
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=envelope),
    )) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        with caplog.at_level(logging.INFO):
            assert await river.generate_json("JSON", "u", response_model=KeywordVariantsPayload,
                                             ai_module="keyword_variants") is not None
    record = next(r for r in caplog.records if getattr(r, "event", None) == "llm_request")
    output = json.loads(JSONFormatter().format(record))
    assert output["provider"] == "river"
    assert output["model"] == RIVER_MODEL
    assert output["usage"] == {"input": 100, "output": 40, "cached": 80, "reasoning": 10}
    assert output["status"] == "ok"
    assert output["attempt"] == 1
    assert not any("cost" in key or "price" in key for key in output)


@pytest.mark.parametrize("failure", [
    400, 401, 402, 403, 404, 408, 413, 422, 429, 500, 502, 503, 504,
    "timeout", "empty", "invalid_json", "validation_error", "truncated",
])
async def test_river_failure_never_retries_or_falls_back_to_another_provider(
    river, failure, monkeypatch, caplog,
):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "api.river.ai"
        if failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE_EXCEPTION")
        if isinstance(failure, int):
            return httpx.Response(failure, headers={"Retry-After": "1"},
                                  text="PRIVATE_ERROR_BODY")
        content = {"empty": " ", "invalid_json": "PRIVATE_INVALID_JSON",
                   "validation_error": '{"variants":["PRIVATE_VALUE",42]}',
                   "truncated": '{"variants":[]}'}[failure]
        return httpx.Response(200, json={"choices": [{
            "message": {"content": content},
            "finish_reason": "length" if failure == "truncated" else "stop",
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        assert await river.generate_json("JSON PRIVATE_PROMPT", "PRIVATE_INPUT",
                                         response_model=KeywordVariantsPayload) is None
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["model"] == RIVER_MODEL
    assert body["response_format"]["type"] == "json_schema"
    record = next(r for r in caplog.records if getattr(r, "event", None) == "llm_request")
    assert record.status == ("http_error" if isinstance(failure, int) else {
        "timeout": "timeout", "empty": "empty_content", "invalid_json": "invalid_json",
        "validation_error": "validation_error", "truncated": "interrupted",
    }[failure])
    output = "".join(JSONFormatter().format(r) for r in caplog.records)
    for private in ("synthetic-river-key", "PRIVATE_EXCEPTION", "PRIVATE_ERROR_BODY",
                    "PRIVATE_INVALID_JSON", "PRIVATE_VALUE", "PRIVATE_PROMPT", "PRIVATE_INPUT"):
        assert private not in output


async def test_river_health_is_one_models_get_without_generation(river, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": RIVER_MODEL}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        assert (await river.check_health()).status == "active"
        assert (await river.check_health()).status == "active"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "https://api.river.ai/v1/models"
    assert requests[0].headers["authorization"] == "Bearer synthetic-river-key"


async def test_river_failure_preserves_deterministic_offer(river, monkeypatch):
    from app.db.models.listing import Listing

    listing = Listing(title="Console", item_price_cents=10000, estimated_total_cents=10000,
                      category_slug="gaming")
    expected = await NegotiationModule(client=FakeLLM(enabled=False)).generate_offer(listing)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(429, text="PRIVATE_ERROR_BODY")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        result = await NegotiationModule(client=river).generate_offer(listing)
    assert result == expected
    assert len(requests) == 1
    assert requests[0].url.host == "api.river.ai"

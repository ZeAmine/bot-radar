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


async def test_river_health_is_unverified_without_any_network_request(river, monkeypatch):
    def forbidden():
        pytest.fail("Une lecture de santé passive ne doit pas ouvrir de transport.")

    monkeypatch.setattr(llm_base, "get_shared_http_client", forbidden)
    for force in (False, True):
        health = await river.check_health(force=force)
        assert health.status == "unverified"
        assert health.as_dict()["configured"] is True
        assert health.as_dict()["last_call"] is None


@pytest.mark.parametrize("status", [200, 401, 403])
async def test_river_passive_health_records_real_completion_and_exact_status(
    river, status, monkeypatch, caplog,
):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "POST"
        body = {"choices": [{"message": {"content": '{"variants":[]}'},
                             "finish_reason": "stop"}]} if status == 200 else {
            "error": {"code": "invalid_api_key", "type": "authentication_error",
                      "message": "PRIVATE_ERROR_BODY"},
        }
        return httpx.Response(status, json=body, headers={"x-request-id": "req_synthetic_123"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        with caplog.at_level(logging.INFO):
            result = await river.generate_json("JSON", "u", response_model=KeywordVariantsPayload)

    def forbidden():
        pytest.fail("Aucun transport après la complétion réelle.")

    monkeypatch.setattr(llm_base, "get_shared_http_client", forbidden)
    health = await river.check_health(force=True)
    assert len(requests) == 1
    assert (result is not None) is (status == 200)
    assert health.status == ("last_call_ok" if status == 200 else "last_call_error")
    last = health.as_dict()["last_call"]
    assert last["http_status"] == status
    assert last["error_category"] == (None if status == 200 else "http_error")
    assert last["x_request_id"] == "req_synthetic_123"
    assert last["observed_at"].endswith("+00:00")
    record = next(r for r in caplog.records if getattr(r, "event", None) == "llm_request")
    output = json.loads(JSONFormatter().format(record))
    assert output["http_status"] == status
    if status != 200:
        assert last["provider_code"] == "invalid_api_key"
        assert last["provider_type"] == "authentication_error"
        assert output["provider_code"] == "invalid_api_key"
        assert output["provider_type"] == "authentication_error"
    assert "PRIVATE_ERROR_BODY" not in json.dumps(health.as_dict())
    assert "PRIVATE_ERROR_BODY" not in json.dumps(output)


@pytest.mark.parametrize(("error", "category"), [
    (httpx.ConnectError, "transport_error"), (httpx.ReadTimeout, "timeout"),
])
async def test_river_passive_health_keeps_network_failure_without_retry(
    river, error, category, monkeypatch,
):
    requests = []

    def respond(request):
        requests.append(request)
        raise error("PRIVATE_TRANSPORT_ERROR")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        assert await river.generate_json("JSON", "u") is None
    health = await river.check_health()
    assert health.status == "last_call_error"
    assert health.as_dict()["last_call"]["error_category"] == category
    assert health.as_dict()["last_call"]["http_status"] is None
    assert len(requests) == 1
    assert "PRIVATE_TRANSPORT_ERROR" not in json.dumps(health.as_dict())


@pytest.mark.parametrize("private", ["synthetic-river-key", "rv_PARTIAL_PRIVATE", "Bearer PRIVATE"])
async def test_river_error_identifiers_never_expose_credentials(
    river, private, monkeypatch, caplog,
):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        401, json={"error": {"code": private, "type": private, "message": private}},
        headers={"x-request-id": private},
    ))) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        await river.generate_json("JSON", "u")
    last = (await river.check_health()).as_dict()["last_call"]
    assert last["provider_code"] is None
    assert last["provider_type"] is None
    assert last["x_request_id"] is None
    serialized = json.dumps(last) + "".join(JSONFormatter().format(r) for r in caplog.records)
    assert private not in serialized


async def test_river_passive_health_is_local_and_keeps_application_liveness_separate(
    river, monkeypatch,
):
    from fastapi import FastAPI

    from app.api.routes import health

    def forbidden():
        pytest.fail("Aucun accès distant pendant /health.")

    monkeypatch.setattr(llm_base, "get_shared_http_client", forbidden)
    monkeypatch.setattr(health, "llm_client", river)
    app = FastAPI()
    app.include_router(health.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as http:
        response = await http.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["scope"] == "http_liveness"
    assert body["unchecked"] == ["database", "redis", "scheduler", "poller"]
    assert body["ai"]["status"] == "unverified"
    assert body["ai"]["configured"] is True


async def test_river_passive_health_disabled_and_unconfigured_do_not_call_transport(
    river, monkeypatch,
):
    def forbidden():
        pytest.fail("Aucun transport pour une IA désactivée ou non configurée.")

    monkeypatch.setattr(llm_base, "get_shared_http_client", forbidden)
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    assert (await river.check_health()).status == "disabled"
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    river.api_key = ""
    assert (await river.check_health()).status == "unconfigured"


async def test_river_passive_health_replaces_failure_with_new_real_success(river, monkeypatch):
    statuses = iter([403, 200])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(next(statuses), json={"choices": [{"message": {"content": "{}"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        assert await river.generate_json("JSON", "u") is None
        failed = await river.check_health()
        assert failed.status == "last_call_error"
        assert await river.generate_json("JSON", "u") == {}
        succeeded = await river.check_health()
    assert succeeded.status == "last_call_ok"
    assert succeeded.last_call["http_status"] == 200
    assert failed.last_call["http_status"] == 403
    assert len(requests) == 2
    fresh = build_llm_client("river")
    fresh.api_key = "synthetic-river-key"
    assert (await fresh.check_health()).status == "unverified"


@pytest.mark.parametrize("content", ["invalid JSON", '{"variants":[42]}'])
async def test_river_passive_health_does_not_count_rejected_content_as_success(
    river, content, monkeypatch,
):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"choices": [{"message": {"content": content}}]},
    ))) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        assert await river.generate_json("JSON", "u", response_model=KeywordVariantsPayload) is None
    assert (await river.check_health()).status == "last_call_error"


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

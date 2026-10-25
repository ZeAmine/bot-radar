import asyncio
import json
import logging

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.core.logging import JSONFormatter
from app.providers.llm import base as llm_base
from app.providers.llm.registry import build_llm_client
from app.services.ai.modules.auto_tuning import AutoTuningModule
from app.services.ai.modules.keyword_variants import KeywordVariantsModule
from app.services.ai.schemas import (
    AutoTuningPayload,
    KeywordVariantsPayload,
    NaturalAddPayload,
    NegotiationPayload,
    ProfitScoringPayload,
)
from tests.fakes_llm import FakeLLM

PAYLOADS = (
    NaturalAddPayload, KeywordVariantsPayload, AutoTuningPayload,
    NegotiationPayload, ProfitScoringPayload,
)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_optional_payload_fields_remain_compatible(payload):
    assert payload.model_validate({}) is not None


@pytest.mark.parametrize("value", [{}, {"variants": None, "exclusions": None}, {
    "variants": [], "exclusions": [],
}])
async def test_empty_keyword_answer_is_distinct_from_failure(value):
    outcome = await KeywordVariantsModule(client=FakeLLM(value)).generate_variants_and_exclusions(
        "Blacksad",
    )
    assert outcome.answered
    assert outcome.is_empty
    missing = await KeywordVariantsModule(client=FakeLLM()).generate_variants_and_exclusions(
        "Blacksad",
    )
    assert not missing.answered


@pytest.mark.parametrize("value", [{}, {"suggested_exclusions": None}, {
    "suggested_exclusions": [],
}])
async def test_empty_tuning_answer_is_distinct_from_failure(value):
    result = await AutoTuningModule(client=FakeLLM(value)).suggest_exclusions("Blacksad", ["a"])
    assert result == ([], None)
    assert await AutoTuningModule(client=FakeLLM()).suggest_exclusions("Blacksad", ["a"]) is None


@pytest.mark.parametrize(("payload", "field"), [
    (NaturalAddPayload, "include_keywords"), (NaturalAddPayload, "exclude_keywords"),
    (NaturalAddPayload, "conditions"), (KeywordVariantsPayload, "variants"),
    (KeywordVariantsPayload, "exclusions"), (AutoTuningPayload, "suggested_exclusions"),
])
@pytest.mark.parametrize("item", [{"unexpected": "private"}, 42, True, None])
def test_non_text_list_entries_are_rejected(payload, field, item):
    with pytest.raises(ValidationError):
        payload.model_validate({field: [item]})


async def test_tuning_does_not_turn_objects_into_exclusions():
    result = await AutoTuningModule(client=FakeLLM({
        "suggested_exclusions": [{"private": "data"}],
    })).suggest_exclusions("Blacksad", ["a"])
    assert result is None


async def test_keyword_rationale_is_not_logged(caplog):
    with caplog.at_level(logging.INFO):
        await KeywordVariantsModule(client=FakeLLM({
            "variants": [], "rationale": "PRIVATE_MODEL_RATIONALE",
        })).generate_variants_and_exclusions("Blacksad")
    assert "PRIVATE_MODEL_RATIONALE" not in "".join(
        JSONFormatter().format(record) for record in caplog.records
    )


def assert_strict_schema(node):
    if isinstance(node, dict):
        assert "default" not in node
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
        for value in node.values():
            assert_strict_schema(value)
    elif isinstance(node, list):
        for value in node:
            assert_strict_schema(value)


def test_openai_payload_uses_official_fields(monkeypatch):
    monkeypatch.setattr(settings, "AI_MODEL", "gpt-6-luna")
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "AI_MAX_TOKENS", 2048)
    monkeypatch.setattr(settings, "AI_REASONING_EFFORT", "none")
    client = build_llm_client("openai")
    payload = client.build_payload("JSON système", "utilisateur", 0.2, None,
                                   response_model=KeywordVariantsPayload)
    assert payload["model"] == "gpt-6-luna"
    assert client.chat_endpoint() == "https://api.openai.com/v1/chat/completions"
    assert payload["reasoning_effort"] == "none"
    assert payload["max_completion_tokens"] == 2048
    assert "thinking" not in payload
    assert "max_tokens" not in payload
    assert payload["response_format"]["type"] == "json_schema"
    contract = payload["response_format"]["json_schema"]
    assert contract["strict"] is True
    assert_strict_schema(contract["schema"])


@pytest.mark.parametrize("payload_model", PAYLOADS)
def test_each_output_schema_is_strict_and_typed(payload_model):
    client = build_llm_client("openai")
    payload = client.build_payload("JSON", "u", 0.2, None, response_model=payload_model)
    schema = payload["response_format"]["json_schema"]["schema"]
    assert_strict_schema(schema)
    for field in ("variants", "exclusions", "include_keywords", "exclude_keywords",
                  "conditions", "suggested_exclusions"):
        if field in schema["properties"]:
            variants = schema["properties"][field]["anyOf"]
            array = next(branch for branch in variants if branch["type"] == "array")
            assert array["items"] == {"type": "string"}


@pytest.mark.parametrize(("provider", "thinking"), [
    ("openai", False), ("openai-compatible", False), ("deepseek", True),
])
def test_dialect_is_selected_only_by_configuration(provider, thinking):
    client = build_llm_client(provider)
    body = client.build_payload("JSON", "u", 0.2, None)
    assert ("thinking" in body) is thinking
    if thinking:
        assert body["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in body


@pytest.mark.parametrize(("content", "expected_status"), [
    ('{"variants":[],"exclusions":null}', "ok"),
    ("not-json", "invalid_json"), ("[]", "invalid_json"),
    ('{"variants":[42]}', "validation_error"),
    (" ", "empty_content"),
])
async def test_final_diagnostics_include_validation_and_visible_usage(
    content, expected_status, monkeypatch, caplog,
):
    client = build_llm_client("openai")
    client.api_key = "synthetic-private-key"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                      "prompt_tokens_details": {"cached_tokens": 80},
                      "completion_tokens_details": {"reasoning_tokens": 0}},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        with caplog.at_level(logging.INFO):
            result = await client.generate_json("JSON PRIVATE_PROMPT", "PRIVATE_INPUT",
                                                response_model=KeywordVariantsPayload,
                                                ai_module="keyword_variants")
    assert (result is not None) is (expected_status == "ok")
    assert len(requests) == 1
    records = [record for record in caplog.records
               if getattr(record, "event", None) == "llm_request"]
    assert len(records) == 1
    output = json.loads(JSONFormatter().format(records[0]))
    assert output["status"] == expected_status
    assert output["ai_module"] == "keyword_variants"
    assert output["provider"] == "openai"
    assert output["model"] == client.model
    assert output["attempt"] == 1
    assert output["usage"] == {"input": 120, "output": 30, "cached": 80, "reasoning": 0}
    assert output["duration_ms"] >= 0
    logs = "".join(JSONFormatter().format(record) for record in caplog.records)
    for private in ("synthetic-private-key", "PRIVATE_PROMPT", "PRIVATE_INPUT", content):
        if private.strip():
            assert private not in logs


@pytest.mark.parametrize("status", [400, 401, 403, 408, 429, 500, 503])
async def test_http_failures_do_not_retry(status, monkeypatch, caplog):
    client = build_llm_client("openai")
    client.api_key = "synthetic"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, text="PRIVATE_HTTP_BODY")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        assert await client.generate_json("JSON", "u", response_model=NegotiationPayload) is None
    assert len(requests) == 1
    record = next(r for r in caplog.records if getattr(r, "event", None) == "llm_request")
    assert record.status == "http_error"
    assert record.http_status == status
    assert record.usage == {"input": None, "output": None, "cached": None, "reasoning": None}
    assert "PRIVATE_HTTP_BODY" not in JSONFormatter().format(record)


@pytest.mark.parametrize("refusal", [True, False])
async def test_refused_or_truncated_valid_json_is_rejected(refusal, monkeypatch, caplog):
    client = build_llm_client("openai")
    client.api_key = "synthetic"
    message = {"content": '{"variants":[]}'}
    if refusal:
        message["refusal"] = "PRIVATE_REFUSAL"
    envelope = {"choices": [{"message": message, "finish_reason": "stop" if refusal else "length"}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=envelope),
    )) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        result = await client.generate_json("JSON", "u", response_model=KeywordVariantsPayload)
        assert result is None
    record = next(r for r in caplog.records if getattr(r, "event", None) == "llm_request")
    assert record.status == ("refused" if refusal else "interrupted")
    assert "PRIVATE_REFUSAL" not in JSONFormatter().format(record)


async def test_overlapping_calls_keep_their_diagnostics(monkeypatch, caplog):
    client = build_llm_client("openai")
    client.api_key = "synthetic"
    both_entered = asyncio.Event()
    count = 0

    async def respond(request):
        nonlocal count
        count += 1
        if count == 2:
            both_entered.set()
        await both_entered.wait()
        content = json.loads(request.content)["messages"][1]["content"]
        return httpx.Response(429 if content == "first" else 200,
                              json={"choices": [{"message": {"content": "{}"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        with caplog.at_level(logging.INFO):
            await asyncio.wait_for(asyncio.gather(
                client.generate_json("JSON", "first", ai_module="one"),
                client.generate_json("JSON", "second", ai_module="two"),
            ), timeout=1)
    rows = {r.ai_module: r for r in caplog.records if getattr(r, "event", None) == "llm_request"}
    assert rows["one"].status == "http_error"
    assert rows["two"].status == "ok"
    assert rows["one"].request_id != rows["two"].request_id


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_constants_are_not_valid_json(constant):
    assert llm_base.clean_and_parse_json('{"value":' + constant + '}') is None


async def test_redirect_cannot_trigger_another_post(monkeypatch):
    client = build_llm_client("openai")
    client.api_key = "synthetic"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://example.invalid/other"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True,
    ) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        assert await client.generate_json("JSON", "u") is None
    assert len(requests) == 1


@pytest.mark.parametrize("provider", [
    "openai", "openai-compatible", "deepseek", "anthropic", "river",
])
@pytest.mark.parametrize(("payload_model", "data"), [
    (NaturalAddPayload, {"name": "Blacksad", "is_series": True, "max_price_cents": 1200}),
    (KeywordVariantsPayload, {}),
    (KeywordVariantsPayload, {"variants": None, "exclusions": []}),
    (AutoTuningPayload, {"suggested_exclusions": ["affiche"], "explanation": None}),
    (NegotiationPayload, {"suggested_offer_cents": 900, "message": " Bonjour ! "}),
    (ProfitScoringPayload, {"estimated_resale_cents": 0, "net_profit_cents": -1000,
                            "pepite_score": 1}),
    (ProfitScoringPayload, {"estimated_resale_cents": None, "net_profit_cents": None,
                            "pepite_score": None, "verdict": "Modèle non identifié."}),
])
async def test_identical_response_preserves_existing_business_input(
    provider, payload_model, data, monkeypatch,
):
    client = build_llm_client(provider)
    client.api_key = "synthetic"
    text = json.dumps(data)
    envelope = ({"content": [{"type": "text", "text": text}]} if provider == "anthropic"
                else {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=envelope),
    )) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        actual = await client.generate_json("JSON", "u", response_model=payload_model)
    expected = await FakeLLM(data).generate_json("JSON", "u")
    assert actual == expected == data
    assert payload_model.model_validate(actual) == payload_model.model_validate(expected)


async def test_all_five_uses_supply_their_schema_and_module():
    from unittest.mock import AsyncMock

    from app.db.models.listing import Listing
    from app.services.ai.modules.natural_add import NaturalAddModule
    from app.services.ai.modules.negotiation import NegotiationModule
    from app.services.ai.pipeline import AIPostAlertPipeline
    from app.services.ai.schemas import PostAlertPayload

    client = FakeLLM()
    await NaturalAddModule(client=client).parse_prompt("Blacksad")
    await KeywordVariantsModule(client=client).generate_variants_and_exclusions("Blacksad")
    await AutoTuningModule(client=client).suggest_exclusions("Blacksad", ["Affiche Blacksad"])
    listing = Listing(title="Console", item_price_cents=1000, category_slug="gaming")
    await NegotiationModule(client=client).generate_offer(listing)
    await AIPostAlertPipeline(client=client).enrich_alert(AsyncMock(), 1, 2, listing, None)
    assert [(call["ai_module"], call["response_model"]) for call in client.calls] == [
        ("natural_add", NaturalAddPayload), ("keyword_variants", KeywordVariantsPayload),
        ("auto_tuning", AutoTuningPayload), ("negotiation", NegotiationPayload),
        ("post_alert", PostAlertPayload),
    ]


def test_nested_wire_schema_preserves_local_defaults():
    from app.services.ai.schemas import PostAlertPayload

    original = PostAlertPayload.model_json_schema()
    client = build_llm_client("openai")
    body = client.build_payload("JSON", "u", 0.2, None, response_model=PostAlertPayload)
    schema = body["response_format"]["json_schema"]["schema"]
    assert_strict_schema(schema)
    assert schema["$defs"]["ProfitScoringPayload"]["additionalProperties"] is False
    assert PostAlertPayload.model_json_schema() == original
    assert PostAlertPayload.model_validate({}).profit_scoring is None


def test_configuration_defaults_match_the_example():
    from pathlib import Path

    from app.core.config import Settings

    example = Path(".env.example").read_text()
    for field in ("AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_MAX_TOKENS",
                  "AI_REASONING_EFFORT", "AI_TIMEOUT_SECONDS"):
        default = Settings.model_fields[field].default
        assert f"{field}={default}\n" in example


@pytest.mark.parametrize(("field", "value"), [
    ("AI_MAX_TOKENS", 0), ("AI_TIMEOUT_SECONDS", 0),
    ("AI_TIMEOUT_SECONDS", float("inf")), ("AI_REASONING_EFFORT", "invalid"),
])
def test_invalid_configuration_is_rejected_before_requests(field, value):
    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, _secrets_dir=None, **{field: value})



def test_pipeline_does_not_format_missing_price_without_applicable_modules():
    from app.db.models.listing import Listing
    from app.services.ai.pipeline import AIPostAlertPipeline

    listing = Listing(title="Blacksad", category_slug="bd_comics", item_price_cents=None)
    prompt, owners = AIPostAlertPipeline(client=FakeLLM()).build_request(listing, None)
    assert (prompt, owners) == ("", {})

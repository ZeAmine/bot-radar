import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services.ai.schemas import NaturalAddPayload
from tests.fakes_llm import FakeLLM

spec = importlib.util.spec_from_file_location(
    "probe_llm_latency", Path(__file__).resolve().parents[2] / "scripts/probe_llm_latency.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize("answer", [None, {"name": "PRIVATE_MODEL_CONTENT"}])
async def test_latency_probe_uses_shared_interface_without_printing_content(answer, capsys):
    client = FakeLLM(answer)
    ok, elapsed = await probe.measure(client, "natural_add", "JSON", "synthetic", NaturalAddPayload)
    assert ok is (answer is not None)
    assert elapsed >= 0
    assert len(client.calls) == 1
    assert client.calls[0]["response_model"] is NaturalAddPayload
    assert client.calls[0]["ai_module"] == "natural_add"
    assert "PRIVATE_MODEL_CONTENT" not in capsys.readouterr().out


async def test_latency_probe_closes_http_on_failure(monkeypatch):
    close = AsyncMock()
    monkeypatch.setattr(probe, "close_shared_http_client", close)
    monkeypatch.setattr(probe, "_run", AsyncMock(side_effect=RuntimeError("synthetic")))
    with pytest.raises(RuntimeError, match="synthetic"):
        await probe.run(None)
    close.assert_awaited_once()

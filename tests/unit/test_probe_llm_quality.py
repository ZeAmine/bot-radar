"""Le banc d'essai des moteurs IA, testé sur ses verdicts — la partie qui décide.

Le script sort sur Internet : ce sont ses verdicts qui sont purs, et c'est eux qui
doivent être faux s'ils sont faux. Aucun test ici n'appelle un moteur.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import probe_llm_quality as probe  # noqa: E402

from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome  # noqa: E402
from app.services.ai.schemas import NaturalAddResult  # noqa: E402

SERIES = probe.NatureCase(label="série", prompt="…", expect_series=True)
ONE_SHOT = probe.NatureCase(label="one-shot", prompt="…", expect_series=False)
KEYWORDS = probe.KeywordCase(
    label="titre de série nu", title="Absolute Martian Manhunter", category="bd_comics"
)


def _result(**overrides: object) -> NaturalAddResult:
    base: dict[str, object] = {
        "name": "Absolute Martian Manhunter",
        "category_slug": "bd_comics",
    }
    return NaturalAddResult(**{**base, **overrides})


def test_nature_verdict_accepts_a_series_without_isbn():
    assert probe.judge_nature(SERIES, _result(is_series=True)) == []


def test_nature_verdict_accepts_a_one_shot():
    assert probe.judge_nature(ONE_SHOT, _result(is_series=False)) == []


def test_nature_verdict_flags_an_isbn_on_a_series():
    failures = probe.judge_nature(SERIES, _result(is_series=True, isbn13="9781799505204"))

    assert any("ISBN" in failure for failure in failures)


def test_nature_verdict_flags_the_wrong_nature():
    failures = probe.judge_nature(ONE_SHOT, _result(is_series=True))

    assert any("nature" in failure for failure in failures)


def test_nature_verdict_flags_a_price_left_in_the_name():
    failures = probe.judge_nature(SERIES, _result(name="One Piece 10€", is_series=True))

    assert any("prix" in failure for failure in failures)


def test_keyword_verdict_accepts_an_empty_delivered_list():
    """« Aucune exclusion pertinente » est une réponse conforme, pas un échec."""
    assert probe.judge_keywords(KEYWORDS, KeywordVariantsOutcome(answered=True)) == []


def test_keyword_verdict_flags_silence():
    failures = probe.judge_keywords(KEYWORDS, KeywordVariantsOutcome.unanswered())

    assert any("aucune réponse" in failure for failure in failures)


def test_keyword_verdict_flags_a_filler_exclusion():
    """La famille réelle : le titre plus un numéro, que le nettoyeur doit avoir coupée."""
    outcome = KeywordVariantsOutcome(
        exclusions=["absolute martian manhunter 2"], answered=True
    )

    assert probe.judge_keywords(KEYWORDS, outcome)


def test_keyword_verdict_flags_a_list_beyond_the_ceiling():
    outcome = KeywordVariantsOutcome(
        exclusions=[f"confusion {index}" for index in range(4)], answered=True
    )

    assert any("au plus" in failure for failure in probe.judge_keywords(KEYWORDS, outcome))


def test_reference_cases_cover_both_outcomes():
    """Un banc d'essai qui n'aurait qu'un type de cas ne comparerait rien."""
    assert any(case.expect_series for case in probe.NATURE_CASES)
    assert any(not case.expect_series for case in probe.NATURE_CASES)
    assert probe.KEYWORD_CASES


@pytest.mark.parametrize("kind", ["profit_scoring", "negotiation", "auto_tuning"])
def test_advisory_cases_cover_remaining_uses(kind):
    assert any(case.kind == kind for case in probe.ADVISORY_CASES)


def test_advisory_verdict_distinguishes_empty_tuning_from_failure():
    case = next(case for case in probe.ADVISORY_CASES if case.kind == "auto_tuning")
    assert probe.judge_advisory(case, ([], None)) == []
    assert probe.judge_advisory(case, None)


def test_advisory_verdict_rejects_incoherent_profit():
    from app.services.ai.schemas import ProfitScoringResult

    case = probe.AdvisoryCase("article identifié", "profit_scoring", "Nintendo Switch OLED")
    result = ProfitScoringResult(
        purchase_cost_cents=1000, estimated_resale_cents=1500,
        net_profit_cents=9999, pepite_score=8,
    )
    assert probe.judge_advisory(case, result)
    result.net_profit_cents = 500
    assert probe.judge_advisory(case, result) == []


async def test_quality_probe_closes_http_on_failure(monkeypatch):
    from unittest.mock import AsyncMock

    close = AsyncMock()
    run = AsyncMock(side_effect=RuntimeError("synthetic"))
    monkeypatch.setattr(probe, "close_shared_http_client", close)
    monkeypatch.setattr(probe, "_run", run)
    with pytest.raises(RuntimeError, match="synthetic"):
        await probe.run(probe.build_parser().parse_args([]))
    close.assert_awaited_once()


@pytest.mark.parametrize("http_status", [429, 200])
async def test_advisory_probe_does_not_count_fallback_as_model_success(monkeypatch, http_status):
    import httpx

    from app.providers.llm import base as llm_base
    from app.providers.llm.registry import build_llm_client

    case = next(case for case in probe.ADVISORY_CASES if case.kind == "negotiation")
    client = build_llm_client()
    client.api_key = "synthetic"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(http_status, json={
            "choices": [{"message": {"content": "{}"}}],
        }),
    )) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        outcome = await probe.run_advisory_case(case, client, 1)
    assert not outcome.ok

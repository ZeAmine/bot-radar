"""Le banc d'essai des moteurs IA, testé sur ses verdicts — la partie qui décide.

Le script sort sur Internet : ce sont ses verdicts qui sont purs, et c'est eux qui
doivent être faux s'ils sont faux. Aucun test ici n'appelle un moteur.
"""

from __future__ import annotations

import sys
from pathlib import Path

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

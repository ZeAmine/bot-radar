import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.db.models.listing import Listing
from app.domain.enums import BookSearchStatus
from app.services.ai.modules.auto_tuning import AutoTuningModule
from app.services.ai.modules.keyword_variants import OPTIMIZE_SYSTEM_PROMPT, KeywordVariantsModule
from app.services.ai.modules.natural_add import NaturalAddModule
from app.services.ai.modules.negotiation import NegotiationModule
from app.services.ai.modules.profit_scoring import ProfitScoringModule
from app.services.ai.pipeline import AIPostAlertPipeline
from app.services.ai.schemas import (
    BookContext,
    KeywordVariantsPayload,
    ProfitScoringPayload,
    ProfitScoringResult,
)
from app.services.book_metadata_service import BookMetadata, BookResolution
from app.use_cases.book_search import BookSearchResult, BookWorkChoice, result_from_resolution
from tests.fakes_llm import FakeLLM

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import probe_llm_quality as probe  # noqa: E402

OBSERVED = json.loads(
    (Path(__file__).parents[1] / "fixtures/ai_quality_observed.json").read_text()
)["cases"]


def observed(index):
    return OBSERVED[index - 1]


def listing_for(index):
    data = observed(index)["input"]
    return Listing(
        title=data["title"],
        item_price_cents=data["price_cents"],
        estimated_total_cents=data["price_cents"],
        category_slug=data["category"],
        condition=data["condition"],
        description=data["description"],
        source_name="leboncoin",
    )


def test_variant_prompt_example_matches_permission_to_propose_no_variants():
    instruction, example = OPTIMIZE_SYSTEM_PROMPT.rsplit(
        "Retourne STRICTEMENT un objet JSON :\n", 1
    )
    assert "Zéro variante est une réponse valide" in instruction
    assert "réponds variants=[]" in instruction
    assert "Ne cherche jamais à remplir un quota" in instruction
    payload = KeywordVariantsPayload.model_validate_json(example)
    assert payload.variants == []
    assert payload.exclusions == ["exclusion1"]


async def test_observed_unidentified_title_does_not_become_a_confirmed_one_shot(monkeypatch):
    search = AsyncMock(return_value=BookSearchResult(status=BookSearchStatus.UNAVAILABLE))
    monkeypatch.setattr(
        "app.services.ai.modules.natural_add.BookSearchUseCase.search_by_title", search
    )
    case = observed(1)
    client = FakeLLM(case["output"])
    result = await NaturalAddModule(client=client).parse_prompt(case["input"]["prompt"])
    assert result is not None
    assert not result.is_series
    assert result.book_unidentified
    assert result.isbn13 is None
    assert result.bibliographic_identity["source"] is None
    assert result.max_price_cents == 1000
    assert len(client.calls) == 1
    assert probe.judge_nature(probe.NATURE_CASES[0], result)


async def test_numbered_catalogue_evidence_can_resolve_the_observed_title_as_a_series(monkeypatch):
    case = observed(1)
    title = case["output"]["name"]
    resolution = BookResolution(best=BookMetadata(title=title), notice_titles=(title + " Vol. 2",))
    found = result_from_resolution(resolution, title)
    monkeypatch.setattr(
        "app.services.ai.modules.natural_add.BookSearchUseCase.search_by_title",
        AsyncMock(return_value=found),
    )
    result = await NaturalAddModule(client=FakeLLM(case["output"])).parse_prompt(
        case["input"]["prompt"]
    )
    assert result is not None
    assert result.is_series
    assert result.isbn13 is None


async def test_observed_explicit_volume_keeps_its_number_without_catalogue_confirmation(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.ai.modules.natural_add.BookSearchUseCase.search_by_title",
        AsyncMock(return_value=BookSearchResult(status=BookSearchStatus.NOT_FOUND)),
    )
    case = observed(2)
    result = await NaturalAddModule(client=FakeLLM(case["output"])).parse_prompt(
        case["input"]["prompt"]
    )
    assert result is not None
    assert not result.is_series
    assert result.requested_volume == 12
    assert result.max_price_cents == 800
    assert result.isbn13 is None
    assert result.book_unidentified


async def test_observed_single_work_can_be_confirmed_without_becoming_a_series(monkeypatch):
    case = observed(3)
    found = result_from_resolution(
        BookResolution(best=BookMetadata(title=case["output"]["name"], isbn13="9782205088168")),
        case["output"]["name"],
    )
    monkeypatch.setattr(
        "app.services.ai.modules.natural_add.BookSearchUseCase.search_by_title",
        AsyncMock(return_value=found),
    )
    result = await NaturalAddModule(client=FakeLLM(case["output"])).parse_prompt(
        case["input"]["prompt"]
    )
    assert result is not None
    assert not result.is_series
    assert not result.book_unidentified
    assert result.isbn13 == "9782205088168"


async def test_ambiguous_work_keeps_choices_without_an_arbitrary_isbn(monkeypatch):
    case = observed(3)
    found = BookSearchResult(
        status=BookSearchStatus.AMBIGUOUS,
        works=(
            BookWorkChoice(title=case["output"]["name"], author="Auteur A", isbn13="9782205088168"),
            BookWorkChoice(title=case["output"]["name"], author="Auteur B", isbn13="9782265001527"),
        ),
    )
    monkeypatch.setattr(
        "app.services.ai.modules.natural_add.BookSearchUseCase.search_by_title",
        AsyncMock(return_value=found),
    )
    result = await NaturalAddModule(client=FakeLLM(case["output"])).parse_prompt(
        case["input"]["prompt"]
    )
    assert result is not None
    assert result.isbn13 is None
    assert not result.is_series
    assert len(result.work_choices) == 2
    assert result.book_context is None


@pytest.mark.parametrize("index", [4, 5])
async def test_observed_repetitive_variants_are_not_hidden_by_cleaning(index):
    case = observed(index)
    source = case["input"]
    outcome = await KeywordVariantsModule(
        client=FakeLLM(case["output"])
    ).generate_variants_and_exclusions(
        source["title"], source["category"], BookContext(**source["context"])
    )
    assert outcome.answered
    assert outcome.proposed_variants == tuple(case["output"]["variants"])
    assert outcome.variant_assessment.removals
    assert probe.judge_keywords(probe.KEYWORD_CASES[index - 4], outcome)
    assert probe.keyword_quality(outcome) == "repetitive"
    assert outcome.variants == ([] if index == 4 else ["black sad"])


async def test_empty_variant_proposal_is_valid_and_distinct_from_full_filtering():
    module = KeywordVariantsModule(client=FakeLLM({"variants": [], "exclusions": []}))
    empty = await module.generate_variants_and_exclusions("Blacksad")
    assert empty.answered
    assert empty.is_empty
    assert probe.keyword_quality(empty) == "empty_proposal"
    assert probe.judge_keywords(probe.KEYWORD_CASES[1], empty) == []
    module.client = FakeLLM({"variants": ["Blacksad"], "exclusions": []})
    filtered = await module.generate_variants_and_exclusions("Blacksad")
    assert filtered.answered
    assert filtered.is_empty
    assert probe.keyword_quality(filtered) == "fully_filtered"
    assert probe.judge_keywords(probe.KEYWORD_CASES[1], filtered)


async def test_distinct_useful_variants_survive_normalization():
    module = KeywordVariantsModule(
        client=FakeLLM({"variants": ["Black Sad", "blacksat"], "exclusions": []})
    )
    outcome = await module.generate_variants_and_exclusions("Blacksad")
    assert outcome.variants == ["black sad", "blacksat"]
    assert probe.keyword_quality(outcome) == "useful_variants"
    assert probe.judge_keywords(probe.KEYWORD_CASES[1], outcome) == []


def test_scoring_abstains_with_existing_nullable_fields_and_reason():
    raw = {
        "estimated_resale_cents": None,
        "net_profit_cents": None,
        "pepite_score": None,
        "verdict": "Modèle non identifié : estimation impossible.",
    }
    assert ProfitScoringPayload.model_validate(raw)
    module = ProfitScoringModule()
    result = module.parse_chunk_result(raw, listing_for(8), None)
    assert result is not None
    assert result.is_abstention
    assert result.purchase_cost_cents == 10000
    text = module.format_telegram_html(result)
    assert "Modèle non identifié" in text
    assert "Indice pépite" not in text
    assert "Revente estimée" not in text
    assert "Marge" not in text
    assert probe.judge_advisory(probe.ADVISORY_CASES[2], result) == []


@pytest.mark.parametrize("index", [8, 9])
def test_unidentified_scoring_cases_require_abstention_instead_of_observed_prices(index):
    case = observed(index)
    result = ProfitScoringModule().parse_chunk_result(
        case["output"]["profit_scoring"], listing_for(index), None
    )
    assert result is not None
    assert probe.judge_advisory(probe.ADVISORY_CASES[index - 6], result)


@pytest.mark.parametrize(
    "raw",
    [
        {"pepite_score": 8.5, "verdict": "Conseil sans prix"},
        {"estimated_resale_cents": 12000, "verdict": "Pas de score"},
        {},
    ],
)
def test_incomplete_scoring_never_displays_a_numeric_opportunity(raw):
    module = ProfitScoringModule()
    result = module.parse_chunk_result(raw, listing_for(8), None)
    assert module.format_telegram_html(result) is None


def test_incoherent_margin_is_rejected_instead_of_displayed():
    module = ProfitScoringModule()
    result = module.parse_chunk_result(
        {"estimated_resale_cents": 12000, "net_profit_cents": 9999, "pepite_score": 8},
        listing_for(8),
        None,
    )
    assert result is None


def test_missing_purchase_cost_stays_unknown_and_cannot_produce_a_margin():
    listing = Listing(title="Appareil inconnu", source_name="leboncoin", category_slug="tech")
    module = ProfitScoringModule()
    raw = {"estimated_resale_cents": 12000, "net_profit_cents": 12000, "pepite_score": 8}
    result = module.parse_chunk_result(raw, listing, None)
    assert result is None or (
        result.purchase_cost_cents is None and result.net_profit_cents is None
    )
    assert module.format_telegram_html(result) is None


def test_known_item_price_is_purchase_cost_when_no_buyer_fees_apply():
    listing = listing_for(8)
    listing.estimated_total_cents = None
    module = ProfitScoringModule()
    chunk = module.build_prompt_chunk(listing, None)
    assert chunk is not None
    assert "100.00 €" in chunk[1]
    result = module.parse_chunk_result(observed(8)["output"]["profit_scoring"], listing, None)
    assert result is not None
    assert result.purchase_cost_cents == 10000
    assert result.net_profit_cents == 2000


def test_margin_is_labeled_as_gross_before_resale_fees():
    module = ProfitScoringModule()
    result = ProfitScoringResult(
        purchase_cost_cents=10000,
        estimated_resale_cents=12000,
        net_profit_cents=2000,
        pepite_score=8,
    )
    text = module.format_telegram_html(result)
    assert "Marge brute estimée" in text
    assert "hors frais de revente" in text
    assert "Marge nette" not in text


async def test_abstention_is_a_background_reply_without_an_invented_score(monkeypatch):
    monkeypatch.setattr(settings, "AI_FEATURE_PROFIT_SCORING", True)
    raw = {
        "profit_scoring": {
            "estimated_resale_cents": None,
            "net_profit_cents": None,
            "pepite_score": None,
            "verdict": "Identification insuffisante.",
        }
    }
    bot = AsyncMock()
    await AIPostAlertPipeline(client=FakeLLM(raw)).enrich_alert(bot, 7, 123, listing_for(8), None)
    bot.send_message.assert_awaited_once()
    call = bot.send_message.call_args.kwargs
    assert call["reply_to_message_id"] == 123
    assert "Identification insuffisante" in call["text"]
    assert "Indice pépite" not in call["text"]


@pytest.mark.parametrize("index", [6, 7])
async def test_observed_negotiation_remains_exploitable(index):
    case = observed(index)
    client = FakeLLM(case["output"])
    result = await NegotiationModule(client=client).generate_offer(listing_for(index))
    assert result is not None
    assert result.suggested_offer_cents == case["output"]["suggested_offer_cents"]
    assert result.message == case["output"]["message"]
    assert len(client.calls) == 1


async def test_observed_auto_tuning_remains_a_proposal_only():
    case = observed(10)
    client = FakeLLM(case["output"])
    result = await AutoTuningModule(client=client).suggest_exclusions(
        case["input"]["title"], case["input"]["false_positive_titles"]
    )
    assert result == (case["output"]["suggested_exclusions"], case["output"]["explanation"])
    assert len(client.calls) == 1


def test_focused_revalidation_selects_exactly_seven_calls():
    args = probe.build_parser().parse_args(
        [
            "--passes",
            "1",
            "--module",
            "natural_add",
            "--module",
            "keyword_variants",
            "--module",
            "profit_scoring",
        ]
    )
    assert probe.expected_call_count(args) == 7
    assert probe.expected_call_count(probe.build_parser().parse_args(["--passes", "1"])) == 10


async def test_normalized_duplicates_are_reported_even_when_one_variant_survives():
    outcome = await KeywordVariantsModule(
        client=FakeLLM(
            {
                "variants": ["Black Sad", "BLACK SÁD!"],
                "exclusions": [],
            }
        )
    ).generate_variants_and_exclusions("Blacksad")
    assert outcome.variants == ["black sad"]
    assert probe.keyword_quality(outcome) == "repetitive"
    assert probe.judge_keywords(probe.KEYWORD_CASES[1], outcome)


async def test_partial_filtering_is_visible_without_rejecting_every_filtered_answer():
    outcome = await KeywordVariantsModule(
        client=FakeLLM(
            {
                "variants": ["Black Sad", "Blacksad"],
                "exclusions": ["figurine"],
            }
        )
    ).generate_variants_and_exclusions("Blacksad")
    assert outcome.variants == ["black sad"]
    assert outcome.exclusions == ["figurine"]
    assert probe.keyword_quality(outcome) == "partly_filtered"
    assert probe.judge_keywords(probe.KEYWORD_CASES[1], outcome) == []


async def test_off_topic_proposals_are_not_hidden_by_a_useful_remaining_variant():
    outcome = await KeywordVariantsModule(
        client=FakeLLM(
            {
                "variants": ["Black Sad", "accessoire inconnu"],
                "exclusions": [],
            }
        )
    ).generate_variants_and_exclusions("Blacksad")
    assert outcome.variants == ["black sad"]
    assert probe.judge_keywords(probe.KEYWORD_CASES[1], outcome)


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_empty_score_without_a_reason_is_not_a_successful_abstention(reason):
    result = ProfitScoringModule().parse_chunk_result({"verdict": reason}, listing_for(8), None)
    assert result is not None
    assert not result.is_abstention
    assert probe.judge_advisory(probe.ADVISORY_CASES[2], result)
    assert ProfitScoringModule().format_telegram_html(result) is None


def test_abstention_reason_is_escaped_and_does_not_enter_logs(caplog):
    import logging

    from app.core.logging import JSONFormatter

    reason = "<script>private-reason</script>"
    module = ProfitScoringModule()
    with caplog.at_level(logging.INFO):
        result = module.parse_chunk_result({"verdict": reason}, listing_for(8), None)
    assert result is not None
    assert result.is_abstention
    assert "&lt;script&gt;" in module.format_telegram_html(result)
    serialized = "".join(JSONFormatter().format(record) for record in caplog.records)
    assert "private-reason" not in serialized
    assert any(getattr(record, "status", None) == "abstained" for record in caplog.records)


def test_zero_margin_is_known_and_missing_margin_can_be_computed_locally():
    result = ProfitScoringModule().parse_chunk_result(
        {
            "estimated_resale_cents": 10000,
            "pepite_score": 8,
        },
        listing_for(8),
        None,
    )
    assert result is not None
    assert result.net_profit_cents == 0
    assert "0,00 €" in ProfitScoringModule().format_telegram_html(result)


async def test_focused_probe_replays_observed_answers_once_and_keeps_quality_failures(
    monkeypatch,
    capsys,
):
    import httpx

    from app.providers.llm import base as llm_base
    from app.providers.llm.registry import build_llm_client

    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_API_KEY", SecretStr("synthetic-key"))
    client = build_llm_client()
    client.api_key = "synthetic-key"
    monkeypatch.setattr(probe, "build_llm_client", lambda _provider: client)
    monkeypatch.setattr(
        "app.services.ai.modules.natural_add.BookSearchUseCase.search_by_title",
        AsyncMock(return_value=BookSearchResult(status=BookSearchStatus.UNAVAILABLE)),
    )
    indexes = [1, 2, 3, 4, 5, 8, 9]
    requests = []

    def respond(request):
        case = observed(indexes[len(requests)])
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(case["output"])},
                    }
                ]
            },
        )

    args = probe.build_parser().parse_args(
        [
            "--passes",
            "1",
            "--module",
            "natural_add",
            "--module",
            "keyword_variants",
            "--module",
            "profit_scoring",
        ]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(llm_base, "get_shared_http_client", lambda: http)
        result = await probe.run(args)
    assert result == 1
    assert len(requests) == 7
    assert "propositions répétitives" in capsys.readouterr().out

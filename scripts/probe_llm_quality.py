#!/usr/bin/env python3
"""Confronte un moteur IA aux cas réels du dépôt, et rend un verdict mécanique.

« Ce moteur comprend mieux le français » n'est pas une preuve. Ce script rejoue les
cas qui ont réellement échoué sur les **vrais** modules — `natural_add.parse_prompt` et
`keyword_variants.generate_variants_and_exclusions` — et compare des faits, pas des
impressions. Il ne réécrit aucun client : il construit l'adaptateur par
`build_llm_client`, donc il mesure le chemin de production, pas un chemin parallèle.

Ce qui est mesuré, par cas :

1. la **nature de l'œuvre** que le moteur déclare (`is_series`) — son second filet,
   celui qui rattrape une autorité muette ;
2. le **JSON strict** : une réponse qui ne valide pas le schéma du module compte comme
   une absence de réponse, pas comme un avis ;
3. les **mots-clés livrés** : un ISBN sur une série, un prix dans le nom, plus de trois
   exclusions, une exclusion qui n'est que le titre plus un numéro ;
4. la **latence** de chaque appel — un moteur plus juste mais deux fois plus lent paie
   sa justesse sur le chemin `/add`.

La clé vient de l'environnement (`AI_API_KEY`), comme pour l'application : elle n'est
jamais un argument de ligne de commande, et jamais affichée.

Usage :
    python scripts/probe_llm_quality.py                     # moteur configuré
    AI_API_KEY=… python scripts/probe_llm_quality.py \\
        --provider openai-compatible --base-url https://… --model …
    python scripts/probe_llm_quality.py --passes 5

Codes de sortie : 0 tous les cas passent, 1 au moins un cas échoue, 2 configuration
incomplète (rien n'a été mesuré).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.core.http_client import close_shared_http_client  # noqa: E402
from app.core.logging import redact_text  # noqa: E402
from app.db.models.listing import Listing  # noqa: E402
from app.domain.rules.keyword_hygiene import Reason, names_only_a_volume  # noqa: E402
from app.providers.llm.base import LLMClient  # noqa: E402
from app.providers.llm.registry import build_llm_client  # noqa: E402
from app.services.ai.modules.auto_tuning import AutoTuningModule  # noqa: E402
from app.services.ai.modules.keyword_variants import (  # noqa: E402
    KeywordVariantsOutcome,
    keyword_variants_module,
)
from app.services.ai.modules.natural_add import natural_add_module  # noqa: E402
from app.services.ai.modules.negotiation import NegotiationModule  # noqa: E402
from app.services.ai.modules.profit_scoring import ProfitScoringModule  # noqa: E402
from app.services.ai.pipeline import COMBINED_SYSTEM_PROMPT, AIPostAlertPipeline  # noqa: E402
from app.services.ai.schemas import (  # noqa: E402
    BookContext,
    NaturalAddResult,
    NegotiationResult,
    PostAlertPayload,
    ProfitScoringResult,
)
from app.utils.text import normalize_text  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)

# Le plafond que le prompt demande au moteur : au-delà, il remplit plus qu'il ne tranche.
MAX_DELIVERED_EXCLUSIONS = 3

# Le signe qui ne doit jamais se retrouver dans le nom d'une alerte.
PRICE_MARK = "€"


class _RequestCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.requests: list[dict[str, object]] = []
        self.rejected = False
        self.loggers = [logging.getLogger(name) for name in (
            "app.providers.llm.base", "app.services.ai",
        )]
        self.previous_levels = [logger.level for logger in self.loggers]

    def emit(self, record):
        if getattr(record, "event", None) == "ai_validation":
            self.rejected = True
        if getattr(record, "event", None) == "llm_request":
            self.requests.append({
                "status": record.status, "usage": record.usage,
                "http_status": getattr(record, "http_status", None),
                "duration_ms": record.duration_ms,
            })

    def __enter__(self):
        for logger in self.loggers:
            logger.addHandler(self)
            logger.setLevel(logging.INFO)
        return self

    def __exit__(self, _kind, _value, _traceback):
        for logger, level in zip(self.loggers, self.previous_levels, strict=True):
            logger.removeHandler(self)
            logger.setLevel(level)

    def report(self, elapsed, result):
        print(f"    durée complète : {elapsed:.2f}s ; appels : "
              + json.dumps(self.requests, ensure_ascii=False))
        ok = (bool(self.requests) and not self.rejected
              and all(row["status"] == "ok" for row in self.requests))
        if ok:
            print("    résultat métier à examiner : " + redact_text(repr(result)))
            if isinstance(result, KeywordVariantsOutcome):
                print("    diagnostic variantes : " + keyword_quality(result))
        return ok


@dataclass(frozen=True)
class NatureCase:
    """Une demande dont on exige qu'elle soit lue comme série, ou comme œuvre unique."""

    label: str
    prompt: str
    expect_series: bool


@dataclass(frozen=True)
class KeywordCase:
    """Un titre sur lequel on exige des mots-clés livrables."""

    label: str
    title: str
    category: str
    context: BookContext | None = None


NATURE_CASES = (
    # Cas de régression : un comics numéroté que la détection déterministe doit
    # rattraper quand le moteur le lit comme une œuvre unique.
    NatureCase(
        label="série numérotée non annoncée",
        prompt="Absolute Martian Manhunter, 10€",
        expect_series=True,
    ),
    NatureCase(
        label="tome explicitement demandé",
        prompt="One Piece tome 12, 8€",
        expect_series=False,
    ),
    NatureCase(
        label="one-shot",
        prompt="Le Monde sans fin, 25€",
        expect_series=False,
    ),
)

KEYWORD_CASES = (
    KeywordCase(
        label="titre de série nu",
        title="Absolute Martian Manhunter",
        category="bd_comics",
        context=BookContext(
            title="Absolute Martian Manhunter",
            author="Deniz Camp",
            publisher="DC Comics",
            publication_year=2025,
            is_series=True,
        ),
    ),
    KeywordCase(
        label="œuvre à confusions réelles",
        title="Blacksad",
        category="bd_comics",
        context=BookContext(
            title="Blacksad",
            author="Juan Díaz Canales",
            publisher="Dargaud",
            is_series=True,
        ),
    ),
)


@dataclass(frozen=True)
class AdvisoryCase:
    label: str
    kind: str
    title: str
    price_cents: int = 10000
    category: str = "gaming"
    expect_abstention: bool = False


ADVISORY_CASES = (
    AdvisoryCase("offre sur article à petit prix", "negotiation", "Jeu de société complet", 500),
    AdvisoryCase("offre sur console", "negotiation", "Console portable complète, bon état"),
    AdvisoryCase("score sur console", "profit_scoring", "Console portable complète, bon état",
                 expect_abstention=True),
    AdvisoryCase("score sur appareil incomplet", "profit_scoring", "Appareil photo pour pièces",
                 category="tech", expect_abstention=True),
    AdvisoryCase("confusions de titre", "auto_tuning", "Blacksad"),
)


def judge_advisory(case: AdvisoryCase, result: object) -> list[str]:
    if result is None:
        return ["aucune réponse exploitable"]
    if case.kind == "negotiation":
        if not isinstance(result, NegotiationResult):
            return ["offre invalide"]
        if not result.message.strip() or result.suggested_offer_cents <= 0:
            return ["offre inutilisable"]
    elif case.kind == "profit_scoring":
        if not isinstance(result, ProfitScoringResult):
            return ["score absent"]
        if case.expect_abstention:
            if result.is_abstention:
                return []
            return ["abstention motivée attendue : article non identifié"]
        if result.is_abstention:
            return []
        if result.pepite_score is None:
            return ["score absent"]
        if (result.estimated_resale_cents is None or result.net_profit_cents is None
                or result.purchase_cost_cents is None):
            return ["montants du score incomplets"]
        if result.net_profit_cents != result.estimated_resale_cents - result.purchase_cost_cents:
            return ["marge incohérente avec les montants retournés"]
    elif not isinstance(result, tuple) or len(result) != 2:
        return ["proposition d'exclusions invalide"]
    return []


async def run_advisory_case(case: AdvisoryCase, client: LLMClient, passes: int) -> CaseOutcome:
    failures: list[str] = []
    latencies: list[float] = []
    listing = Listing(
        id=uuid4(), title=case.title, category_slug=case.category,
        item_price_cents=case.price_cents, estimated_total_cents=case.price_cents,
        source_name="leboncoin", condition="Bon état",
    )
    module_offer = NegotiationModule(client=client)
    module_tuning = AutoTuningModule(client=client)
    pipeline = AIPostAlertPipeline(client=client)
    for _ in range(passes):
        started = time.monotonic()
        with _RequestCapture() as captured:
            result: object = None
            if case.kind == "negotiation":
                result = await module_offer.generate_offer(listing)
            elif case.kind == "auto_tuning":
                result = await module_tuning.suggest_exclusions(
                    case.title, ["Affiche Blacksad", "Ex-libris Blacksad", "Figurine Blacksad"],
                )
            else:
                prompt, owners = pipeline.build_request(listing, None)
                if "profit_scoring" not in owners:
                    failures.append("module de scoring désactivé")
                    break
                data = await client.generate_json(
                    COMBINED_SYSTEM_PROMPT, prompt,
                    response_model=PostAlertPayload, ai_module="post_alert",
                )
                if data is not None:
                    result = ProfitScoringModule(client=client).parse_chunk_result(
                        data.get("profit_scoring"), listing, None,
                    )
        latencies.append(time.monotonic() - started)
        if not captured.report(latencies[-1], result):
            result = None
        failures.extend(judge_advisory(case, result))
    return CaseOutcome(case.label, failures, latencies)


def judge_nature(case: NatureCase, result: NaturalAddResult) -> list[str]:
    """Ce que le livrable doit respecter, quel que soit le moteur. Liste vide = conforme."""
    failures: list[str] = []
    if (case.expect_series and not result.is_series and result.book_unidentified):
        failures.append("nature non vérifiée : aucune identité bibliographique établie")
    if result.is_series is not case.expect_series:
        failures.append(
            f"nature : série attendue={case.expect_series}, obtenue={result.is_series}"
        )
    if PRICE_MARK in result.name:
        failures.append(f"un prix s'est glissé dans le nom : {result.name!r}")
    if case.expect_series and result.isbn13 is not None:
        failures.append(f"un ISBN est rattaché à une série : {result.isbn13}")
    return failures


def keyword_quality(outcome: KeywordVariantsOutcome) -> str:
    if not outcome.answered:
        return "unanswered"
    if outcome.proposed_variants is None:
        return "not_observed"
    normalized = [normalize_text(value) for value in outcome.proposed_variants]
    if len(normalized) != len(set(normalized)):
        return "repetitive"
    if not normalized:
        return "empty_proposal"
    if not outcome.variants:
        return "fully_filtered"
    if outcome.variant_assessment and outcome.variant_assessment.removals:
        return "partly_filtered"
    return "useful_variants"


def judge_keywords(case: KeywordCase, outcome: KeywordVariantsOutcome) -> list[str]:
    """Ce que la liste livrée doit respecter. Liste vide = conforme.

    Le verdict porte sur ce que l'utilisateur voit : le module applique déjà le
    nettoyeur, donc une exclusion qui ne serait que le titre plus un numéro est
    structurellement impossible — c'est ce qui est vérifié, pas ce qui est espéré.
    """
    failures: list[str] = []
    if not outcome.answered:
        failures.append("aucune réponse : JSON invalide, erreur ou délai dépassé")
    quality = keyword_quality(outcome)
    if quality == "repetitive":
        failures.append("propositions répétitives après normalisation")
    if (outcome.proposed_variants and not outcome.variants):
        failures.append("propositions entièrement filtrées : aucune variante utile conservée")
    if (outcome.variant_assessment and any(
        removal.reason in (Reason.FOREIGN_NUMBER, Reason.DRIFTED_WRITING)
        for removal in outcome.variant_assessment.removals
    )):
        failures.append("propositions hors identité de la recherche")
    if len(outcome.exclusions) > MAX_DELIVERED_EXCLUSIONS:
        failures.append(
            f"{len(outcome.exclusions)} exclusions livrées, "
            f"{MAX_DELIVERED_EXCLUSIONS} au plus"
        )
    for exclusion in outcome.exclusions:
        if names_only_a_volume(exclusion, case.title):
            failures.append(
                f"exclusion qui n'est que le titre plus un numéro : {exclusion!r}"
            )
    return failures


@dataclass
class CaseOutcome:
    """Le verdict d'un cas et la latence observée sur ses passes."""

    label: str
    failures: list[str]
    latencies: list[float]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def median_seconds(self) -> float | None:
        return statistics.median(self.latencies) if self.latencies else None


async def _timed(coro):
    with _RequestCapture() as captured:
        started = time.monotonic()
        result = await coro
        elapsed = time.monotonic() - started
    if not captured.report(elapsed, result):
        result = None
    return result, elapsed


async def run_nature_case(case: NatureCase, passes: int) -> CaseOutcome:
    """Rejoue une demande de `/add` sur le module réel, `passes` fois."""
    failures: list[str] = []
    latencies: list[float] = []
    seen_failure = False
    for _ in range(passes):
        result, elapsed = await _timed(natural_add_module.parse_prompt(case.prompt))
        latencies.append(elapsed)
        if result is None:
            failures.append("aucun résultat : le parsing a échoué")
            seen_failure = True
            continue
        case_failures = judge_nature(case, result)
        if case_failures and not seen_failure:
            failures.extend(case_failures)
            seen_failure = True
    return CaseOutcome(label=case.label, failures=failures, latencies=latencies)


async def run_keyword_case(case: KeywordCase, passes: int) -> CaseOutcome:
    """Rejoue la recherche de mots-clés sur le module réel, `passes` fois."""
    failures: list[str] = []
    latencies: list[float] = []
    seen_failure = False
    for _ in range(passes):
        outcome, elapsed = await _timed(
            keyword_variants_module.generate_variants_and_exclusions(
                case.title, category_slug=case.category, context=case.context
            )
        )
        latencies.append(elapsed)
        case_failures = (judge_keywords(case, outcome) if outcome is not None
                         else ["aucune réponse exploitable"])
        if case_failures and not seen_failure:
            failures.extend(case_failures)
            seen_failure = True
    return CaseOutcome(label=case.label, failures=failures, latencies=latencies)


def _render(case_outcome: CaseOutcome) -> None:
    mark = f"{GREEN}PASS{RESET}" if case_outcome.ok else f"{RED}ÉCHEC{RESET}"
    median = case_outcome.median_seconds
    latency = f"{DIM}{median:.1f}s médiane{RESET}" if median is not None else ""
    print(f"  {mark}  {case_outcome.label} {latency}")
    for failure in case_outcome.failures:
        print(f"        {YELLOW}{redact_text(failure)}{RESET}")


def expected_call_count(args: argparse.Namespace) -> int:
    modules = set(args.module or ("natural_add", "keyword_variants", "profit_scoring",
                                 "negotiation", "auto_tuning"))
    return args.passes * (
        (len(NATURE_CASES) if "natural_add" in modules else 0)
        + (len(KEYWORD_CASES) if "keyword_variants" in modules else 0)
        + sum(case.kind in modules for case in ADVISORY_CASES)
    )


async def _run(args: argparse.Namespace) -> int:
    if not settings.AI_ENABLED:
        print(f"{RED}AI_ENABLED=false : rien à mesurer.{RESET}", file=sys.stderr)
        return 2
    if not settings.AI_API_KEY.get_secret_value():
        print(
            f"{RED}AI_API_KEY absente : configurez l'environnement dédié.{RESET}",
            file=sys.stderr,
        )
        return 2

    client = build_llm_client(args.provider)
    if args.model:
        client.model = args.model
    if args.base_url:
        client.base_url = args.base_url.rstrip("/")

    print(f"{BOLD}Mesure de la qualité d'un moteur IA{RESET}")
    print(f"  fournisseur : {client.vendor}")
    print(f"  modèle      : {client.model}")
    print(f"  passes      : {args.passes} par cas")
    print(f"  appels prévus : {expected_call_count(args)}")
    print(f"  {DIM}la clé n'est jamais affichée{RESET}")

    # Les modules reçoivent le client par assignation : c'est le point d'injection des
    # tests, le seul qui mesure le chemin réel sans le réécrire.
    natural_add_module.client = client
    keyword_variants_module.client = client

    modules = set(args.module or ("natural_add", "keyword_variants", "profit_scoring",
                                 "negotiation", "auto_tuning"))
    outcomes: list[CaseOutcome] = []
    print(f"\n{BOLD}Nature de l'œuvre (nature de la demande){RESET}")
    for case in (NATURE_CASES if "natural_add" in modules else ()):
        outcome = await run_nature_case(case, args.passes)
        outcomes.append(outcome)
        _render(outcome)

    print(f"\n{BOLD}Mots-clés livrés{RESET}")
    for keyword_case in (KEYWORD_CASES if "keyword_variants" in modules else ()):
        outcome = await run_keyword_case(keyword_case, args.passes)
        outcomes.append(outcome)
        _render(outcome)

    print(f"\n{BOLD}Conseils à examiner manuellement (français, pertinence, prudence){RESET}")
    for advisory_case in (case for case in ADVISORY_CASES if case.kind in modules):
        outcome = await run_advisory_case(advisory_case, client, args.passes)
        outcomes.append(outcome)
        _render(outcome)

    failed = [outcome for outcome in outcomes if not outcome.ok]
    print()
    if failed:
        print(f"{RED}{len(failed)} cas sur {len(outcomes)} échouent.{RESET}")
        return 1
    print(f"{GREEN}{len(outcomes)} cas sur {len(outcomes)} passent.{RESET}")
    return 0


async def run(args: argparse.Namespace) -> int:
    previous_natural = natural_add_module.client
    previous_keywords = keyword_variants_module.client
    try:
        return await _run(args)
    finally:
        natural_add_module.client = previous_natural
        keyword_variants_module.client = previous_keywords
        await close_shared_http_client()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rejoue les cas réels du dépôt sur le chemin IA de production et rend un "
            "verdict mécanique, pour comparer deux moteurs sans se fier à une impression."
        )
    )
    parser.add_argument(
        "--provider",
        help="valeur d'AI_PROVIDER à viser (défaut : celle de la configuration)",
    )
    parser.add_argument(
        "--model",
        help="modèle à viser (défaut : AI_MODEL). La clé vient de AI_API_KEY.",
    )
    parser.add_argument(
        "--base-url",
        help="URL de base à viser (défaut : AI_BASE_URL)",
    )
    parser.add_argument(
        "--module", action="append",
        choices=("natural_add", "keyword_variants", "profit_scoring", "negotiation", "auto_tuning"),
        help="usage à vérifier ; répétable, tous les usages par défaut",
    )
    parser.add_argument(
        "--passes",
        type=int,
        default=3,
        help="nombre de passes par cas (défaut : 3). Le moteur est stochastique.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.passes < 1:
        print(f"{RED}--passes doit valoir au moins 1.{RESET}", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

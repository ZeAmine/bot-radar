#!/usr/bin/env python3
"""Explique un couple ou évalue un corpus explicitement fourni, exclusivement hors ligne.

Usage : python scripts/matching_diagnostics.py explain pair.json
        python scripts/matching_diagnostics.py evaluate corpus.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# Exécution directe, depuis n'importe quel répertoire ; aucun import de settings/session.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models.alert import Alert  # noqa: E402
from app.db.models.category import Category  # noqa: E402
from app.domain.enums import AlertStatus, CategorySlug  # noqa: E402
from app.domain.rules.listing_match import evaluate_listing_against_alert  # noqa: E402
from app.services.tome_extractor import TomeExtractor  # noqa: E402
from app.utils.text import normalize_text  # noqa: E402

Text = Annotated[str, Field(max_length=4096)]
Term = Annotated[str, Field(min_length=1, max_length=128)]
Terms = Annotated[list[Term], Field(max_length=32)]
Label = Literal["accepter", "rejeter", "indetermine", "non_etiquete"]
Source = Literal["business_test", "human_validated", "none"]
MAX_INPUT_BYTES = 1_048_576


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AlertSnapshot(InputModel):
    name: Annotated[str, Field(max_length=255)]
    category_slug: Literal["bd_comics", "gaming", "tech", "clothes"]
    status: Literal["active", "paused", "deleted"] = "active"
    isbn: Annotated[str, Field(max_length=32)] | None = None
    include_keywords: Terms = Field(default_factory=list)
    variants: Terms = Field(default_factory=list)
    exclude_keywords: Terms = Field(default_factory=list)
    max_item_price_cents: Annotated[int, Field(ge=0)] | None = None
    accepted_conditions: Terms | None = None


class ListingSnapshot(InputModel):
    title: Text
    description: Text | None = None
    category_slug: Literal["bd_comics", "gaming", "tech", "clothes"]
    item_price_cents: Annotated[int, Field(ge=0)]
    condition: Annotated[str, Field(max_length=128)] | None = None
    isbn13: Annotated[str, Field(max_length=32)] | None = None
    url: Text | None = None


class Pair(InputModel):
    alert: AlertSnapshot
    listing: ListingSnapshot


class Annotation(InputModel):
    expected: Label = "non_etiquete"
    source: Source = "none"
    reviewed: bool = False

    @model_validator(mode="after")
    def consistent_label(self) -> Self:
        if (self.expected == "non_etiquete") != (self.source == "none"):
            raise ValueError("label_source_mismatch")
        if self.reviewed != (self.source == "human_validated"):
            raise ValueError("human_review_required")
        return self


class Case(InputModel):
    pair: Pair
    origin: Literal["synthetic", "provided"]
    annotation: Annotation = Field(default_factory=Annotation)


class Corpus(InputModel):
    version: Literal[1]
    cases: Annotated[list[Case], Field(max_length=200)]


def explain_pair(pair: Pair) -> dict[str, Any]:
    """Une seule décision réelle ; ni raison libre ni données d'identité dans le rapport."""
    a, listing = pair.alert, pair.listing
    category = Category(id=UUID(int=0), slug=a.category_slug, label="", emoji="")
    alert = Alert(
        id=UUID(int=0),
        user_id=UUID(int=0),
        category_id=category.id,
        category=category,
        name=a.name,
        status=AlertStatus(a.status),
        isbn=a.isbn,
        include_keywords=a.include_keywords,
        variants=a.variants,
        exclude_keywords=a.exclude_keywords,
        max_item_price_cents=a.max_item_price_cents,
        accepted_conditions=a.accepted_conditions,
    )
    result = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug=CategorySlug(listing.category_slug).value,
        listing_normalized_title=normalize_text(listing.title),
        listing_raw_title=listing.title,
        listing_volume=(TomeExtractor.extract(
            listing.title, listing.description, series_key=a.name,
        ) if listing.category_slug == CategorySlug.BD_COMICS.value else None),
        listing_normalized_description=listing.description,
        listing_condition=listing.condition,
        listing_item_price_cents=listing.item_price_cents,
        listing_isbn13=listing.isbn13,
        listing_raw_url=listing.url,
    )
    return {
        "mode": "rejeu",
        "decision": "accepter" if result.is_match else "rejeter",
        "reason_code": result.reason_code,
        "acceptance_path": result.acceptance_path,
        "decisive_details": dict(result.decisive_details),
        "evaluated_steps": result.evaluated_steps,
        "not_evaluated_steps": result.not_evaluated_steps,
        "configured_keyword_score": result.keyword_match_score,
        "legacy_condition_matches": result.condition_matches,
        "legacy_price_matches": result.price_matches,
    }


def ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "value": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
    }


def evaluate_corpus(corpus: Corpus) -> dict[str, Any]:
    """Sépare attentes de test, validation humaine et absence de décision métier."""
    groups: dict[str, Any] = {
        source: {
            "population": 0,
            "binary_labeled": 0,
            "indeterminate": 0,
            "matrix": {
                expected: dict.fromkeys(("accepter", "rejeter"), 0)
                for expected in ("accepter", "rejeter")
            },
        }
        for source in ("business_test", "human_validated")
    }
    disagreements = []
    diagnostics = []
    unlabeled = 0
    origins = {"synthetic": 0, "provided": 0}
    for index, case in enumerate(corpus.cases):
        explanation = explain_pair(case.pair)
        actual = explanation["decision"]
        annotation = case.annotation
        origins[case.origin] += 1
        diagnostics.append({"case_index": index, **explanation})
        if annotation.source == "none":
            unlabeled += 1
            continue
        group = groups[annotation.source]
        group["population"] += 1
        if annotation.expected == "indetermine":
            group["indeterminate"] += 1
            continue
        group["binary_labeled"] += 1
        group["matrix"][annotation.expected][actual] += 1
        if actual != annotation.expected:
            disagreements.append(
                {
                    "case_index": index,
                    "label_source": annotation.source,
                    "expected": annotation.expected,
                    "actual": actual,
                    "reason_code": explanation["reason_code"],
                }
            )
    for group in groups.values():
        matrix = group["matrix"]
        tp = matrix["accepter"]["accepter"]
        group["sample_precision"] = ratio(tp, tp + matrix["rejeter"]["accepter"])
        group["sample_recall"] = ratio(tp, tp + matrix["accepter"]["rejeter"])
    return {
        "mode": "rejeu",
        "population": len(corpus.cases),
        "origins": origins,
        "unlabeled": unlabeled,
        "groups": groups,
        "disagreements": disagreements,
        "diagnostics": diagnostics,
        "scope": "Corpus sélectionné : aucune précision globale de production revendiquée.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("explain", "evaluate"))
    parser.add_argument("input", type=Path)
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as stream:
            data = stream.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("input_too_large")
        raw = json.loads(data)
        report = (
            explain_pair(Pair.model_validate(raw))
            if args.mode == "explain"
            else evaluate_corpus(Corpus.model_validate(raw))
        )
    except (OSError, ValueError, ValidationError):
        # ValidationError et OSError peuvent contenir payload, chemin ou secret : ne pas les rendre.
        print(json.dumps({"error": "invalid_input"}))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

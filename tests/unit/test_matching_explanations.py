"""Les explications suivent la décision réelle, sans modifier ses sorties historiques."""

from types import SimpleNamespace

import pytest

from app.domain.enums import AlertStatus
from app.domain.rules.listing_match import evaluate_listing_against_alert


def evaluate(title="blacksad", price=1000, **alert_changes):
    alert = SimpleNamespace(
        **(
            {
                "status": AlertStatus.ACTIVE,
                "category": SimpleNamespace(slug="bd_comics"),
                "name": "Blacksad",
                "isbn": None,
                "include_keywords": ["blacksad"],
                "variants": [],
                "exclude_keywords": [],
                "max_item_price_cents": 1500,
                "accepted_conditions": None,
            }
            | alert_changes
        )
    )
    return evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title=title,
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=price,
    )


@pytest.mark.parametrize(
    ("changes", "title", "price", "code", "last"),
    [
        ({"status": AlertStatus.PAUSED}, "blacksad poster", 1600, "alert_inactive", "status"),
        (
            {"category": SimpleNamespace(slug="tech")},
            "blacksad",
            1600,
            "category_mismatch",
            "category",
        ),
        ({}, "blacksad poster", 1600, "price_above_max", "price"),
        ({}, "blacksad poster", 1000, "default_exclusion", "default_exclusions"),
        (
            {"exclude_keywords": ["secret_synthetic"]},
            "blacksad secret_synthetic",
            1000,
            "user_exclusion",
            "user_exclusions",
        ),
        ({}, "autre ouvrage", 1000, "required_keywords_missing", "includes"),
        (
            {"accepted_conditions": ["Neuf avec étiquette"]},
            "blacksad",
            1000,
            "condition_rejected",
            "condition",
        ),
    ],
)
def test_first_decisive_reason_and_unvisited_steps(changes, title, price, code, last):
    result = evaluate(title, price, **changes)
    assert result.reason_code == code
    assert result.evaluated_steps[-1] == last
    assert not set(result.evaluated_steps) & set(result.not_evaluated_steps)
    assert result.acceptance_path is None


@pytest.mark.parametrize(
    ("changes", "title", "path"),
    [
        ({}, "blacksad", "includes"),
        # La variante doit être une écriture de ce que l'alerte cherche ;
        # « blaksad » est la faute de frappe qui montre la même route.
        ({"variants": ["blaksad"]}, "blaksad", "variant"),
        ({"isbn": "9782205049657"}, "bd blacksad", "isbn_hybrid/includes"),
        ({"isbn": "9782205049657"}, "9782205049657", "isbn_text"),
        ({"include_keywords": []}, "autre ouvrage", "unconstrained"),
    ],
)
def test_acceptance_route_without_reinterpreting_score(changes, title, path):
    result = evaluate(title, **changes)
    assert result.is_match
    assert result.reason == "Match confirmé"
    assert result.reason_code == "accepted"
    assert result.acceptance_path == path
    assert result.keyword_match_score == len(changes.get("include_keywords", ["blacksad"]))
    assert result.not_evaluated_steps == ()


def test_missing_keyword_evidence_is_an_index_without_echoing_content():
    result = evaluate("blacksad", include_keywords=["blacksad", "secret_synthetic"])
    assert result.decisive_details == (("missing_include_index", 1),)
    assert "secret_synthetic" not in repr(result.decisive_details)


def test_rule_predicate_is_not_repeated_for_explanation(monkeypatch):
    import app.domain.rules.listing_match as rules

    original = rules.price_is_above_max
    calls = []

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(rules, "price_is_above_max", counted)
    result = evaluate(price=1600)
    assert result.reason_code == "price_above_max"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("name", "title", "code"),
    [
        ("Blacksad", "autre ouvrage", "isbn_keywords_missing"),
        ("RIP", "rip", "isbn_fallback_rejected"),
    ],
)
def test_isbn_rejection_distinguishes_missing_terms_and_unproved_fallback(name, title, code):
    result = evaluate(title, name=name, isbn="9782205049657", include_keywords=[name])
    assert result.reason_code == code
    assert result.evaluated_steps == ("status", "category", "price", "isbn")
    assert result.not_evaluated_steps == (
        "default_exclusions",
        "user_exclusions",
        "gaming_language",
        "includes",
        "condition",
    )


def test_gaming_rejection_is_explained_without_changing_language_policy():
    alert = SimpleNamespace(
        status=AlertStatus.ACTIVE,
        category=SimpleNamespace(slug="gaming"),
        name="Jeu exemple",
        isbn=None,
        include_keywords=["jeu exemple"],
        variants=[],
        exclude_keywords=[],
        max_item_price_cents=None,
        accepted_conditions=None,
    )
    result = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="gaming",
        listing_normalized_title="jeu exemple en castellano",
        listing_normalized_description=None,
        listing_condition=None,
        listing_item_price_cents=1000,
    )
    assert result.reason_code == "gaming_language_rejected"
    assert not result.is_match
    assert result.not_evaluated_steps == ("includes", "condition")


def test_declared_isbn_does_not_erase_the_inclusion_requirement():
    alert = SimpleNamespace(
        status=AlertStatus.ACTIVE,
        category=SimpleNamespace(slug="bd_comics"),
        name="Blacksad",
        isbn="9782205049657",
        include_keywords=["blacksad"],
        variants=[],
        exclude_keywords=[],
        max_item_price_cents=None,
        accepted_conditions=None,
    )
    args = {
        "alert": alert,
        "listing_category_slug": "bd_comics",
        "listing_normalized_description": None,
        "listing_condition": None,
        "listing_item_price_cents": 1000,
        "listing_isbn13": "9782205049657",
    }
    rejected = evaluate_listing_against_alert(**args, listing_normalized_title="autre ouvrage")
    accepted = evaluate_listing_against_alert(**args, listing_normalized_title="blacksad")
    assert rejected.reason_code == "required_keywords_missing"
    assert accepted.acceptance_path == "isbn_field/includes"


def test_entity_adapter_preserves_the_explanation():
    """Le rejet réel indique l'inclusion manquante sans évaluer l'état ensuite."""
    result = evaluate(title="autre ouvrage", max_item_price_cents=None)
    assert result.reason_code == "required_keywords_missing"
    assert result.decisive_details == (("missing_include_index", 0),)
    assert result.not_evaluated_steps == ("condition",)

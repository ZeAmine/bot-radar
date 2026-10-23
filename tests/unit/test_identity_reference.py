import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.enums import ProviderQueryStatus
from app.providers.google_books import ProviderQueryResult
from app.services.book_metadata_service import BookMetadata, BookMetadataService
from scripts.matching_diagnostics import Pair, explain_pair

CORPUS = json.loads(
    (
        Path(__file__).resolve().parents[2] / "docs/evaluations/identity-reference-v1.json"
    ).read_text()
)
CASES = CORPUS["cases"]


async def replay(case):
    if case["replay"] == "catalogue":
        service = BookMetadataService()
        meta = BookMetadata(
            title=case["candidate_title"],
            isbn13=case["candidate_isbn"],
            author=case.get("candidate_author"),
            language=case.get("candidate_language"),
        )
        response = ProviderQueryResult(status=ProviderQueryStatus.SUCCESS, items=(meta,))
        with (
            patch.object(service, "_bnf_records_result", AsyncMock(return_value=(response, None))),
            patch.object(
                service, "_complete_series_from_openlibrary", AsyncMock(return_value=([], None))
            ),
        ):
            verdict = await service.lookup_series_notices(
                case["expected_title"], author_hint=case.get("author_hint")
            )
        return bool(verdict.notices)
    pair = Pair.model_validate(
        {
            "alert": {
                "name": case["expected_title"],
                "category_slug": "bd_comics",
                "isbn": case["expected_isbn"],
            },
            "listing": {
                "title": case["candidate_title"],
                "category_slug": "bd_comics",
                "isbn13": case["candidate_isbn"],
                "item_price_cents": 100,
            },
        }
    )
    return explain_pair(pair)["decision"] == "accepter"


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["label"] != "ambigu"], ids=lambda c: c["id"]
)
async def test_labelled_identity_reference(case):
    assert await replay(case) == case.get("expected_acceptance", case["label"] == "correspond")


@pytest.mark.parametrize(
    "title", ["Blacksad tome 2", "BLACKSAD TOME 2", "Blacksad : tome 2 !", "Blàcksad — tome 2"]
)
def test_another_volume_never_matches_a_requested_volume(title):
    pair = Pair.model_validate(
        {
            "alert": {
                "name": "Blacksad tome 1",
                "category_slug": "bd_comics",
                "include_keywords": ["blacksad"],
            },
            "listing": {"title": title, "category_slug": "bd_comics", "item_price_cents": 100},
        }
    )
    assert explain_pair(pair)["decision"] != "accepter"


@pytest.mark.parametrize(
    "title", ["Hélène de Wyndhorn", "HELENE DE WYNDHORN", "Hélène — de Wyndhorn !"]
)
def test_matching_normalization_preserves_true_positives_without_a_book(title):
    pair = Pair.model_validate(
        {
            "alert": {
                "name": "Hélène de Wyndhorn",
                "category_slug": "bd_comics",
                "include_keywords": ["Hélène de Wyndhorn"],
            },
            "listing": {"title": title, "category_slug": "bd_comics", "item_price_cents": 100},
        }
    )
    assert explain_pair(pair)["decision"] == "accepter"


@pytest.mark.parametrize(
    "title", ["Blacksad intégrale", "Blacksad lot de 3", "Blacksad tomes 1-3", "Blacksad tomes 1 3"]
)
def test_a_bundle_does_not_prove_an_individual_volume(title):
    pair = Pair.model_validate(
        {
            "alert": {
                "name": "Blacksad tome 3",
                "category_slug": "bd_comics",
                "include_keywords": ["blacksad"],
            },
            "listing": {"title": title, "category_slug": "bd_comics", "item_price_cents": 100},
        }
    )
    assert explain_pair(pair)["decision"] == "rejeter"


@pytest.mark.parametrize(
    ("author", "language"), [(None, "fr"), ("Inconnu", "fr"), ("Manu Larcenet", None)]
)
def test_catalogue_abstains_without_work_and_language_evidence(author, language):
    from app.domain.rules.bibliographic_identity import series_association

    decision = series_association(
        requested_title="Blast",
        title="Blast tome 1",
        author=author,
        author_hint=None,
        language=language,
        number=1,
    )
    assert decision.outcome == "abstain"


async def test_abstention_does_not_mean_the_catalogue_is_exhausted():
    from app.domain.rules.catalogue_search import is_catalogue_exhausted

    service = BookMetadataService()
    response = ProviderQueryResult(
        status=ProviderQueryStatus.SUCCESS,
        has_results=True,
        items=(BookMetadata(title="Blast", isbn13="9782492759147", language="fr"),),
    )
    with (
        patch.object(service, "_bnf_records_result", AsyncMock(return_value=(response, None))),
        patch.object(
            service, "_complete_series_from_openlibrary", AsyncMock(return_value=([], None))
        ),
    ):
        result = await service.lookup_series_notices("Blast")
    assert result.notices == ()
    assert not is_catalogue_exhausted(result)


def test_a_seller_isbn_cannot_select_another_cached_edition():
    from types import SimpleNamespace
    from uuid import UUID

    from app.db.models.book import Book
    from app.db.models.listing import Listing
    from app.use_cases.ingest_listing import comparison_line_from_loaded_books

    book = Book(id=UUID(int=1), title="Blacksad tome 1", isbn13="9782205049657")
    book.market_cache = None
    alert = SimpleNamespace(name="Blacksad", isbn=None, alert_books=[SimpleNamespace(book=book)])
    listing = Listing(
        title="Blacksad tome 1",
        description="",
        isbn13="9782205051995",
        item_price_cents=100,
        source_name="synthetic",
        external_id="1",
        url="https://example.invalid/1",
    )
    _, _, selected = comparison_line_from_loaded_books(listing, alert)
    assert selected is None


@pytest.mark.parametrize(
    "expected", ["Blacksad Livre 1", "Blacksad tome I", "Blacksad tome 1 (Tome 1)"]
)
def test_volume_spellings_keep_the_conflict_guard(expected):
    pair = Pair.model_validate(
        {
            "alert": {
                "name": expected,
                "category_slug": "bd_comics",
                "include_keywords": ["blacksad"],
            },
            "listing": {
                "title": "Blacksad tome 2",
                "category_slug": "bd_comics",
                "item_price_cents": 100,
            },
        }
    )
    assert explain_pair(pair)["decision"] == "rejeter"


@pytest.mark.parametrize(
    ("author", "hint", "language", "accepted"),
    [
        ("Philippe Claudel", None, "fr", False),
        ("Philippe Claudel", "Manu Larcenet", "fr", False),
        ("Manu Larcenet", "Manu Larcenet", None, False),
        ("Manu Larcenet", "Manu Larcenet", "fr", True),
    ],
)
async def test_single_work_discovery_requires_identity(author, hint, language, accepted):
    service = BookMetadataService()
    meta = BookMetadata(
        title="Le Rapport de Brodeck", isbn13="9782205073850", author=author, language=language
    )
    response = ProviderQueryResult(
        status=ProviderQueryStatus.SUCCESS, items=(meta,), has_results=True
    )
    query = "Le Rapport de Brodeck" + (f" par {hint}" if hint else "")
    with patch.object(service, "_bnf_records_result", AsyncMock(return_value=(response, None))):
        result = await service.lookup_by_title_and_author(query)
    assert bool(result.notices) is accepted


async def test_an_unknown_isbn_never_falls_back_to_a_different_edition():
    from app.db.models.book import Book
    from app.services.book_catalog_service import BookCatalogService

    service = BookCatalogService(AsyncMock())
    service.book_repo.get_by_isbn13 = AsyncMock(return_value=None)
    service.book_repo.get_by_normalized_title = AsyncMock(
        return_value=[
            Book(title="Blacksad", isbn13="9782205049657", normalized_title="blacksad"),
        ]
    )
    book, confidence = await service.identify_book(isbn13="9782205051995", title="Blacksad")
    assert book is None
    assert confidence is None


async def test_a_contradictory_authority_language_invalidates_an_initial_series_isbn():
    service = BookMetadataService()
    code = "9782205049657"
    french_candidate = BookMetadata(
        title="Blacksad tome 1", isbn13=code, author="Juan Diaz Canales", language="fr"
    )
    foreign_notice = BookMetadata(
        title="Blacksad tome 1", isbn13=code, author="Juan Diaz Canales", language="en"
    )
    with (
        patch.object(
            service,
            "_bnf_records_result",
            AsyncMock(
                return_value=(
                    ProviderQueryResult(items=(french_candidate,), has_results=True),
                    None,
                )
            ),
        ),
        patch.object(
            service, "_openlibrary_works", AsyncMock(return_value=([], ProviderQueryResult()))
        ),
        patch.object(service, "verify_isbn", AsyncMock(return_value=foreign_notice)),
    ):
        result = await service.lookup_series_notices(
            "Blacksad", author_hint="Juan Diaz Canales", for_creation=True
        )
    assert result.notices == ()
    assert result.rejected_isbns == (code,)


def test_a_related_title_by_the_same_author_is_not_work_identity():
    from app.domain.rules.bibliographic_identity import series_association

    result = series_association(
        requested_title="Blacksad",
        title="Blacksad Stories",
        author="Juan Diaz Canales",
        author_hint="Juan Diaz Canales",
        language="fr",
        number=None,
    )
    assert result.outcome != "accept"


@pytest.mark.parametrize("title", ["Blacksad t1 et t2", "Blacksad tome 1 volume 2"])
def test_explicit_multiple_volumes_cannot_pass_as_one(title):
    pair = Pair.model_validate(
        {
            "alert": {
                "name": "Blacksad tome 1",
                "category_slug": "bd_comics",
                "include_keywords": ["blacksad"],
            },
            "listing": {"title": title, "category_slug": "bd_comics", "item_price_cents": 100},
        }
    )
    assert explain_pair(pair)["decision"] == "rejeter"


@pytest.mark.parametrize(
    ("title", "number", "reason"),
    [
        ("Blacksad tome 2", 2, "volume_conflict"),
        ("Blacksad intégrale", None, "collection_for_volume"),
        ("Blacksad tome 1 et tome 2", 1, "collection_for_volume"),
    ],
)
def test_proven_french_author_does_not_override_volume_conflicts(title, number, reason):
    from app.domain.rules.bibliographic_identity import series_association

    result = series_association(
        requested_title="Blacksad tome 1",
        title=title,
        author="Juan Diaz Canales",
        author_hint="Juan Diaz Canales",
        language="fr",
        number=number,
    )
    assert result.outcome == "reject"
    assert result.reason == reason


@pytest.mark.parametrize(
    ("title", "accepted"),
    [
        ("Blacksad tome 1", False),
        ("Blacksad tomes 1 et 2", True),
    ],
)
def test_every_requested_volume_requires_listing_coverage(title, accepted):
    pair = Pair.model_validate(
        {
            "alert": {
                "name": "Blacksad tomes 1 et 2",
                "category_slug": "bd_comics",
                "include_keywords": ["blacksad"],
            },
            "listing": {"title": title, "category_slug": "bd_comics", "item_price_cents": 100},
        }
    )
    assert (explain_pair(pair)["decision"] == "accepter") is accepted


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Martin Panchaud", "Martin Smith"),
        ("François Dupont", "François Durand"),
    ],
)
def test_a_shared_first_name_does_not_merge_distinct_works(first, second):
    left = BookMetadata(title="Homonyme", isbn13="9782205049657", author=first)
    right = BookMetadata(title="Homonyme", isbn13="9782205051995", author=second)
    assert not BookMetadataService._is_same_work(left, right)


@pytest.mark.parametrize(
    ("title", "accepted"),
    [("Blacksad tome I", False), ("Blacksad tomes I et II", True)],
)
def test_roman_volume_requests_require_complete_coverage(title, accepted):
    pair = Pair.model_validate({
        "alert": {
            "name": "Blacksad tomes I et II",
            "category_slug": "bd_comics",
            "include_keywords": ["blacksad"],
        },
        "listing": {"title": title, "category_slug": "bd_comics", "item_price_cents": 100},
    })
    assert (explain_pair(pair)["decision"] == "accepter") is accepted


def test_a_negated_reference_to_other_volumes_preserves_the_matching_volume():
    pair = Pair.model_validate({
        "alert": {"name": "Blacksad tome 1", "category_slug": "bd_comics", "isbn": "9782205049657"},
        "listing": {
            "title": "Blacksad tome 1",
            "description": "Les autres tomes ne sont pas disponibles",
            "isbn13": "9782205049657",
            "category_slug": "bd_comics",
            "item_price_cents": 100,
        },
    })
    assert explain_pair(pair)["decision"] == "accepter"


@pytest.mark.parametrize(
    ("title", "number"),
    [("Blacksad tome 1/5", 1), ("Blacksad tome 1 d’occasion", 1),
     ("Blacksad tome 4 L’Enfer, le silence", 4)],
)
def test_volume_notation_preserves_single_volume_positives(title, number):
    from app.domain.rules.book_volume import is_volume_composition, requested_volume

    assert requested_volume(title) == number
    assert not is_volume_composition(title)


@pytest.mark.parametrize(
    "description",
    ["Livre d'occasion en bon état", "Livre d’or", "vol d'oiseau", "t c'est"],
)
def test_prose_in_the_description_is_not_a_roman_volume(description):
    pair = Pair.model_validate({
        "alert": {"name": "Blacksad tome 1", "category_slug": "bd_comics",
                  "include_keywords": ["blacksad"]},
        "listing": {"title": "Blacksad tome 1", "description": description,
                    "category_slug": "bd_comics", "item_price_cents": 100},
    })
    assert explain_pair(pair)["decision"] == "accepter"


@pytest.mark.parametrize("phrase", ["livre d'occasion", "livre d'or", "vol d'oiseau", "t c'est"])
def test_normalized_french_contractions_do_not_declare_roman_volumes(phrase):
    from app.domain.rules.book_volume import volume_numbers
    from app.utils.text import normalize_text

    assert volume_numbers(phrase) == set()
    assert volume_numbers(normalize_text(phrase)) == set()


@pytest.mark.parametrize(
    ("wanted", "candidate", "number"),
    [("Le livre d’or", "Le livre d’occasion", 500),
     ("Le vol d’oiseau", "Le vol d’argent", 500),
     ("Le t c’est ici", "Le t c’est ailleurs", 100)],
)
def test_title_identity_does_not_split_french_contractions_as_roman_volumes(
    wanted, candidate, number,
):
    from app.domain.rules.bibliographic_identity import series_association

    result = series_association(
        requested_title=wanted, title=candidate, author="Auteur connu",
        author_hint="Auteur connu", language="fr", number=number,
    )
    assert result.outcome != "accept"


def test_the_listing_domain_does_not_import_a_service():
    import ast

    path = Path(__file__).resolve().parents[2] / "app/domain/rules/listing_match.py"
    tree = ast.parse(path.read_text())
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(name and name.startswith("app.services") for name in imports)


@pytest.mark.parametrize(
    ("title", "number"),
    [("Blacksad tome d", 500), ("Blacksad tome c : le retour", 100),
     ("Blacksad tome liv", 54), ("Blacksad tome D - Le retour", 500)],
)
def test_unambiguous_roman_markers_still_declare_a_volume(title, number):
    from app.domain.rules.book_volume import requested_volume

    assert requested_volume(title) == number

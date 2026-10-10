"""Empreinte de non-régression F05 ; elle ne constitue pas une étiquette humaine.

Les cinq sorties historiques sont capturées depuis le matcher inchangé de 0cb54b7.
Le produit cartésien est fixe : aucune sortie F10 ne définit l'attendu.
"""

import hashlib
import itertools
import json
from types import SimpleNamespace

from app.domain.rules.listing_match import evaluate_listing_against_alert


def functional_digest():
    outputs = []
    for (
        status, category, isbn, terms, title, price, condition, listing_code, raw_url
    ) in itertools.product(
        ["active", "paused"],
        ["bd_comics", "gaming", "tech"],
        [None, "9782205049657", "9782205049658"],
        [
            ([], []),
            (["blacksad"], []),
            (["blacksad", "integrale"], ["chat detective"]),
            (["9782205049657"], ["blacksad"]),
        ],
        [
            "blacksad",
            "bd blacksad integrale",
            "chat detective",
            "blacksad poster",
            "blacksad poche",
            "9782205049657",
            "blacksad en castellano",
        ],
        [1000, 1600],
        [None, "very_good", "Bon état"],
        [None, "9782205049657", "9782205049658"],
        [None, "https://synthetic.invalid/9782205049657"],
    ):
        alert = SimpleNamespace(
            status=status,
            category=SimpleNamespace(slug=category),
            name="Blacksad",
            isbn=isbn,
            include_keywords=terms[0],
            variants=terms[1],
            exclude_keywords=["poche"],
            max_item_price_cents=1500,
            accepted_conditions=["Très bon état"],
        )
        r = evaluate_listing_against_alert(
            alert=alert,
            listing_category_slug=category,
            listing_normalized_title=title,
            listing_normalized_description="",
            listing_condition=condition,
            listing_item_price_cents=price,
            listing_isbn13=listing_code,
            listing_raw_url=raw_url,
        )
        outputs.append(
            [r.is_match, r.reason, r.keyword_match_score, r.condition_matches, r.price_matches]
        )
    encoded = json.dumps(outputs, ensure_ascii=False, separators=(",", ":")).encode()
    return len(outputs), hashlib.sha256(encoded).hexdigest()


def test_functional_outputs_are_identical_to_f05_base():
    assert functional_digest() == (
        18144,
        "3d14b12ea22fc1b4afca417ab460fb25695d9d382b2156da0bc32452fda81dbc",
    )

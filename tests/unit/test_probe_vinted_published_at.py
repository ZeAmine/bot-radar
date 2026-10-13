"""Helpers de la sonde Vinted — sans réseau, seules les fonctions pures."""

from scripts.probe_vinted_published_at import (
    date_like_keys,
    describe_item,
    dig,
    is_date_like,
    request_params,
    summarize_value,
)


def test_date_like_key_detection():
    """Les noms évocateurs sont retenus, les champs métier non."""
    assert is_date_like("created_at")
    assert is_date_like("published_at_ts")
    assert is_date_like("UpdatedAt")
    assert is_date_like("timestamp")
    assert is_date_like("ts")
    assert not is_date_like("title")
    assert not is_date_like("price")
    assert not is_date_like("photos")
    assert not is_date_like("results")
    assert not is_date_like(42)


def test_date_like_keys_walks_the_whole_tree():
    """La recherche descend dans les sous-objets, sans s'arrêter au premier niveau."""
    payload = {
        "id": 1,
        "title": "Tome",
        "item_box": {"second_line": "Neuf", "created_at": 1234567890},
    }

    assert date_like_keys(payload) == ["created_at"]
    assert date_like_keys({"a": 1}) == []
    assert date_like_keys(None) == []
    assert date_like_keys([1, 2]) == []


def test_describe_item_lists_nested_keys():
    """Les clés des sous-objets connus sont listées ; un item invalide reste vide."""
    item = {
        "id": 7,
        "url": "/items/7",
        "price": {"amount": "4.00"},
        "photos": [{"full_size": True}],
    }

    top, nested = describe_item(item)

    assert top == ["id", "photos", "price", "url"]
    assert nested == {"price": ["amount"], "photos[0]": ["full_size"]}
    assert describe_item("pas un dict") == ([], {})
    assert describe_item(None) == ([], {})


def test_dig_finds_a_nested_value():
    """La valeur d'une clé est retrouvée où qu'elle soit dans l'arbre."""
    payload = {"item_box": {"created_at": 123}}

    assert dig(payload, "created_at") == 123
    assert dig(payload, "ts") is None
    assert dig(3, "created_at") is None


def test_summarize_value_truncates():
    """Résumé court : de quoi juger, pas une charge complète."""
    assert summarize_value("court") == "court"
    assert summarize_value("x" * 100, limit=10) == "x" * 10 + "…"


def test_request_params_asks_for_a_single_item():
    """La sonde demande un seul item : une réponse, pas un flux."""
    params = request_params()

    assert params["per_page"] == "1"
    assert params["order"] == "newest_first"


def test_observed_catalogue_schema_exposes_no_publication_timestamp():
    """Schéma réellement observé le 2026-10-13 : aucune clé de date de publication.

    Les clés sont celles qu'a renvoyées `/svc-catalogue/items` sur un item réel. Le
    seul nom retenu par la détection est `show_1st_time_seller_discount`, un booléen
    — la chaîne « time » de « first time », pas un horodatage. Ce test garde le
    constat sous les yeux : si Vinted ajoute un jour un champ de date, il apparaîtra
    ici plutôt que dans une hypothèse.
    """
    observed = {
        "content_source": "catalog",
        "favourite_count": 0,
        "id": 10113230090,
        "is_favourite": False,
        "item_box": {
            "accessibility_label": "Marvel, État: Neuf sans étiquette, 4.00 €",
            "exposures": [],
            "ff_exposures": [],
            "first_line": "",
            "item_id": 10113230090,
            "second_line": "Neuf sans étiquette",
        },
        "photo": {"id": 1, "url": "https://images1.vinted.net/cover.webp"},
        "photos": [{"id": 1, "url": "https://images1.vinted.net/cover.webp"}],
        "price": {"amount": "4.00", "currency_code": "EUR"},
        "promoted": False,
        "search_tracking_params": {},
        "service_fee": "0.00",
        "show_1st_time_seller_discount": False,
        "title": "Tome",
        "total_item_price": "4.00",
        "url": "/items/1",
        "user": {"id": 1, "login": "vendeur"},
        "view_count": 12,
    }

    candidates = date_like_keys(observed)

    assert candidates == ["show_1st_time_seller_discount"]
    assert isinstance(observed[candidates[0]], bool)

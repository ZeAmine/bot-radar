"""Tests unitaires pour les fonctions de validation et parsing du service alertes."""

import pytest

from app.services.alert_service import (
    AlertServiceError,
    parse_comma_separated_list,
    parse_price_to_cents,
    validate_and_parse_vinted_url,
)


def test_parse_price_to_cents():
    assert parse_price_to_cents("15") == 1500
    assert parse_price_to_cents("15.50") == 1550
    assert parse_price_to_cents("15,50 €") == 1550
    assert parse_price_to_cents(" 9.99 ") == 999
    assert parse_price_to_cents("0") == 0

    with pytest.raises(AlertServiceError):
        parse_price_to_cents("-10")

    with pytest.raises(AlertServiceError):
        parse_price_to_cents("abc")


def test_parse_comma_separated_list():
    raw = "blacksad, intégrale, , tome 1, blacksad "
    parsed = parse_comma_separated_list(raw)
    assert parsed == ["blacksad", "intégrale", "tome 1"]

    assert parse_comma_separated_list("") == []
    assert parse_comma_separated_list("   ") == []


def test_validate_and_parse_vinted_url_valid():
    url = "https://www.vinted.fr/catalog?search_text=blacksad&price_to=15.50&catalog[]=12"
    result = validate_and_parse_vinted_url(url)

    assert result["url"] == url
    assert result["domain"] == "www.vinted.fr"
    assert result["search_text"] == "blacksad"
    assert result["price_to_cents"] == 1550
    assert "catalog[]" in result["all_params"]


def test_validate_and_parse_vinted_url_com_domain():
    url = "https://vinted.com/items?search_text=marvel"
    result = validate_and_parse_vinted_url(url)
    assert result["domain"] == "vinted.com"
    assert result["search_text"] == "marvel"
    assert result["price_to_cents"] is None


def test_validate_and_parse_vinted_url_http_rejected():
    url = "http://www.vinted.fr/catalog?search_text=blacksad"
    with pytest.raises(AlertServiceError, match="HTTPS"):
        validate_and_parse_vinted_url(url)


def test_validate_and_parse_vinted_url_wrong_domain_rejected():
    url = "https://www.leboncoin.fr/recherche?text=blacksad"
    with pytest.raises(AlertServiceError, match="vinted.fr ou vinted.com"):
        validate_and_parse_vinted_url(url)


def test_extract_clean_isbn():
    from app.bot.handlers.alerts import extract_clean_isbn

    assert extract_clean_isbn("9782803672080") == "9782803672080"
    assert extract_clean_isbn("978-2-8036-7208-0") == "9782803672080"
    assert (
        extract_clean_isbn("https://www.vinted.fr/catalog?search_text=9782803672080")
        == "9782803672080"
    )
    assert extract_clean_isbn("Soli Deo Gloria 978-2-8036-7208-0") == "9782803672080"
    assert extract_clean_isbn("2205089757") == "9782205089752"
    assert extract_clean_isbn("blacksad") is None
    assert extract_clean_isbn("") is None

"""Tests unitaires de la construction d'URL du collecteur Leboncoin."""

from app.services.leboncoin_scraper import DEFAULT_SEARCH_URL, _with_page


def test_page_one_keeps_url_untouched():
    assert _with_page(DEFAULT_SEARCH_URL, 1) == DEFAULT_SEARCH_URL


def test_page_two_preserves_genre_encoding():
    url = _with_page(DEFAULT_SEARCH_URL, 2)

    assert url.endswith("&page=2")
    assert "leisure_book_genre=comics%26mangas" in url
    assert "category=27" in url


def test_page_replaces_existing_parameter():
    url = _with_page(DEFAULT_SEARCH_URL + "&page=3", 2)

    assert url.count("page=") == 1
    assert url.endswith("&page=2")

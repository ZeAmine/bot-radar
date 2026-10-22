"""Contrats du canari de schéma Leboncoin, sans réseau."""

import importlib
import json
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "leboncoin" / "search_page.json"


def canary():
    return importlib.import_module("scripts.leboncoin_schema_canary")


def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def page(document: dict) -> str:
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(document)
        + "</script>"
    )


def test_fixture_is_clean():
    assert canary().validate_html(page(payload())) == []


def test_missing_next_data_is_reported():
    assert canary().validate_html("<html><body>JS requis</body></html>") == [
        "__NEXT_DATA__ absent"
    ]


def test_unreadable_next_data_is_reported():
    html = '<script id="__NEXT_DATA__">{pas du json}</script>'
    assert canary().validate_html(html) == ["__NEXT_DATA__ illisible"]


def test_empty_ads_is_reported():
    document = payload()
    document["props"]["pageProps"]["searchData"]["ads"] = []
    assert "ads absent ou vide" in canary().validate_html(page(document))


def test_missing_field_is_reported():
    document = payload()
    del document["props"]["pageProps"]["searchData"]["ads"][0]["subject"]
    problems = canary().validate_html(page(document))
    assert any("champs absents" in problem and "subject" in problem for problem in problems)


def test_changed_date_format_is_reported():
    document = payload()
    document["props"]["pageProps"]["searchData"]["ads"][0]["first_publication_date"] = (
        "2026-10-20T01:37:26Z"
    )
    assert "first_publication_date hors format" in canary().validate_html(page(document))


def test_price_type_change_is_reported():
    document = payload()
    document["props"]["pageProps"]["searchData"]["ads"][0]["price"] = "5"
    assert "price de type inattendu" in canary().validate_html(page(document))


def test_missing_condition_and_images_across_page_are_reported():
    document = payload()
    for ad in document["props"]["pageProps"]["searchData"]["ads"]:
        ad["attributes"] = []
        ad["images"] = {}
    problems = canary().validate_html(page(document))
    assert "attribut condition absent" in problems
    assert "images.urls absentes" in problems


def test_main_accepts_local_html(tmp_path, capsys):
    source = tmp_path / "page.html"
    source.write_text(page(payload()), encoding="utf-8")
    assert canary().main(["--html", str(source)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["problems"] == []

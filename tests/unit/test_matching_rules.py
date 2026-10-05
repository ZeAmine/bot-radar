"""Tests des règles de matching : extensions d'exclusions et déterminisme."""

from app.domain.rules.listing_match import expand_exclusions_multilingual


def test_expand_exclusions_keeps_input_order_first() -> None:
    """Les exclusions de l'alerte viennent en tête, dans l'ordre saisi."""
    assert expand_exclusions_multilingual(["roman", "poche"]) == ["roman", "poche"]


def test_expand_exclusions_appends_equivalents_deterministically() -> None:
    """L'extension ajoute les équivalents dans l'ordre du dictionnaire, sans doublon.

    L'ordre était auparavant le tirage d'un `set`, donc différent d'un démarrage à
    l'autre : le motif affiché à l'utilisateur changeait selon le process. La liste
    étant mémoïsée, un ordre non déterministe aurait été rejoué à l'identique pendant
    toute la durée de vie du process.
    """
    first = expand_exclusions_multilingual(["contrefacon"])
    second = expand_exclusions_multilingual(["contrefacon"])

    assert first == second
    assert first[0] == "contrefacon"
    assert "falso" in first
    assert "falschung" in first


def test_expand_exclusions_never_duplicates() -> None:
    """Une exclusion déjà présente en équivalent ne doit pas être dupliquée."""
    expanded = expand_exclusions_multilingual(["fake", "falso"])

    assert expanded.count("falso") == 1
    assert expanded.count("fake") == 1


def test_expand_exclusions_leaves_unknown_exclusions_untouched() -> None:
    """Une exclusion sans équivalent multilingue reste seule."""
    assert expand_exclusions_multilingual(["zzz"]) == ["zzz"]
    assert expand_exclusions_multilingual([]) == []
"""La règle du verdict de recherche : dire « personne ne l'a », ou se taire.

Un seul endroit tranche, et il est pur : la même règle juge le verdict d'une
recherche qui vient d'avoir lieu, et celui qu'une alerte porte en base. Ces tests
la pincent sur ses trois cas — les autorités ont répondu et ne connaissent pas
l'ouvrage, une autorité s'est tue, une notice a été trouvée — plus les deux
gardes qui empêchent d'annoncer un verdict qu'on n'a pas : personne n'a été
interrogé, ou l'alerte ne porte aucun verdict.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.rules.catalogue_search import is_catalogue_exhausted, is_uncatalogued


@dataclass(frozen=True)
class _Verdict:
    """Un verdict minimal : la règle ne lit que ces trois faits."""

    consulted: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    notices: tuple[object, ...] = ()


def test_every_authority_answered_with_nothing_is_a_verdict():
    """Toutes les autorités consultées ont répondu, aucune ne connaît l'ouvrage."""
    verdict = _Verdict(consulted=("bnf", "openlibrary", "google_books"))

    assert is_catalogue_exhausted(verdict) is True


def test_one_unavailable_authority_forbids_the_verdict():
    """Une autorité muette ne prouve rien : « personne ne le connaît » se tairait ici."""
    verdict = _Verdict(consulted=("bnf", "openlibrary"), unavailable=("openlibrary",))

    assert is_catalogue_exhausted(verdict) is False


def test_a_found_notice_ends_the_question():
    """Un ouvrage trouvé n'est pas un ouvrage introuvable, même avec un silence à côté."""
    verdict = _Verdict(
        consulted=("bnf", "openlibrary"),
        unavailable=("openlibrary",),
        notices=("notice",),
    )

    assert is_catalogue_exhausted(verdict) is False
    assert is_catalogue_exhausted(_Verdict(consulted=("bnf",), notices=("notice",))) is False


def test_nobody_was_asked_is_not_a_verdict():
    """Personne n'a cherché : le silence n'est pas « aucune source ne le connaît »."""
    assert is_catalogue_exhausted(_Verdict()) is False


def test_an_alert_carries_the_verdict_only_as_a_dated_releve():
    """La colonne porte la date du relevé ; un drapeau ou une absence ne valent rien."""

    class _Alert:
        book_search_exhausted_at = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)

    class _NeverSearched:
        book_search_exhausted_at = None

    class _WithoutVerdict:
        pass

    class _FlagNotDate:
        book_search_exhausted_at = True

    assert is_uncatalogued(_Alert()) is True
    assert is_uncatalogued(_NeverSearched()) is False
    assert is_uncatalogued(_WithoutVerdict()) is False
    assert is_uncatalogued(_FlagNotDate()) is False

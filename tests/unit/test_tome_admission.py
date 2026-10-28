"""Admission d'un tome : ce qui prouve sa parution, et ce qui l'écarte.

Les cas sont ceux de l'incident : `add « La Passe-miroir » de Christelle Dabos`
retenait quatre tomes dont deux jamais parus. Les notices réelles sont dans
`tests/fixtures/bnf/` ; les autres cas sont écrits à la main pour couvrir chaque
motif une fois.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.domain.rules.tome_admission import (
    TomeNotice,
    admit_tome,
    notice_from_bnf_record,
    parse_notice_date,
)
from app.services.book_metadata_service import parse_tome_notices

FIXTURES = Path(__file__).parent.parent / "fixtures" / "bnf"
TODAY = date(2026, 10, 27)

PASSE_MIROIR = "La Passe-miroir"
DABOS = "Christelle Dabos"


def notice(**overrides: object) -> TomeNotice:
    """Une notice par défaut conforme, que chaque test contredit sur un seul point."""
    base = TomeNotice(
        isbn13="9782075163743",
        title="Les fiancés de l'hiver",
        date_type="d",
        publication_date=date(2013, 9, 12),
        publication_year=2013,
        authors=("Dabos Christelle",),
        series_statement=PASSE_MIROIR,
        number=1,
        publisher="Gallimard jeunesse",
    )
    return replace(base, **overrides)


def verdict(notice_value: TomeNotice | None, **overrides: object):
    arguments: dict[str, object] = {
        "isbn13": "9782075163743",
        "notice": notice_value,
        "series_title": PASSE_MIROIR,
        "author_hint": DABOS,
        "number": 1,
        "today": TODAY,
    }
    arguments.update(overrides)
    return admit_tome(**arguments)


def test_a_proven_published_tome_is_admitted():
    admission = verdict(notice())

    assert admission.accepted
    assert admission.reason is None
    assert not admission.other_work


@pytest.mark.parametrize(
    ("isbn", "expected"),
    [
        ("978207516374", "invalid_isbn"),
        ("978207516374X", "invalid_isbn"),
        ("", "invalid_isbn"),
        (None, "invalid_isbn"),
    ],
)
def test_an_unreadable_identifier_proves_nothing(isbn: str | None, expected: str):
    assert verdict(notice(), isbn13=isbn).reason == expected


def test_an_isbn_without_notice_is_refused_by_name():
    """Un ISBN ne prouve pas qu'un livre est sorti : seule une notice le fait."""
    admission = verdict(None)

    assert admission.reason == "no_bnf_notice"
    assert not admission.other_work, "une notice absente n'accuse pas un autre ouvrage"


def test_an_unknown_date_type_never_becomes_a_publication_date():
    """`u` annonce une date « inconnue » : le millésime est là, la preuve non."""
    admission = verdict(notice(date_type="u", publication_date=None, publication_year=2026))

    assert admission.reason == "date_unproven"


def test_a_certain_date_in_the_future_is_a_tome_to_come_not_an_unproven_date():
    admission = verdict(notice(publication_date=date(2027, 3, 5), publication_year=2027))

    assert admission.reason == "not_published_yet"
    assert not admission.other_work


def test_a_notice_of_another_series_is_named_as_such():
    admission = verdict(notice(series_statement="Fétiche", title="Une autre histoire"))

    assert admission.reason == "series_mismatch"
    assert admission.other_work, "un autre ouvrage ne doit pas attendre une date pour partir"


def test_a_series_title_inside_the_notice_title_is_enough():
    """Beaucoup de notices n'ont pas de `225` : le titre porte alors la série."""
    admission = verdict(
        notice(series_statement=None, title="La passe-miroir les disparus du Clairdelune")
    )

    assert admission.accepted


def test_a_different_volume_number_is_inconsistent():
    admission = verdict(notice(number=3), number=1)

    assert admission.reason == "tome_number_inconsistent"
    assert admission.other_work


def test_an_unknown_notice_number_never_contradicts_the_requested_one():
    """Une notice sans numéro ne dit pas « autre tome » : elle ne dit rien."""
    assert verdict(notice(number=None), number=4).accepted


def test_another_author_is_refused_before_any_date_question():
    admission = verdict(notice(authors=("Vanyda",), date_type="u", publication_date=None))

    assert admission.reason == "author_mismatch"
    assert admission.other_work


def test_an_author_mention_in_any_order_matches():
    """La BNF écrit « Dabos, Christelle » : c'est la même personne que la saisie."""
    assert verdict(notice(authors=("Dabos, Christelle",))).accepted


def test_another_publishing_house_is_refused():
    admission = verdict(notice(publisher="Gallimard bande dessinée"), reference_publisher="Dupuis")

    assert admission.reason == "publisher_mismatch"
    assert not admission.other_work, "une autre édition du même tome reste un tome"


def test_editorial_noise_does_not_create_a_publisher_mismatch():
    """« Éditions Gallimard Jeunesse » et « Gallimard jeunesse » sont la même maison."""
    admission = verdict(
        notice(publisher="Éditions Gallimard Jeunesse"), reference_publisher="Gallimard jeunesse"
    )

    assert admission.accepted


def test_identity_checks_come_before_the_date_otherwise_the_reason_would_lie():
    """Un refus « date non prouvée » sur un autre ouvrage masquerait le vrai motif."""
    admission = verdict(notice(series_statement="Fétiche", date_type="u"))

    assert admission.reason == "series_mismatch"


def test_parse_notice_date_reads_the_type_and_the_day_written_before_it():
    stamp = parse_notice_date("20260206d2026")

    assert stamp.date_type == "d"
    assert stamp.publication_date == date(2026, 2, 6)
    assert stamp.publication_year == 2026


def test_parse_notice_date_keeps_an_unknown_type_as_unknown():
    stamp = parse_notice_date("20261013u2026")

    assert stamp.date_type == "u"
    assert stamp.publication_date == date(2026, 10, 13)


@pytest.mark.parametrize("code", [None, "", "d", "2026", 20260206])
def test_parse_notice_date_never_invents_a_date(code: object):
    stamp = parse_notice_date(code)  # type: ignore[arg-type]

    assert stamp.date_type is None
    assert stamp.publication_date is None


def test_notice_from_record_projects_only_what_it_proves():
    notice_value = notice_from_bnf_record({
        "isbn13": "9782075163743",
        "title": "Les fiancés de l'hiver",
        "date_code": "20260206d2026",
        "authors": ("Dabos Christelle",),
        "series_statement": PASSE_MIROIR,
        "number": 1,
        "publisher": "Gallimard bande dessinée",
    })

    assert notice_value is not None
    assert notice_value.certain_date == date(2026, 2, 6)


def test_notice_from_record_refuses_a_record_without_a_title():
    """Un enregistrement illisible rend `None`, que l'admission dit « pas de notice »."""
    assert notice_from_bnf_record({"isbn13": "9782075163743"}) is None


def _payload(isbn: str) -> bytes:
    return (FIXTURES / f"marcxchange_{isbn}.xml").read_bytes()


def test_the_passe_miroir_volume_one_keeps_only_its_published_edition():
    """Le cas de l'incident : deux notices pour le tome 1, une seule est parue.

    La première décrit l'adaptation en BD (parution non prouvée), la seconde
    l'album de 2026, dont la date est certaine. L'admission doit retenir la
    seconde et refuser l'autre pour son motif réel.
    """
    notices = parse_tome_notices(_payload("9782075163743"))

    assert len(notices) == 2
    verdicts = [
        admit_tome(
            isbn13="9782075163743",
            notice=notice_value,
            series_title=PASSE_MIROIR,
            author_hint=DABOS,
            number=1,
            today=TODAY,
        )
        for notice_value in notices
    ]

    # Une seule notice est admise, et c'est celle dont la date est certaine : le
    # classement de la BNF ne décide pas à la place de la preuve.
    assert sorted(str(admission.reason) for admission in verdicts) == [
        "None", "date_unproven",
    ]
    admitted = [
        notice_value
        for admission, notice_value in zip(verdicts, notices, strict=True)
        if admission.accepted
    ]
    assert [notice_value.certain_date for notice_value in admitted] == [date(2026, 2, 6)]


@pytest.mark.parametrize("isbn", ["9782075215510", "9782075215619"])
def test_the_unpublished_tomes_of_the_series_are_refused_by_their_date(isbn: str):
    """Tomes 2 et 4 : la BNF les annonce avec une date de type `u`, donc non prouvée."""
    notices = parse_tome_notices(_payload(isbn))

    assert notices, "la fixture doit contenir la notice qui a fait entrer le tome"
    admissions = [
        admit_tome(
            isbn13=isbn, notice=notice_value, series_title=PASSE_MIROIR,
            author_hint=DABOS, today=TODAY,
        )
        for notice_value in notices
    ]

    assert [admission.reason for admission in admissions] == ["date_unproven"] * len(notices)


def test_a_truncated_payload_renders_no_notice_rather_than_a_wrong_one():
    assert parse_tome_notices(b"<srw:searchRetrieveResponse") == []

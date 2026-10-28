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
    ADMITTED_DATE_TYPES,
    TomeAdmission,
    TomeNotice,
    admit_tome,
    notice_from_bnf_record,
    parse_catalogue_entry,
    parse_publication_field,
)
from app.services.book_metadata_service import parse_tome_notices
from tests.fakes_catalogue import marcxchange_notice, notices_from_marcxchange

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
        declared_year=2013,
        publication_date=date(2013, 9, 12),
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


def test_a_failed_verification_is_not_an_absent_notice():
    """La BNF muette ne dit pas que le livre n'existe pas : le motif doit le dire."""
    admission = TomeAdmission("bnf_unavailable")

    assert admission.reason == "bnf_unavailable"
    assert not admission.other_work, "on n'a pas pu regarder, on n'accuse pas l'ouvrage"


def test_an_isbn_without_notice_is_refused_by_name():
    """Un ISBN ne prouve pas qu'un livre est sorti : seule une notice le fait."""
    admission = verdict(None)

    assert admission.reason == "no_bnf_notice"
    assert not admission.other_work, "une notice absente n'accuse pas un autre ouvrage"


def test_an_unknown_date_type_never_becomes_a_publication_date():
    """`u` annonce une date « inconnue » : le millésime est là, la preuve non."""
    admission = verdict(notice(date_type="u", publication_date=None, declared_year=2026))

    assert admission.reason == "date_unproven"


def test_a_certain_date_in_the_future_is_a_tome_to_come_not_an_unproven_date():
    admission = verdict(notice(publication_date=date(2027, 3, 5), declared_year=2027))

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


def test_the_fixed_field_reads_three_positions_with_three_meanings():
    """0-7 : entrée au catalogue. 8 : type de date. 9-12 : Date 1, l'année déclarée."""
    entry = parse_catalogue_entry("20260206d2026    m  y0frey50      ba")

    assert entry.date_type == "d"
    assert entry.entry_date == date(2026, 2, 6)
    assert entry.declared_year == 2026


def test_the_catalogue_entry_day_is_never_a_publication_day():
    """La notice du tome annoncé est entrée au catalogue en juillet : le livre, non."""
    entry = parse_catalogue_entry("20260716u        m  y0frey50      ba")

    assert entry.entry_date == date(2026, 7, 16)
    assert entry.date_type == "u"
    assert entry.declared_year is None


@pytest.mark.parametrize("code", [None, "", "d", "2026", 20260206])
def test_the_fixed_field_never_invents_a_date(code: object):
    entry = parse_catalogue_entry(code)  # type: ignore[arg-type]

    assert entry.date_type is None
    assert entry.entry_date is None
    assert entry.declared_year is None


@pytest.mark.parametrize(
    ("written", "expected_day", "expected_year"),
    [
        ("DL 2026", None, 2026),
        ("2026", None, 2026),
        ("[2026]", None, 2026),
        ("DL 06/02/2026", date(2026, 2, 6), 2026),
        ("2026-02-06", date(2026, 2, 6), 2026),
        ("", None, None),
        (None, None, None),
    ],
)
def test_the_publication_field_gives_a_day_only_when_it_names_one(
    written: str | None, expected_day: date | None, expected_year: int | None
):
    assert parse_publication_field(written) == (expected_day, expected_year)


def test_notice_from_record_projects_only_what_it_proves():
    notice_value = notice_from_bnf_record({
        "isbn13": "9782075163743",
        "title": "Les fiancés de l'hiver",
        "date_code": "20260206d2026",
        "publication_field": "DL 2026",
        "authors": ("Dabos Christelle",),
        "series_statement": PASSE_MIROIR,
        "number": 1,
        "publisher": "Gallimard bande dessinée",
    })

    assert notice_value is not None
    assert notice_value.entry_date == date(2026, 2, 6)
    assert notice_value.proven_day is None, "aucun jour de parution n'est écrit ici"
    assert notice_value.proven_year == 2026


def _admission_of(date_code: str, *, publication_field: str | None = None,
                  today: date = TODAY):
    """Admission jugée sur la zone telle qu'elle est écrite, lue par le vrai parseur."""
    record = marcxchange_notice(
        isbn="9782075163743", title="Les fiancés de l'hiver", date_code=date_code,
        publisher="Gallimard bande dessinée", creator="Dabos Christelle",
        publication_field=publication_field, number=1, series=PASSE_MIROIR,
    )
    (notice_value,) = notices_from_marcxchange(record)
    admission = admit_tome(
        isbn13="9782075163743", notice=notice_value, series_title=PASSE_MIROIR,
        author_hint=DABOS, number=1, today=today,
    )
    return admission, notice_value


def test_a_notice_entered_fifteen_days_ago_for_a_book_due_next_year_is_not_a_tome_yet():
    """`20261013d2027` : catalogué le 13/10/2026, annoncé pour 2027.

    Lire les positions 0-7 comme une date de parution ferait entrer en librairie un
    livre qui n'y est pas — c'est exactement l'erreur corrigée ici.
    """
    admission, notice_value = _admission_of("20261013d2027    m  y0frey50      ba")

    assert notice_value.entry_date == date(2026, 10, 13)
    assert notice_value.proven_year == 2027
    assert admission.reason == "not_published_yet"


def test_an_entry_date_already_past_does_not_make_a_future_tome_published():
    admission, _ = _admission_of("20250815d2027    m  y0frey50      ba")

    assert admission.reason == "not_published_yet"


def test_the_reference_notice_is_admitted_on_its_proven_year_not_on_its_entry_day():
    admission, notice_value = _admission_of("20260206d2026    m  y0frey50      ba")

    assert admission.accepted
    assert notice_value.proven_day is None, "aucun jour de parution n'est prouvé"
    assert notice_value.proven_year == 2026


def test_a_real_publication_day_wins_over_the_year():
    admission, notice_value = _admission_of(
        "20260101u        m  y0frey50      ba", publication_field="DL 06/02/2026",
    )

    assert admission.accepted
    assert notice_value.proven_day == date(2026, 2, 6)
    assert notice_value.proven_year == 2026


def test_a_publication_day_in_the_future_is_not_published_yet():
    admission, _ = _admission_of(
        "20260101d2026    m  y0frey50      ba", publication_field="DL 06/02/2027",
    )

    assert admission.reason == "not_published_yet"


def test_a_declared_deposit_year_alone_never_proves_the_publication():
    """`DL 2026` sur une notice de type `u` : l'éditeur a déclaré, la BNF ne sait pas.

    C'est le cas des tomes 2 et 4 de « La Passe-miroir » : promouvoir cette année en
    preuve les ferait entrer dans l'alerte alors que rien ne dit qu'ils sont sortis.
    """
    admission, notice_value = _admission_of(
        "20260716u        m  y0frey50      ba", publication_field="DL 2026",
    )

    assert notice_value.declared_publication_year == 2026
    assert notice_value.proven_year is None
    assert admission.reason == "date_unproven"


def test_only_the_single_year_monograph_type_is_admitted():
    """L'ensemble admis est un choix : `d` seul affirme une année de parution."""
    assert frozenset({"d"}) == ADMITTED_DATE_TYPES


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
    assert [notice_value.proven_year for notice_value in admitted] == [2026]
    assert [notice_value.entry_date for notice_value in admitted] == [date(2026, 2, 6)]
    assert [notice_value.proven_day for notice_value in admitted] == [None]


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

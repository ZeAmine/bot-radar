"""Le numéro demandé et la langue de l'édition survivent au parcours d'ajout."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.handlers.alerts_helpers import build_creation_recap
from app.domain.enums import BookSearchStatus
from app.services.ai.modules.natural_add import natural_add_module
from app.services.book_metadata_service import BookMetadataService
from app.use_cases.book_search import BookSearchUseCase, metadata_from_result
from tests.add_fakes import DraftState
from tests.fakes_catalogue import BNF_RECORD_TEMPLATE, FakeCatalogueClient

VOLUME_4_ISBN = "9781444977387"
VOLUME_1_ISBN = "9782019467500"


def record(title, isbn=VOLUME_4_ISBN, language=None):
    xml = BNF_RECORD_TEMPLATE.format(
        ark="123456789",
        title=title,
        creator="Meddings, Lize",
        publisher="Hodder Children's Books",
        date="",
        fmt="240 p.",
        isbn=isbn,
    )
    if language:
        xml = xml.replace("</oai_dc:dc>", f"<dc:language>{language}</dc:language></oai_dc:dc>")
    return xml


def search_case(records):
    return BookSearchUseCase(
        BookMetadataService(
            http_client=FakeCatalogueClient(bnf_records=records, openlibrary_docs=[])
        )
    )


@pytest.mark.parametrize(
    "wrong_title",
    [
        "The Sad Ghost Club Volume 1",
        "The Sad Ghost Club Volume 3",
        "The Sad Ghost Club",
        "The Sad Ghost Club intégrale volumes 1 à 4",
        "The Sad Ghost Club lot volume 4",
        "The Sad Ghost Club collection complète volume 4",
        "The Sad Ghost Club volumes 1 à 4",
        "The Sad Ghost Club : collection complète Volume 4",
    ],
)
async def test_wrong_or_unproved_volume_is_never_confirmed(wrong_title):
    case = search_case([record(wrong_title, VOLUME_1_ISBN)])
    by_title = await case.search_by_title("The Sad Ghost Club Volume 4")
    by_isbn = await case.search_by_isbn(VOLUME_1_ISBN, "The Sad Ghost Club Volume 4")
    assert by_title.status != BookSearchStatus.FOUND
    assert by_isbn.status != BookSearchStatus.FOUND


async def test_subtitle_number_selects_volume_four_and_filters_other_editions():
    case = search_case(
        [
            record("The Sad Ghost Club : Volume 1", VOLUME_1_ISBN, "fre"),
            record("The Sad Ghost Club : Volume 4", language="eng"),
        ]
    )
    result = await case.search_by_title("The Sad Ghost Club Volume 4")
    assert result.status == BookSearchStatus.FOUND
    assert result.reference.isbn13 == VOLUME_4_ISBN
    assert [edition.isbn13 for edition in result.editions] == [VOLUME_4_ISBN]
    meta = metadata_from_result(result)
    assert getattr(meta, "volume_number", None) == 4
    assert getattr(result.reference, "language", None) == "en"
    assert getattr(meta, "language", None) == "en"


async def test_volume_number_in_the_middle_of_a_subtitle_is_preserved():
    case = search_case([record("The Sad Ghost Club : Volume 4, Find Your Kindred Spirits")])
    result = await case.search_by_title("The Sad Ghost Club Volume 4")
    assert result.status == BookSearchStatus.FOUND
    assert result.reference.isbn13 == VOLUME_4_ISBN
    assert result.confirmed.volume_number == 4


@pytest.mark.parametrize("marker", ["tome 4", "volume 4", "vol. 4", "t. 4"])
@pytest.mark.parametrize("ai_series", [False, True])
async def test_raw_volume_survives_ai_omission_and_series_flag(marker, ai_series):
    case = search_case([record("The Sad Ghost Club : Volume 4", language="eng")])
    client = MagicMock(is_configured=True)
    client.generate_json = AsyncMock(
        return_value={
            "name": "The Sad Ghost Club",
            "category_slug": "bd_comics",
            "is_series": ai_series,
            "max_price_cents": 1200,
        }
    )
    with (
        patch.object(natural_add_module, "client", client),
        patch("app.services.ai.modules.natural_add.BookSearchUseCase", return_value=case),
    ):
        parsed = await natural_add_module.parse_prompt(f"The Sad Ghost Club {marker}, 12€")
    assert parsed is not None
    assert parsed.isbn13 == VOLUME_4_ISBN
    assert parsed.name == "The Sad Ghost Club - Volume 4"
    assert parsed.is_series is False
    assert getattr(parsed, "requested_volume", None) == 4
    assert getattr(parsed.book_context, "language", None) == "en"


@pytest.mark.parametrize(
    ("language", "label"), [("eng", "Anglais"), ("fre", "Français"), (None, "Inconnue")]
)
async def test_recap_displays_edition_language(language, label):
    from app.bot.handlers.alerts_helpers import draft_updates_from_search

    result = await search_case(
        [record("The Sad Ghost Club Volume 4", language=language)]
    ).search_by_title("The Sad Ghost Club Volume 4")
    draft = draft_updates_from_search(result, query_title="The Sad Ghost Club Volume 4")
    text, keyboard = build_creation_recap(draft)
    assert f"<b>Langue :</b> {label}" in text
    assert "The Sad Ghost Club - Volume 4" in text
    assert any(
        b.callback_data == "alert_create_confirm" for row in keyboard.inline_keyboard for b in row
    )
    assert "uniquement en anglais" not in text
    assert "aucune édition française" not in text.lower()


async def test_unconfirmed_volume_recap_offers_edit_and_cancel_without_creation():
    from app.bot.handlers.alerts_helpers import draft_updates_from_search

    result = await search_case(
        [record("The Sad Ghost Club Volume 1", VOLUME_1_ISBN)]
    ).search_by_title("The Sad Ghost Club Volume 4")
    draft = draft_updates_from_search(result, query_title="The Sad Ghost Club Volume 4")
    text, keyboard = build_creation_recap(draft)
    callbacks = {b.callback_data for row in keyboard.inline_keyboard for b in row}
    assert "Le volume 4 demandé n’a pas pu être confirmé" in text
    assert "alert_create_confirm" not in callbacks
    assert {"alert_create_edit", "alert_create_cancel"} <= callbacks


async def test_stale_confirmation_does_not_write_an_unconfirmed_volume():
    from sqlalchemy import select

    from app.bot.handlers.alerts_create import save_alert
    from app.db.models.alert import Alert
    from tests.database import sqlite_only_database
    from tests.screens import confirm_press

    state = DraftState()
    state.data = {
        "name": "The Sad Ghost Club Volume 4",
        "category_slug": "bd_comics",
        "isbn": VOLUME_1_ISBN,
        "requested_volume": 4,
    }
    callback, message = confirm_press()
    async with sqlite_only_database() as database:
        await save_alert(callback, state, MagicMock(), database.session)
        assert (await database.session.execute(select(Alert))).scalars().all() == []
    assert state.data["requested_volume"] == 4
    assert "volume 4" in message.edit_text.call_args.args[0]


@pytest.mark.parametrize("language", ["eng", "fre", None])
async def test_edition_switch_uses_new_language_even_when_cached_choice_is_wrong(language):
    from app.bot.handlers.alerts_create_confirm import select_alert_creation_edition
    from tests.screens import confirm_press

    state = DraftState()
    state.data = {
        "name": "The Sad Ghost Club - Volume 4", "category_slug": "bd_comics",
        "requested_volume": 4, "isbn": VOLUME_1_ISBN, "language": "en",
        "edition_alternatives": [{"isbn13": VOLUME_4_ISBN, "language": "fr"}],
    }
    callback, message = confirm_press()
    callback.data = "al_create_edition:0"
    case = search_case([record("The Sad Ghost Club : Volume 4", language=language)])
    with patch("app.bot.handlers.alerts_create_confirm.BookSearchUseCase", return_value=case):
        await select_alert_creation_edition(callback, state)
    assert state.data["isbn"] == VOLUME_4_ISBN
    assert state.data["book_context"]["volume_number"] == 4
    label = {"eng": "Anglais", "fre": "Français", None: "Inconnue"}[language]
    assert f"<b>Langue :</b> {label}" in message.edit_text.call_args.args[0]


async def test_edition_switch_refuses_the_wrong_volume():
    from app.bot.handlers.alerts_create_confirm import select_alert_creation_edition
    from tests.screens import confirm_press

    state = DraftState()
    state.data = {
        "name": "The Sad Ghost Club - Volume 4", "category_slug": "bd_comics",
        "requested_volume": 4, "isbn": VOLUME_4_ISBN,
        "edition_alternatives": [{"isbn13": VOLUME_1_ISBN}],
    }
    callback, message = confirm_press()
    callback.data = "al_create_edition:0"
    case = search_case([record("The Sad Ghost Club Volume 1", VOLUME_1_ISBN)])
    with patch("app.bot.handlers.alerts_create_confirm.BookSearchUseCase", return_value=case):
        await select_alert_creation_edition(callback, state)
    assert state.data["isbn"] == VOLUME_4_ISBN
    message.edit_text.assert_not_awaited()


@pytest.mark.parametrize("edition_title", ["The Sad Ghost Club Volume 4", None])
async def test_openlibrary_requires_exact_isbn_notice_and_ignores_work_languages(edition_title):
    from tests.fakes_catalogue import OPENLIBRARY_DOC, FakeResponse

    class Client(FakeCatalogueClient):
        async def get(self, url, **kwargs):
            if f"/isbn/{VOLUME_4_ISBN}.json" in url:
                payload = {} if edition_title is None else {
                    "title": edition_title, "isbn_13": [VOLUME_4_ISBN],
                    "languages": [{"key": "/languages/eng"}],
                }
                return FakeResponse(payload=payload)
            return await super().get(url, **kwargs)

    doc = {**OPENLIBRARY_DOC, "title": "The Sad Ghost Club Volume 4",
           "isbn": [VOLUME_4_ISBN], "language": ["fre", "eng"]}
    case = BookSearchUseCase(BookMetadataService(
        http_client=Client(bnf_empty=True, openlibrary_docs=[doc])
    ))
    result = await case.search_by_title("The Sad Ghost Club Volume 4")
    if edition_title:
        assert result.status == BookSearchStatus.FOUND
        assert result.reference.language == "en"
    else:
        assert result.status != BookSearchStatus.FOUND

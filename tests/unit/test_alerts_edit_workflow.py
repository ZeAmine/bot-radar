"""Tests unitaires pour le workflow de modification épuré d'alertes (/edit)."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.handlers.alerts_manage import (
    handle_edit_condition_toggle,
    prompt_new_field_value,
    save_edited_field_value,
    select_edit_field,
)
from app.bot.keyboards.alerts import (
    get_cancel_edit_keyboard,
    get_edit_conditions_keyboard,
)
from app.core.constants import AlertStatus, CategorySlug
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.user import User


def test_get_cancel_edit_keyboard():
    alert_id = uuid.uuid4()
    kb = get_cancel_edit_keyboard(alert_id)
    assert len(kb.inline_keyboard) == 1
    assert kb.inline_keyboard[0][0].text == "Retour aux champs"
    assert kb.inline_keyboard[0][0].callback_data == f"al_edit:{alert_id}"


def test_get_edit_conditions_keyboard():
    alert_id = uuid.uuid4()

    # Quand accepted_conditions est None -> tous les états sont cochés
    kb_all = get_edit_conditions_keyboard(alert_id, None)
    for row in kb_all.inline_keyboard[:-2]:
        assert row[0].text.startswith("✅")

    # Quand seulement un état est sélectionné
    kb_sel = get_edit_conditions_keyboard(alert_id, ["Très bon état"])
    found_checked = False
    for row in kb_sel.inline_keyboard[:-2]:
        # Abrégé : « TBE » pour « Très bon état ».
        if row[0].text.endswith("TBE"):
            assert row[0].text.startswith("✅")
            found_checked = True
        else:
            assert row[0].text.startswith("⬜")
    assert found_checked is True


@pytest.mark.asyncio
async def test_select_edit_field_clears_state():
    callback = AsyncMock()
    callback.data = f"al_edit:{uuid.uuid4()}"
    callback.message = AsyncMock()
    state = AsyncMock()
    user = MagicMock(spec=User, id=uuid.uuid4())
    session = AsyncMock()

    mock_alert = MagicMock(
        spec=Alert,
        id=uuid.UUID(callback.data.split(":")[1]),
        name="Berserk",
        category=MagicMock(spec=Category, slug=CategorySlug.BD_COMICS.value, emoji="📚"),
    )
    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mock_alert

    from unittest.mock import patch

    with patch("app.bot.handlers.alerts_manage._get_alert_repo", return_value=repo_mock):
        await select_edit_field(callback, db_user=user, db_session=session, state=state)

    state.clear.assert_awaited_once()
    callback.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_prompt_new_field_value_shows_code_block_to_copy():
    alert_id = uuid.uuid4()
    callback = AsyncMock()
    callback.data = f"ed_f:{alert_id}:exclude_keywords"
    callback.message = AsyncMock()
    state = AsyncMock()
    user = MagicMock(spec=User, id=uuid.uuid4())
    session = AsyncMock()

    mock_alert = MagicMock(
        spec=Alert,
        id=alert_id,
        name="One Piece",
        exclude_keywords=["tome 1", "coffret"],
    )
    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mock_alert

    from unittest.mock import patch

    with patch("app.bot.handlers.alerts_manage._get_alert_repo", return_value=repo_mock):
        await prompt_new_field_value(callback, state=state, db_user=user, db_session=session)

    callback.message.edit_text.assert_awaited_once()
    call_args = callback.message.edit_text.call_args[0][0]
    # Doit contenir le bloc <code> cliquable pour copier en 1 clic
    assert "<code>tome 1, coffret</code>" in call_args
    assert "Valeur actuelle" in call_args
    state.set_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_prompt_new_field_value_scalar_shows_code_block():
    alert_id = uuid.uuid4()
    callback = AsyncMock()
    callback.data = f"ed_f:{alert_id}:max_price"
    callback.message = AsyncMock()
    state = AsyncMock()
    user = MagicMock(spec=User, id=uuid.uuid4())
    session = AsyncMock()

    mock_alert = MagicMock(
        spec=Alert,
        id=alert_id,
        name="One Piece",
        max_item_price_cents=1500,
    )
    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mock_alert

    from unittest.mock import patch

    with patch("app.bot.handlers.alerts_manage._get_alert_repo", return_value=repo_mock):
        await prompt_new_field_value(callback, state=state, db_user=user, db_session=session)

    callback.message.edit_text.assert_awaited_once()
    call_args = callback.message.edit_text.call_args[0][0]
    assert "<code>15</code>" in call_args


@pytest.mark.asyncio
async def test_save_edited_field_value_updates_list():
    alert_id = uuid.uuid4()
    message = AsyncMock()
    message.text = "tome 1, abimé"
    state = AsyncMock()
    state.get_data.return_value = {
        "edit_alert_id": str(alert_id),
        "edit_field": "exclude_keywords",
    }
    user = MagicMock(spec=User, id=uuid.uuid4())
    session = AsyncMock()

    mock_alert = MagicMock(
        spec=Alert,
        id=alert_id,
        status=AlertStatus.ACTIVE,
        category=None,
        max_item_price_cents=1500,
        exclude_keywords=["tome 1", "coffret"],
    )
    # `name=` passée à MagicMock nomme le mock, ce n'est pas l'attribut : le nettoyeur
    # de mots-clés lit le titre de l'alerte, il doit donc être posé explicitement.
    mock_alert.name = "Bleach"
    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mock_alert
    repo_mock.count_by_user.return_value = 1

    from unittest.mock import patch

    with patch("app.bot.handlers.alerts_manage._get_alert_repo", return_value=repo_mock):
        await save_edited_field_value(message, state=state, db_user=user, db_session=session)

    assert mock_alert.exclude_keywords == ["tome 1", "abimé"]
    session.commit.assert_awaited_once()
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_edited_field_value_keeps_an_exclusion_qualified_by_the_title():
    """Une exclusion saisie à la main et qualifiée par le titre doit survivre.

    « deep it coffret » écarte les intégrales d'un « Deep It » : c'est la forme
    normale d'un rejet, et c'est la seule façon d'exprimer ce refus. Le passage par
    l'écran « Modifier » est le point où elle se perdait, en silence.
    """
    alert_id = uuid.uuid4()
    message = AsyncMock()
    message.text = "deep it coffret, tome 1"
    state = AsyncMock()
    state.get_data.return_value = {
        "edit_alert_id": str(alert_id),
        "edit_field": "exclude_keywords",
    }
    user = MagicMock(spec=User, id=uuid.uuid4())
    session = AsyncMock()

    mock_alert = MagicMock(
        spec=Alert,
        id=alert_id,
        status=AlertStatus.ACTIVE,
        category=None,
        max_item_price_cents=1500,
        exclude_keywords=[],
    )
    mock_alert.name = "Deep It"
    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mock_alert
    repo_mock.count_by_user.return_value = 1

    from unittest.mock import patch

    with patch("app.bot.handlers.alerts_manage._get_alert_repo", return_value=repo_mock):
        await save_edited_field_value(message, state=state, db_user=user, db_session=session)

    assert mock_alert.exclude_keywords == ["deep it coffret", "tome 1"]
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_edited_field_value_skip_clears_list():
    alert_id = uuid.uuid4()
    message = AsyncMock()
    message.text = "/skip"
    state = AsyncMock()
    state.get_data.return_value = {
        "edit_alert_id": str(alert_id),
        "edit_field": "exclude_keywords",
    }
    user = MagicMock(spec=User, id=uuid.uuid4())
    session = AsyncMock()

    mock_alert = MagicMock(
        spec=Alert,
        id=alert_id,
        name="Dragon Ball",
        status=AlertStatus.ACTIVE,
        category=None,
        max_item_price_cents=1500,
        exclude_keywords=["coffret"],
    )
    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mock_alert
    repo_mock.count_by_user.return_value = 1

    from unittest.mock import patch

    with patch("app.bot.handlers.alerts_manage._get_alert_repo", return_value=repo_mock):
        await save_edited_field_value(message, state=state, db_user=user, db_session=session)

    assert mock_alert.exclude_keywords == []
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_edited_field_value_rejects_isbn_of_another_book():
    """Un ISBN qui désigne un autre ouvrage est refusé avec le vrai titre de l'ouvrage."""
    from unittest.mock import patch

    from app.services.book_metadata_service import BookMetadata

    alert_id = uuid.uuid4()
    message = AsyncMock()
    message.text = "9782369903062"
    state = AsyncMock()
    state.get_data.return_value = {
        "edit_alert_id": str(alert_id),
        "edit_field": "isbn",
    }
    user = MagicMock(spec=User, id=uuid.uuid4())
    session = AsyncMock()

    mock_alert = MagicMock(
        spec=Alert,
        id=alert_id,
        isbn=None,
        status=AlertStatus.ACTIVE,
        category=None,
        max_item_price_cents=1200,
        include_keywords=["La couleur des choses"],
    )
    mock_alert.name = "La couleur des choses"
    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mock_alert

    conflicting = BookMetadata(
        title="Keeping two", author="Jordan Crane", isbn13="9782369903062"
    )
    lookup_mock = AsyncMock(return_value=conflicting)

    with (
        patch("app.bot.handlers.alerts_manage._get_alert_repo", return_value=repo_mock),
        patch(
            "app.services.book_metadata_service.BookMetadataService.lookup_by_isbn",
            new=lookup_mock,
        ),
    ):
        await save_edited_field_value(message, state=state, db_user=user, db_session=session)

    assert mock_alert.isbn is None
    session.commit.assert_not_awaited()
    error_text = message.answer.call_args[0][0]
    assert "Keeping two" in error_text
    assert "La couleur des choses" in error_text


@pytest.mark.asyncio
async def test_save_edited_field_value_accepts_isbn_matching_alert():
    """Un ISBN confirmé pour le bon ouvrage est écrit et propagé."""
    from unittest.mock import patch

    alert_id = uuid.uuid4()
    message = AsyncMock()
    message.text = "978-2-36990-308-6"
    state = AsyncMock()
    state.get_data.return_value = {
        "edit_alert_id": str(alert_id),
        "edit_field": "isbn",
    }
    user = MagicMock(spec=User, id=uuid.uuid4())
    session = AsyncMock()

    mock_alert = MagicMock(
        spec=Alert,
        id=alert_id,
        isbn=None,
        status=AlertStatus.ACTIVE,
        category=None,
        max_item_price_cents=1200,
        include_keywords=["La couleur des choses"],
    )
    mock_alert.name = "La couleur des choses"
    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mock_alert
    repo_mock.count_by_user.return_value = 1

    from app.services.book_metadata_service import BookMetadata

    lookup_mock = AsyncMock(
        return_value=BookMetadata(
            title="La couleur des choses", author="Martin Panchaud", isbn13="9782369903086"
        )
    )

    with (
        patch("app.bot.handlers.alerts_manage._get_alert_repo", return_value=repo_mock),
        patch(
            "app.services.book_metadata_service.BookMetadataService.lookup_by_isbn",
            new=lookup_mock,
        ),
        patch("app.db.repositories.book_repo.BookRepository", return_value=AsyncMock()),
        patch(
            "app.db.repositories.market_price_repo.MarketPriceRepository",
            return_value=AsyncMock(),
        ),
    ):
        await save_edited_field_value(message, state=state, db_user=user, db_session=session)

    assert mock_alert.isbn == "9782369903086"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_edit_condition_toggle():
    alert_id = uuid.uuid4()
    callback = AsyncMock()
    callback.data = f"ed_c_tog:{alert_id}:0"
    callback.message = AsyncMock()
    user = MagicMock(spec=User, id=uuid.uuid4())
    session = AsyncMock()

    mock_alert = MagicMock(
        spec=Alert,
        id=alert_id,
        name="Naruto",
        accepted_conditions=None,  # Tous les états
    )
    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mock_alert

    from unittest.mock import patch

    with patch("app.bot.handlers.alerts_manage._get_alert_repo", return_value=repo_mock):
        await handle_edit_condition_toggle(callback, db_user=user, db_session=session)

    session.commit.assert_awaited_once()
    callback.answer.assert_awaited()

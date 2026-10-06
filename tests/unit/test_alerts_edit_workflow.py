"""Tests unitaires pour le workflow de modification épuré d'alertes (/edit)."""

import contextlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from app.services.book_metadata_service import BookMetadata


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
    tasks: list = []

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
        # L'alerte n'avait aucun ISBN : en poser un change d'ouvrage, donc la
        # recherche de mots-clés repart en arrière-plan (couverte par les parcours
        # dédiés plus bas).
        patch("app.bot.handlers.alerts_manage.safe_create_task", new=_capture_task(tasks)),
    ):
        await save_edited_field_value(message, state=state, db_user=user, db_session=session)

    assert mock_alert.isbn == "9782369903086"
    session.commit.assert_awaited_once()
    assert len(tasks) == 1
    for coro, _name in tasks:
        coro.close()


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


# ---------------------------------------------------------------------------------
# Parcours « Modifier → ISBN » : changer d'ouvrage rend ses mots-clés à l'alerte.
#
# Ces tests traversent les vrais handlers (`prompt_new_field_value` puis
# `save_edited_field_value`) et ne simulent que les frontières : la notice, le moteur,
# la base et la tâche de fond. Ils verrouillent trois cas — l'alerte portait déjà les
# mots-clés du moteur, une recherche était encore en vol quand l'ouvrage a changé une
# seconde fois, et le nouvel ISBN ne donne rien à personne.
# ---------------------------------------------------------------------------------

ANCIEN_ISBN = "9782369903086"
NOUVEL_ISBN = "9782369904151"
ANCIENNES_VARIANTES = ["couleurdeschose"]
ANCIENNES_EXCLUSIONS = ["coffret"]
NOUVELLES_VARIANTES = ["couleurdeschoses"]
NOUVELLES_EXCLUSIONS = ["keeping two"]


class _EditState:
    """Fausse FSM de l'écran « Modifier » : le champ ouvert et ce qui y a été saisi."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.state = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, data: dict | None = None, **kwargs) -> None:
        self.data.update(data or {})
        self.data.update(kwargs)

    async def set_state(self, state) -> None:
        self.state = state

    async def clear(self) -> None:
        self.data = {}
        self.state = None


class _BackgroundSession:
    """Session de la tâche de fond : seul l'écriture des mots-clés est observée."""

    def __init__(self) -> None:
        self.commit = AsyncMock()

    async def __aenter__(self) -> "_BackgroundSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _capture_task(tasks: list):
    """Remplace `safe_create_task` : la coroutine attend d'être jouée par le test."""

    def _run(coro, name=None):
        tasks.append((coro, name))
        return None

    return _run


async def _drain(tasks: list) -> None:
    """Joue la recherche de fond, comme le ferait la boucle d'événements."""
    for coro, _name in tasks:
        await coro
    tasks.clear()


@contextlib.contextmanager
def _frontieres(patches: list):
    """Active toutes les frontières simulées du parcours, et les rend à la fin."""
    with contextlib.ExitStack() as stack:
        for entered in patches:
            stack.enter_context(entered)
        yield


def _notice(title: str = "La couleur des choses", isbn13: str = NOUVEL_ISBN) -> BookMetadata:
    """Notice que les autorités rendent pour le nouvel ISBN."""
    return BookMetadata(
        title=title,
        author="Martin Panchaud",
        publisher="Editions çà et là",
        isbn13=isbn13,
    )


def _isbn_journey(
    *,
    isbn: str | None,
    variants: list[str],
    exclusions: list[str],
    notice: BookMetadata | None,
    outcome: KeywordVariantsOutcome,
) -> dict:
    """Une alerte telle qu'elle est en base, et les frontières du parcours d'édition.

    L'alerte est le vrai objet que le handler modifie : ses `variants` et
    `exclude_keywords` sont donc ce qui sera persisté, pas une copie de test.
    """
    alert = MagicMock(
        spec=Alert,
        id=uuid.uuid4(),
        isbn=isbn,
        status=AlertStatus.ACTIVE,
        category=MagicMock(spec=Category, slug=CategorySlug.BD_COMICS.value, emoji="📚"),
        max_item_price_cents=1200,
        include_keywords=["La couleur des choses"],
        exclude_keywords=list(exclusions),
        variants=list(variants),
        vinted_search_url="https://www.vinted.fr/catalog?search_text=ancien",
        accepted_conditions=None,
    )
    # `name=` en argument de `MagicMock` nomme le mock, ce n'est pas l'attribut : le
    # titre de l'alerte est lu par le nettoyeur de mots-clés et par la carte.
    alert.name = "La couleur des choses"
    alert_repo = AsyncMock()
    alert_repo.get_by_id.return_value = alert
    alert_repo.count_by_user.return_value = 1

    book_repo = AsyncMock()
    book_repo.get_by_isbn13.return_value = MagicMock(id=uuid.uuid4())

    words = AsyncMock(return_value=outcome)
    tasks: list = []

    card_message = AsyncMock()
    message = AsyncMock()
    message.answer = AsyncMock(return_value=card_message)

    state = _EditState()
    user = MagicMock(spec=User, id=uuid.uuid4())

    return {
        "alert": alert,
        "words": words,
        "tasks": tasks,
        "state": state,
        "user": user,
        "message": message,
        "card_message": card_message,
        "db_session": AsyncMock(),
        "patches": [
            patch(
                "app.bot.handlers.alerts_manage._get_alert_repo", return_value=alert_repo
            ),
            patch(
                "app.bot.handlers.alerts_manage.AsyncSessionLocal",
                return_value=_BackgroundSession(),
            ),
            patch(
                "app.bot.handlers.alerts_manage.safe_create_task",
                new=_capture_task(tasks),
            ),
            patch("app.bot.handlers.alerts_manage.schedule_book_pricing"),
            patch(
                "app.bot.handlers.alerts_manage.keyword_variants_module."
                "generate_variants_and_exclusions",
                new=words,
            ),
            patch(
                "app.services.book_metadata_service.BookMetadataService.lookup_by_isbn",
                new=AsyncMock(return_value=notice),
            ),
            patch(
                "app.db.repositories.book_repo.BookRepository", return_value=book_repo
            ),
        ],
    }


async def _open_isbn_field(journey: dict) -> None:
    """Ouvre le champ ISBN par le bouton « Modifier », comme le vrai clavier."""
    callback = MagicMock()
    callback.data = f"ed_f:{journey['alert'].id}:isbn"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    await prompt_new_field_value(
        callback,
        state=journey["state"],
        db_user=journey["user"],
        db_session=journey["db_session"],
    )
    assert journey["state"].data["edit_field"] == "isbn"


async def _type_isbn(journey: dict, isbn: str) -> None:
    """Enregistre la valeur saisie dans le champ ISBN ouvert."""
    journey["message"].text = isbn
    await save_edited_field_value(
        journey["message"],
        state=journey["state"],
        db_user=journey["user"],
        db_session=journey["db_session"],
    )


@pytest.mark.asyncio
async def test_changing_the_isbn_from_edit_replaces_the_previous_works_keywords():
    """L'alerte gardait les variantes de l'ouvrage précédent : la recherche les remplace.

    L'ancien ISBN avait laissé ses variantes et ses exclusions, raisonnées sur sa fiche.
    Changer d'ouvrage les invalide : elles partent avec lui dans le même commit, et le
    moteur en propose d'autres pour le nouvel ouvrage — celui-ci, pas l'ancien.
    """
    journey = _isbn_journey(
        isbn=ANCIEN_ISBN,
        variants=ANCIENNES_VARIANTES,
        exclusions=ANCIENNES_EXCLUSIONS,
        notice=_notice(),
        outcome=KeywordVariantsOutcome(
            variants=NOUVELLES_VARIANTES,
            exclusions=NOUVELLES_EXCLUSIONS,
            answered=True,
        ),
    )

    with _frontieres(journey["patches"]):
        await _open_isbn_field(journey)
        await _type_isbn(journey, NOUVEL_ISBN)

        # Les mots-clés de l'ancien ouvrage ne survivent pas à l'enregistrement.
        assert journey["alert"].isbn == NOUVEL_ISBN
        assert journey["alert"].variants == []
        assert journey["alert"].exclude_keywords == []

        await _drain(journey["tasks"])

    # Ceux du nouvel ouvrage sont écrits, à partir de sa fiche : la recherche a été
    # posée sur l'ISBN affiché, pas sur celui qui vient d'être remplacé.
    assert journey["alert"].variants == NOUVELLES_VARIANTES
    assert journey["alert"].exclude_keywords == NOUVELLES_EXCLUSIONS
    args, kwargs = journey["words"].await_args
    assert args[0] == "La couleur des choses"
    assert kwargs["category_slug"] == CategorySlug.BD_COMICS.value
    assert kwargs["context"].isbn13 == NOUVEL_ISBN
    assert kwargs["context"].author == "Martin Panchaud"
    # Le moteur a répondu : il n'y a rien à expliquer, et donc rien à envoyer.
    journey["card_message"].answer.assert_not_called()


@pytest.mark.asyncio
async def test_a_search_in_flight_never_lands_on_a_work_that_changed_again():
    """Pendant la recherche, l'ouvrage change encore : la réponse ne s'écrit pas.

    L'utilisateur peut corriger l'ISBN deux fois de suite. La première réponse décrit
    alors un livre que l'alerte ne surveille plus : l'écrire écraserait les mots-clés du
    second — et l'alerte matcherait sur le premier.
    """
    journey = _isbn_journey(
        isbn=ANCIEN_ISBN,
        variants=ANCIENNES_VARIANTES,
        exclusions=ANCIENNES_EXCLUSIONS,
        notice=_notice(),
        outcome=KeywordVariantsOutcome(
            variants=NOUVELLES_VARIANTES,
            exclusions=NOUVELLES_EXCLUSIONS,
            answered=True,
        ),
    )

    with _frontieres(journey["patches"]):
        await _open_isbn_field(journey)
        await _type_isbn(journey, NOUVEL_ISBN)
        # Second changement d'ouvrage, avant que la première recherche ait répondu.
        journey["alert"].isbn = "9782070413775"

        await _drain(journey["tasks"])

    assert NOUVELLES_VARIANTES[0] not in journey["alert"].variants
    assert journey["alert"].exclude_keywords == []
    journey["card_message"].answer.assert_not_called()


@pytest.mark.asyncio
async def test_the_variants_left_awaiting_confirmation_do_not_survive_the_change():
    """Les variantes proposées mais pas encore validées portaient sur l'ancien ouvrage.

    La proposition attend un clic. Le changement d'ISBN la rend caduque : le bouton doit
    répondre qu'elle a expiré plutôt que d'écrire les variantes du livre précédent sur le
    nouveau.
    """
    from app.bot.handlers.autotune import get_pending_variants, save_pending_variants

    journey = _isbn_journey(
        isbn=ANCIEN_ISBN,
        variants=ANCIENNES_VARIANTES,
        exclusions=ANCIENNES_EXCLUSIONS,
        notice=_notice(),
        outcome=KeywordVariantsOutcome(
            variants=NOUVELLES_VARIANTES,
            exclusions=NOUVELLES_EXCLUSIONS,
            answered=True,
        ),
    )
    save_pending_variants(journey["alert"].id, ANCIENNES_VARIANTES)

    with _frontieres(journey["patches"]):
        await _open_isbn_field(journey)
        await _type_isbn(journey, NOUVEL_ISBN)

    assert get_pending_variants(journey["alert"].id) == []


@pytest.mark.asyncio
async def test_a_new_isbn_that_nobody_knows_leaves_the_alert_mute_and_says_so():
    """Le nouvel ISBN n'est dans aucun catalogue : le moteur cherche sur le titre.

    L'alerte garde l'ISBN saisi — personne ne prouve qu'il désigne un autre ouvrage —,
    elle perd les mots-clés de l'ancien, et le silence du moteur se dit au lieu de se
    lire comme un avis rendu (ADR-005).
    """
    journey = _isbn_journey(
        isbn=ANCIEN_ISBN,
        variants=ANCIENNES_VARIANTES,
        exclusions=ANCIENNES_EXCLUSIONS,
        notice=None,
        outcome=KeywordVariantsOutcome.unanswered(),
    )

    with _frontieres(journey["patches"]):
        await _open_isbn_field(journey)
        await _type_isbn(journey, NOUVEL_ISBN)
        await _drain(journey["tasks"])

    assert journey["alert"].isbn == NOUVEL_ISBN
    assert journey["alert"].variants == []
    assert journey["alert"].exclude_keywords == []
    # Aucune fiche n'est disponible : la recherche part quand même, sur le titre.
    args, kwargs = journey["words"].await_args
    assert args[0] == "La couleur des choses"
    assert kwargs["context"] is None
    note = journey["card_message"].answer.call_args[0][0]
    assert note.startswith("<i>")
    assert note.endswith("</i>")
    assert "L'IA n'a pas répondu après une relance" in note
    assert "aucune variante ni exclusion n'a été ajoutée" in note


@pytest.mark.asyncio
async def test_re_entering_the_same_isbn_leaves_the_keywords_alone():
    """Ressaisir le même ISBN ne change pas d'ouvrage : rien n'est jeté, rien ne repart.

    Le déclencheur est le **changement** d'ouvrage, pas l'enregistrement du champ :
    corriger un prix ou reconfirmer le même ISBN effacerait sinon des mots-clés qui
    décrivent toujours l'ouvrage surveillé.
    """
    journey = _isbn_journey(
        isbn=ANCIEN_ISBN,
        variants=ANCIENNES_VARIANTES,
        exclusions=ANCIENNES_EXCLUSIONS,
        notice=_notice(isbn13=ANCIEN_ISBN),
        outcome=KeywordVariantsOutcome(
            variants=NOUVELLES_VARIANTES,
            exclusions=NOUVELLES_EXCLUSIONS,
            answered=True,
        ),
    )

    with _frontieres(journey["patches"]):
        await _open_isbn_field(journey)
        await _type_isbn(journey, ANCIEN_ISBN)

    assert journey["alert"].variants == ANCIENNES_VARIANTES
    assert journey["alert"].exclude_keywords == ANCIENNES_EXCLUSIONS
    assert journey["tasks"] == []
    journey["words"].assert_not_awaited()

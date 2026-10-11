"""Tests unitaires pour la création d'alertes multi-catégories (/add)."""

import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aiogram.types import InlineKeyboardMarkup, Message

from app.bot.handlers.alerts import (
    back_to_recap,
    edit_alert_creation_prompt,
    handle_category_selected,
    handle_query_input,
    prompt_new_price,
    save_alert,
    start_add_alert,
)
from app.bot.handlers.alerts_helpers import RECAP_TITLE
from app.bot.states.alert_creation import AlertCreationState
from app.core.constants import (
    DEFAULT_GAMING_CONDITIONS,
    DEFAULT_GAMING_EXCLUSIONS,
    DEFAULT_TECH_CONDITIONS,
    DEFAULT_TECH_EXCLUSIONS,
    AlertStatus,
)
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.user import User
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome


@pytest.mark.asyncio
async def test_start_add_alert_shows_category_keyboard():
    """Vérifie que /add sans argument propose le clavier de choix de catégorie."""
    message = MagicMock()
    message.text = "/add"
    message.answer = AsyncMock()

    state = AsyncMock()
    state.clear = AsyncMock()
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    await start_add_alert(message, state, db_session, db_user)

    state.clear.assert_called_once()
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_category)
    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert "Choisis la catégorie" in args[0]
    keyboard = kwargs["reply_markup"]
    token = state.update_data.await_args.kwargs["creation_token"]
    assert len(token) == 12
    assert all(btn[0].callback_data.endswith(f"@{token}") for btn in keyboard.inline_keyboard)
    callbacks = [btn[0].callback_data.partition("@")[0] for btn in keyboard.inline_keyboard]
    assert "add_cat:bd_comics" in callbacks
    assert "add_cat:tech" in callbacks
    assert "add_cat:gaming" in callbacks
    assert "add_cat:clothes" in callbacks


@pytest.mark.asyncio
async def test_handle_category_selected_tech():
    """Vérifie que la sélection 'tech' configure l'état et demande le produit."""
    callback = MagicMock()
    callback.data = "add_cat:tech"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state_data = {}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_get_data():
        return state_data

    state = AsyncMock()
    state.get_data = mock_get_data
    state.update_data = mock_update_data
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    await handle_category_selected(callback, state, db_session, db_user)

    assert state_data.get("category_slug") == "tech"
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_query)
    callback.message.edit_text.assert_called_once()
    assert "Électronique" in callback.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_category_selected_gaming():
    """Vérifie que la sélection 'gaming' configure l'état et demande le jeu / console."""
    callback = MagicMock()
    callback.data = "add_cat:gaming"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state_data = {}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_get_data():
        return state_data

    state = AsyncMock()
    state.get_data = mock_get_data
    state.update_data = mock_update_data
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    await handle_category_selected(callback, state, db_session, db_user)

    assert state_data.get("category_slug") == "gaming"
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_query)
    callback.message.edit_text.assert_called_once()
    assert "Jeux Vidéo" in callback.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_gaming_query_input_sets_defaults():
    """Vérifie que la saisie d'un jeu configure mots-clés, exclusions et conditions gaming."""
    message = MagicMock()
    message.text = "Mario Kart 8 Deluxe Switch"
    message.answer = AsyncMock()

    state_data = {"category_slug": "gaming"}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_get_data():
        return state_data

    state = AsyncMock()
    state.get_data = mock_get_data
    state.update_data = mock_update_data
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    cat_gaming = Category(id=uuid.uuid4(), slug="gaming", label="Jeux Vidéo", emoji="🎮")
    mock_cat_repo = MagicMock()
    mock_cat_repo.get_by_slug = AsyncMock(return_value=cat_gaming)

    mock_alert_repo = MagicMock()
    mock_alert_repo.find_existing = AsyncMock(return_value=None)

    with (
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=mock_cat_repo),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=mock_alert_repo),
    ):
        await handle_query_input(message, state, db_session, db_user)

    assert state_data["name"] == "Mario Kart 8 Deluxe Switch"
    assert "mario" in [k.lower() for k in state_data["include_keywords"]]
    assert "kart" in [k.lower() for k in state_data["include_keywords"]]
    assert "8" in [k.lower() for k in state_data["include_keywords"]]
    assert state_data["exclude_keywords"] == DEFAULT_GAMING_EXCLUSIONS
    assert state_data["selected_conditions"] == DEFAULT_GAMING_CONDITIONS
    assert state_data["isbn"] is None
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_max_price)
    message.answer.assert_called_once()
    assert "Mario Kart 8 Deluxe Switch" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_tech_query_input_sets_defaults():
    """Vérifie que la saisie tech configure mots-clés, exclusions et conditions."""
    message = MagicMock()
    message.text = "Raspberry Pi 4"
    message.answer = AsyncMock()

    state_data = {"category_slug": "tech"}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_get_data():
        return state_data

    state = AsyncMock()
    state.get_data = mock_get_data
    state.update_data = mock_update_data
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    cat_tech = Category(id=uuid.uuid4(), slug="tech", label="Électronique", emoji="💻")
    mock_cat_repo = MagicMock()
    mock_cat_repo.get_by_slug = AsyncMock(return_value=cat_tech)

    mock_alert_repo = MagicMock()
    mock_alert_repo.find_existing = AsyncMock(return_value=None)

    with (
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=mock_cat_repo),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=mock_alert_repo),
    ):
        await handle_query_input(message, state, db_session, db_user)

    assert state_data["name"] == "Raspberry Pi 4"
    assert "raspberry" in [k.lower() for k in state_data["include_keywords"]]
    assert "pi" in [k.lower() for k in state_data["include_keywords"]]
    assert "4" in [k.lower() for k in state_data["include_keywords"]]
    assert state_data["exclude_keywords"] == DEFAULT_TECH_EXCLUSIONS
    assert state_data["selected_conditions"] == DEFAULT_TECH_CONDITIONS
    assert state_data["isbn"] is None
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_max_price)
    message.answer.assert_called_once()
    assert "Raspberry Pi 4" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_save_alert_tech_creates_alert_without_books():
    """Vérifie que la sauvegarde d'une alerte tech n'appelle pas le catalogue de livres."""
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    cat_tech = Category(id=uuid.uuid4(), slug="tech", label="Électronique", emoji="💻")
    user_id = uuid.uuid4()
    db_user = User(id=user_id, telegram_user_id=123)
    db_session = AsyncMock()
    db_session.commit = AsyncMock()

    state_data = {
        "category_id": str(cat_tech.id),
        "category_slug": "tech",
        "name": "Raspberry Pi 4",
        "isbn": None,
        "include_keywords": ["raspberry", "pi", "4"],
        "exclude_keywords": list(DEFAULT_TECH_EXCLUSIONS),
        "variants": [],
        "max_item_price_cents": 5000,
        "selected_conditions": list(DEFAULT_TECH_CONDITIONS),
    }

    state = AsyncMock()
    state.get_data = AsyncMock(return_value=state_data)
    state.clear = AsyncMock()

    mock_alert = MagicMock()
    mock_alert.id = uuid.uuid4()
    mock_alert.name = "Raspberry Pi 4"
    mock_alert.status = AlertStatus.ACTIVE
    mock_alert.category = cat_tech

    mock_alert_repo = MagicMock()
    mock_alert_repo.create = AsyncMock(return_value=mock_alert)

    mock_cat_repo = MagicMock()
    mock_cat_repo.get_by_id = AsyncMock(return_value=cat_tech)
    mock_cat_repo.get_by_slug = AsyncMock(return_value=cat_tech)

    mock_book_repo = MagicMock()

    with (
        patch("app.bot.handlers.alerts.AlertRepository", return_value=mock_alert_repo),
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=mock_cat_repo),
        patch("app.bot.handlers.alerts.BookRepository", return_value=mock_book_repo),
    ):
        await save_alert(callback, state, db_user, db_session)

    # Vérifications
    mock_alert_repo.create.assert_called_once()
    create_kwargs = mock_alert_repo.create.call_args[1]
    assert create_kwargs["name"] == "Raspberry Pi 4"
    assert create_kwargs["isbn"] is None
    assert create_kwargs["max_item_price_cents"] == 5000
    assert "search_text=Raspberry+Pi+4" in create_kwargs["vinted_search_url"]

    # BookRepository ne doit JAMAIS être appelé pour un produit tech
    mock_book_repo.create.assert_not_called()
    mock_book_repo.link_alert_to_book.assert_not_called()

    # Le message de confirmation est la carte de récap relue, avec son propre en-tête :
    # mêmes libellés de champ, et aucune ligne ISBN — un produit n'a pas d'identifiant à
    # trouver, sa catégorie le dit. Le bandeau de catégorie du message précédent, lui,
    # n'appartient pas à la carte.
    callback.message.edit_text.assert_called_once()
    conf_text = callback.message.edit_text.call_args[0][0]
    assert "Alerte créée" in conf_text
    assert "<b>Titre :</b> Raspberry Pi 4" in conf_text
    assert "50,00 €" in conf_text
    assert "<b>ISBN :</b>" not in conf_text
    assert "(Électronique)" not in conf_text


@pytest.mark.asyncio
async def test_edit_alert_creation_prompt_and_sub_options():
    """Vérifie que le menu Modifier présente les options et que les sous-options marchent."""
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state_data = {
        "name": "Veste Dickies",
        "max_item_price_cents": 1000,
        "category_slug": "clothes",
        "include_keywords": ["Veste", "Dickies"],
        "exclude_keywords": [],
        "selected_conditions": ["Très bon état"],
    }
    state = AsyncMock()
    state.get_data = AsyncMock(return_value=state_data)
    state.set_state = AsyncMock()
    state.clear = AsyncMock()

    # 1. Test du menu Modifier
    await edit_alert_creation_prompt(callback, state)
    callback.answer.assert_called_once()
    callback.message.edit_text.assert_called_once()
    call_args = callback.message.edit_text.call_args
    prompt_text, kwargs = call_args[0][0], call_args[1]
    assert "Modifier l'alerte" in prompt_text
    kb = kwargs["reply_markup"]
    actions = [btn[0].callback_data for btn in kb.inline_keyboard]
    assert "al_create_edit:name" in actions
    assert "al_create_edit:price" in actions
    assert "al_create_edit:include_keywords" in actions
    assert "al_create_edit:exclude_keywords" in actions
    assert "al_create_edit:variants" in actions
    assert "al_create_edit:conditions" in actions
    assert "al_create_edit:back" in actions
    # Le bouton « Recommencer » a disparu avec son handler : aucun callback orphelin.
    assert not any("restart" in action for action in actions)

    # Les libellés sont nus : rien entre parenthèses, la valeur courante ne tronque
    # plus le nom du champ. Le clavier réel est inspecté, pas une constante.
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "(" not in "".join(labels)
    assert "Prix maximum" in labels
    assert "Retour au récapitulatif" in labels

    # 2. Test de la sous-option modification de prix
    callback.answer.reset_mock()
    callback.message.edit_text.reset_mock()
    callback.data = "al_create_edit:price"
    await prompt_new_price(callback, state)
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_edit_field)
    assert "Prix maximum" in callback.message.edit_text.call_args[0][0]

    # 3. Test de la sous-option retour au récapitulatif
    callback.answer.reset_mock()
    callback.message.edit_text.reset_mock()
    state.set_state.reset_mock()
    await back_to_recap(callback, state)
    state.set_state.assert_called_once_with(AlertCreationState.confirm_creation)
    assert "Veste Dickies" in callback.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_edit_menu_labels_are_bare_for_a_bd_with_an_isbn():
    """Le cas de l'écran réel — ISBN et prix renseignés : les libellés restent nus.

    L'écran affichait « ISBN (9781799510871) » et « Prix maximum (10,00 €) » : la
    parenthèse tronquait le nom du champ sans rien apprendre de plus. Le clavier
    réellement rendu est inspecté, libellé par libellé.
    """
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state = AsyncMock()
    state.get_data = AsyncMock(
        return_value={
            "name": "Absolute Martian Manhunter",
            "category_slug": "bd_comics",
            "isbn": "9781799510871",
            "max_item_price_cents": 1000,
            "include_keywords": ["absolute martian manhunter"],
            "exclude_keywords": [],
            "selected_conditions": ["Très bon état"],
        }
    )

    await edit_alert_creation_prompt(callback, state)

    kb = callback.message.edit_text.call_args[1]["reply_markup"]
    buttons = [button for row in kb.inline_keyboard for button in row]
    assert [button.text for button in buttons] == [
        "Nom / Titre",
        "ISBN",
        "Prix maximum",
        "Mots-clés inclus",
        "Mots-clés exclus",
        "Variantes",
        "États acceptés",
        "Retour au récapitulatif",
    ]
    # Ni la valeur de l'ISBN ni celle du prix ne débordent dans un libellé.
    assert "9781799510871" not in str(kb.inline_keyboard)
    assert "10,00 €" not in str(kb.inline_keyboard)
    # Aucun callback orphelin : le bouton supprimé ne survit pas dans le clavier.
    assert not any(
        "restart" in (button.callback_data or "") for button in buttons
    )


class MockMessage(MagicMock):
    __class__ = Message


class DraftState:
    """Fausse FSM fidèle à aiogram 3 : `update_data(data=...)` fusionne au premier niveau.

    Le brouillon doit être relu tel qu'il a été écrit : la recherche des variantes
    s'en sert pour vérifier qu'elle travaille encore sur l'ouvrage affiché.
    """

    def __init__(self) -> None:
        self.data: dict = {}
        self.state = None

    async def clear(self) -> None:
        self.data = {}
        self.state = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, data=None, **kwargs) -> None:
        if data:
            kwargs.update(data)
        self.data.update(kwargs)


def _defer_task(holder: list[tuple[Any, str | None]]) -> Any:
    """Remplace `safe_create_task` : retient la coroutine pour que le test la pilote.

    Le vrai `safe_create_task` est synchrone et confie la coroutine à la boucle
    d'événements. Le test garde la main : il prouve que le handler rend la main,
    puis exécute la recherche quand il le décide.
    """

    def _run(coro, name=None):
        holder.append((coro, name))
        return None

    return _run


async def _drain(holder: list[tuple[Any, str | None]]) -> None:
    """Exécute la recherche de fond retenue, comme le ferait la boucle d'événements."""
    for coro, _name in holder:
        await coro
    holder.clear()


@contextlib.contextmanager
def _natural_add_patches(parsed, outcome, existing_alert=None):
    """Frontières du `/add` naturel : parsing, variantes, dépôt, tâche de fond.

    Le dépôt est simulé par son dépôt d'alertes : c'est la frontière que le jalon
    consulte pour savoir si l'ouvrage est déjà surveillé. `existing_alert` permet de
    rejouer le cas du doublon.

    Rend la liste des tâches pour que le test puisse déclencher lui-même la recherche.
    """
    tasks: list[tuple[Any, str | None]] = []
    alert_repo = MagicMock()
    alert_repo.find_existing = AsyncMock(return_value=existing_alert)
    with (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
            new_callable=AsyncMock,
            return_value=parsed,
        ),
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module"
            ".generate_variants_and_exclusions",
            new_callable=AsyncMock,
            return_value=outcome,
        ),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=alert_repo),
        patch("app.bot.handlers.alerts_create.safe_create_task", new=_defer_task(tasks)),
    ):
        yield tasks


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_shows_loading_and_edits_to_recap():
    """Vérifie que /add <prompt> envoie le message d'attente puis l'édite avec le récap."""
    from app.services.ai.schemas import NaturalAddResult

    message = MockMessage()
    message.text = "/add Asterix tome 1 10€"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = DraftState()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    fake_result = NaturalAddResult(
        name="Asterix tome 1",
        category_slug="bd_comics",
        is_series=False,
        isbn13="9782012101333",
        author="Goscinny & Uderzo",
        publisher="Hachette",
        include_keywords=["Asterix", "tome 1"],
        exclude_keywords=["figurine"],
        max_price_cents=1000,
        conditions=["Très bon état"],
        explanation="Astérix le Gaulois tome 1 max 10€",
    )

    with _natural_add_patches(fake_result, KeywordVariantsOutcome.unanswered()) as tasks:
        await start_add_alert(message, state, db_session, db_user)

        # Le handler rend la main sur le jalon : le récap attend la recherche.
        assert loading_message.edit_text.await_count == 1
        await _drain(tasks)

    message.bot.send_chat_action.assert_awaited_once_with(chat_id=12345, action="typing")
    message.answer.assert_awaited_once()
    assert "L'IA analyse votre demande" in message.answer.call_args[0][0]

    # Le message d'attente progresse en deux fois : le jalon « ouvrage trouvé », puis
    # le récap. Le seul `answer` reste le message d'attente initial.
    assert loading_message.edit_text.await_count == 2
    milestone = loading_message.edit_text.await_args_list[0][0][0]
    assert "🔎 <b>Asterix tome 1</b> identifié" in milestone

    edited_text = loading_message.edit_text.call_args[0][0]
    # Le récap est la carte du brouillon, quel que soit le chemin qui y mène : un
    # titre unique le précède, aucun bandeau de parcours ne s'intercale.
    assert edited_text.startswith(f"{RECAP_TITLE}\n\n")
    for banner in ("Alerte configurée en langage naturel", "Modification enregistrée"):
        assert banner not in edited_text
    assert "<b>ISBN :</b> <code>9782012101333</code>" in edited_text
    assert "<b>Titre :</b> Asterix tome 1" in edited_text
    assert "<b>Prix maximum :</b> 10,00 €" in edited_text
    assert state.state is AlertCreationState.confirm_creation
    # Le moteur n'a pas répondu : le récap le dit plutôt que de laisser croire à un avis.
    assert "L'IA n'a pas répondu après une relance" in edited_text
    # Et il ne laisse pas cette carte sans issue : le bouton de relance est dessous.
    keyboard = loading_message.edit_text.call_args[1]["reply_markup"]
    assert any(
        button.callback_data == f"alert_create_ai_variants@{state.data['creation_token']}"
        for row in keyboard.inline_keyboard
        for button in row
    )
    # Aucune alerte existante : rien à signaler.
    milestone = loading_message.edit_text.await_args_list[0][0][0]
    assert "Déjà surveillé" not in milestone


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_interrupts_when_already_watched():
    """Un livre déjà surveillé interrompt immédiatement la création (binaire : pas de doublon).

    L'écran propose de mettre à jour le prix ou de conserver l'existante,
    et ne lance aucune recherche de variantes ni récapitulatif de création.
    """
    from unittest.mock import PropertyMock

    from app.services.ai.schemas import NaturalAddResult

    message = MockMessage()
    message.text = "/add asterix le gaulois"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    already = Alert(
        id=uuid.uuid4(),
        name="Astérix le Gaulois",
        user_id=db_user.id,
        isbn="9782012101333",
        max_item_price_cents=1500,
        status=AlertStatus.ACTIVE,
    )
    fake_result = NaturalAddResult(
        name="Astérix le Gaulois",
        category_slug="bd_comics",
        isbn13="9782012101333",
        include_keywords=["Astérix le Gaulois"],
        max_price_cents=2000,
    )

    alert_repo = MagicMock()
    # 1ère vérification brute (avant IA) ne trouve rien, 2ème vérification après parsing IA trouve
    alert_repo.find_existing = AsyncMock(side_effect=[None, already])

    with (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
            new_callable=AsyncMock,
            return_value=fake_result,
        ),
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module.generate_variants_and_exclusions",
            new_callable=AsyncMock,
        ) as variants_mock,
        patch("app.bot.handlers.alerts.AlertRepository", return_value=alert_repo),
    ):
        await start_add_alert(message, state, AsyncMock(), db_user)

        # Aucune recherche de variantes n'est lancée en arrière-plan
        variants_mock.assert_not_called()

        # Le message édite le message d'attente pour afficher l'alerte existante et la mise à jour
        edited_text = loading_message.edit_text.call_args[0][0]
        assert "Cette BD est déjà surveillée dans tes alertes" in edited_text
        assert "Astérix le Gaulois" in edited_text
        assert "9782012101333" in edited_text
        assert "15,00 €" in edited_text
        assert "20,00 €" in edited_text

        # Charte, règle 2 : les trois phrases d'information et de consigne sont en italique…
        assert "<i>Cette BD est déjà surveillée dans tes alertes.</i>" in edited_text
        assert "<i>Tu as indiqué un prix de 20,00 €.</i>" in edited_text
        assert edited_text.count("<i>") == 3
        # … et la règle 1 tient : libellés gras, valeurs en romain, jamais italiques.
        assert "<b>Prix maximum actuel :</b> 15,00 €" in edited_text
        assert "<i>15,00 €" not in edited_text
        assert "<b>Statut :</b> Active" in edited_text

        # Clavier avec mise à jour et conservation
        kb = loading_message.edit_text.call_args[1]["reply_markup"]
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert f"upd_alert_price:{already.id}:2000" in callbacks
        assert "alert_create_cancel" in callbacks

        # Le récapitulatif de création n'est jamais proposé
        assert "Confirmer la création de cette alerte ?" not in edited_text
        assert state.state is not AlertCreationState.confirm_creation


@pytest.mark.asyncio
async def test_start_add_alert_pre_check_avoids_ia_when_already_watched():
    """Une requête contenant un titre ou ISBN déjà en base évite tout appel IA."""
    from unittest.mock import PropertyMock

    message = MockMessage()
    message.text = "/add Astérix le Gaulois"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()
    message.answer = AsyncMock()

    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    already = Alert(
        id=uuid.uuid4(),
        name="Astérix le Gaulois",
        user_id=db_user.id,
        isbn="9782012101333",
        max_item_price_cents=1500,
        status=AlertStatus.ACTIVE,
    )

    alert_repo = MagicMock()
    alert_repo.find_existing = AsyncMock(return_value=already)

    parse_mock = AsyncMock()
    with (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
            new=parse_mock,
        ),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=alert_repo),
    ):
        await start_add_alert(message, state, AsyncMock(), db_user)

        # L'IA n'est jamais appelée
        parse_mock.assert_not_called()

        # Le message répond directement avec l'alerte existante
        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "Cette BD est déjà surveillée dans tes alertes" in text
        assert "Astérix le Gaulois" in text

        # Charte, règle 2 : la phrase d'information et la consigne du pied sont en italique,
        # la règle 1 garde les valeurs en romain.
        assert "<i>Cette BD est déjà surveillée dans tes alertes.</i>" in text
        assert (
            "<i>Pour modifier son prix maximum, entre un nouveau montant "
            "ou choisis Conserver sans modification.</i>"
        ) in text
        assert text.count("<i>") == 2
        assert "<b>Prix maximum actuel :</b> 15,00 €" in text
        assert "<i>15,00 €" not in text


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_merges_grounded_keywords_and_isbn_note():
    """Le récap signale la provenance de l'ISBN et fusionne les mots-clés ancrés."""
    from app.services.ai.schemas import BookContext, NaturalAddResult

    message = MockMessage()
    message.text = "/add la couleur des choses. Max 12 €"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = DraftState()

    book_context = BookContext(
        title="La couleur des choses",
        author="Martin Panchaud",
        publisher="Çà et là",
        publication_year=2022,
        format_detail="1 vol. (224 p.) : ill. en coul. ; 24 cm",
        isbn13="9782369903086",
    )
    fake_result = NaturalAddResult(
        name="La couleur des choses",
        category_slug="bd_comics",
        is_series=False,
        isbn13="9782369903086",
        author="Martin Panchaud",
        publisher="Çà et là",
        include_keywords=["La couleur des choses"],
        exclude_keywords=["roman poche"],
        max_price_cents=1200,
        isbn_note="Source OpenLibrary (repli BNF)",
        book_context=book_context,
    )

    outcome = KeywordVariantsOutcome(
        variants=["couleurdeschoses"],
        exclusions=["keeping two"],
        answered=True,
    )
    keywords_mock = AsyncMock(return_value=outcome)
    tasks: list = []
    alert_repo = MagicMock()
    alert_repo.find_existing = AsyncMock(return_value=None)

    with (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
            new_callable=AsyncMock,
            return_value=fake_result,
        ),
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module"
            ".generate_variants_and_exclusions",
            new=keywords_mock,
        ),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=alert_repo),
        patch("app.bot.handlers.alerts_create.safe_create_task", new=_defer_task(tasks)),
    ):
        await start_add_alert(message, state, AsyncMock(), User(id=uuid.uuid4()))
        await _drain(tasks)

    # La génération de mots-clés reçoit la fiche vérifiée de l'ouvrage
    keywords_mock.assert_awaited_once_with(
        "La couleur des choses",
        category_slug="bd_comics",
        context=book_context,
    )

    edited_text = loading_message.edit_text.call_args[0][0]
    assert "9782369903086" in edited_text
    # La provenance secondaire de l'ISBN reste signalée dans la carte compacte.
    assert "Source OpenLibrary (repli BNF)" in edited_text
    assert "<b>ISBN :</b> <code>9782369903086</code>" in edited_text
    # Exclusions du parsing puis celles ancrées sur la fiche, sans doublon
    assert "roman poche" in edited_text
    assert "keeping two" in edited_text
    assert "couleurdeschoses" in edited_text

    # Le brouillon se lit à plat : les variantes se posent dans l'état du parsing.
    assert state.data["exclude_keywords"] == ["roman poche", "keeping two"]
    assert state.data["variants"] == ["couleurdeschoses"]
    assert state.data["book_context"]["isbn13"] == "9782369903086"


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_warns_when_isbn_unconfirmed():
    """Sans ISBN confirmé, le récap prévient que la surveillance se fait par titre."""
    from app.services.ai.schemas import NaturalAddResult

    message = MockMessage()
    message.text = "/add oeuvre inconnue"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = DraftState()

    fake_result = NaturalAddResult(
        name="Œuvre inconnue",
        category_slug="bd_comics",
        isbn13=None,
        include_keywords=["Œuvre inconnue"],
        isbn_note="Aucun ISBN confirmé par la BNF pour « Œuvre inconnue » : surveillance par titre",
    )

    with _natural_add_patches(fake_result, KeywordVariantsOutcome.unanswered()) as tasks:
        await start_add_alert(message, state, AsyncMock(), User(id=uuid.uuid4()))
        await _drain(tasks)

    edited_text = loading_message.edit_text.call_args[0][0]
    assert "À confirmer" in edited_text
    assert "surveillance par titre" in edited_text


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_reports_engine_had_nothing_to_say():
    """Le moteur tranche « rien de pertinent » : le récap le distingue d'une panne."""
    from app.services.ai.schemas import NaturalAddResult

    message = MockMessage()
    message.text = "/add asterix"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = DraftState()
    fake_result = NaturalAddResult(
        name="Astérix",
        category_slug="bd_comics",
        is_series=True,
        include_keywords=["Astérix"],
        max_price_cents=2000,
    )

    with _natural_add_patches(fake_result, KeywordVariantsOutcome(answered=True)) as tasks:
        await start_add_alert(message, state, AsyncMock(), User(id=uuid.uuid4()))
        await _drain(tasks)

    edited_text = loading_message.edit_text.call_args[0][0]
    assert "L'IA n'a trouvé aucune variante ni exclusion pertinente" in edited_text
    # Un avis du moteur ne doit jamais se lire comme un silence du moteur.
    assert "pas répondu à temps" not in edited_text
    assert state.data["variants"] == []
    assert state.data["exclude_keywords"] == []


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_drops_stale_recap_if_draft_changed():
    """Si l'utilisateur a changé d'ouvrage, le récap tardif n'est pas publié.

    Entre le jalon et la réponse du moteur, le brouillon a pu être modifié ou
    abandonné. Écrire quand même le récap afficherait des mots-clés calculés pour un
    autre produit, par-dessus l'état réellement en cours.
    """
    from app.services.ai.schemas import NaturalAddResult

    message = MockMessage()
    message.text = "/add asterix"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = DraftState()
    fake_result = NaturalAddResult(
        name="Astérix",
        category_slug="bd_comics",
        is_series=True,
        include_keywords=["Astérix"],
        max_price_cents=2000,
    )
    outcome = KeywordVariantsOutcome(
        variants=["asterix tome 1"],
        exclusions=["figurine"],
        answered=True,
    )

    with _natural_add_patches(fake_result, outcome) as tasks:
        await start_add_alert(message, state, AsyncMock(), User(id=uuid.uuid4()))
        # L'utilisateur change d'ouvrage pendant que le moteur travaille.
        await state.update_data(name="Iznogoud")

        await _drain(tasks)

    # Seul le jalon est à l'écran : aucun récap d'un produit abandonné.
    assert loading_message.edit_text.await_count == 1
    assert "Alerte configurée en langage naturel" not in (
        loading_message.edit_text.await_args[0][0]
    )
    # Et rien n'a été écrit dans le brouillon courant.
    assert state.data["variants"] == []


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_fallback_on_parse_failure():
    """Vérifie que si l'IA ne peut pas parser, le message d'attente bascule sur le sélecteur."""
    message = MockMessage()
    message.text = "/add requete incomprehensible"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = AsyncMock()
    state.clear = AsyncMock()
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    mock_alert_repo = MagicMock()
    mock_alert_repo.find_existing = AsyncMock(return_value=None)
    with (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=mock_alert_repo),
    ):
        await start_add_alert(message, state, db_session, db_user)

    message.bot.send_chat_action.assert_awaited_once_with(chat_id=12345, action="typing")
    message.answer.assert_awaited_once()
    loading_message.edit_text.assert_awaited_once()
    edited_text = loading_message.edit_text.call_args[0][0]
    assert "Choisis la catégorie de ce produit" in edited_text
    state.set_state.assert_called_with(AlertCreationState.waiting_for_category)


@pytest.mark.asyncio
async def test_handle_negotiate_shows_toast_and_typing_action():
    """Vérifie que [ 💬 Négocier ] répond immédiatement par un toast et du typing."""
    from app.bot.handlers.callbacks import handle_negotiate
    from app.services.ai.schemas import NegotiationResult

    listing_id = uuid.uuid4()
    callback = MagicMock()
    callback.data = f"negotiate:{listing_id}"
    callback.answer = AsyncMock()
    mock_msg = MockMessage()
    mock_msg.chat = MagicMock()
    mock_msg.chat.id = 12345
    mock_msg.bot = MagicMock()
    mock_msg.bot.send_chat_action = AsyncMock()
    mock_msg.reply = AsyncMock()
    callback.message = mock_msg

    fake_listing = MagicMock()
    fake_listing.id = listing_id
    fake_listing.title = "BD Tintin"
    fake_listing.item_price_cents = 2000

    fake_offer = NegotiationResult(
        suggested_offer_cents=1600,
        suggested_offer_euros=16.0,
        message="Bonjour, seriez-vous ouvert à 16€ ?",
    )

    db_session = AsyncMock()

    with (
        patch("app.bot.handlers.callbacks.ListingRepository") as mock_repo_cls,
        patch(
            "app.bot.handlers.callbacks.negotiation_module.generate_offer",
            new_callable=AsyncMock,
            return_value=fake_offer,
        ),
    ):
        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=fake_listing)
        mock_repo_cls.return_value = mock_repo

        await handle_negotiate(callback, db_session)

    callback.answer.assert_awaited_once_with("⏳ Coach IA : Analyse de l'offre en cours...")
    callback.message.bot.send_chat_action.assert_awaited_once_with(chat_id=12345, action="typing")
    callback.message.reply.assert_awaited_once()
    reply_text = callback.message.reply.call_args[1]["text"]
    assert "Offre suggérée" in reply_text


@pytest.mark.asyncio
async def test_select_alert_creation_edition_switches_isbn():
    """Le clic sur une édition concurrente change l'ISBN et rafraîchit le récap."""
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import CallbackQuery, Message

    from app.bot.handlers.alerts_create_confirm import select_alert_creation_edition
    from app.bot.states.alert_creation import AlertCreationState
    from app.services.book_metadata_service import BookMetadata, BookResolution
    from tests.add_fakes import fake_bnf, fake_book_search

    editions = [
        {"isbn13": "9782369903086", "publisher": "Çà et là"},
        {"isbn13": "9782369904151", "publisher": "Futuropolis"},
    ]
    draft = {
        "name": "La couleur des choses",
        "author": "Martin Panchaud",
        "publisher": "Çà et là",
        "isbn": "9782369903086",
        "max_item_price_cents": 1200,
        "include_keywords": ["la couleur des choses"],
        "exclude_keywords": [],
        "category_slug": "bd_comics",
        "edition_alternatives": editions,
    }

    stored = dict(draft)
    state = AsyncMock()
    state.get_data = AsyncMock(side_effect=lambda: dict(stored))

    async def _update(data=None, **_kwargs):
        stored.update(data or {})

    state.update_data = AsyncMock(side_effect=_update)
    state.set_state = AsyncMock()

    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "al_create_edition:1"
    callback.message = message
    callback.answer = AsyncMock()

    # Le clic revalide l'ISBN choisi : l'autorité doit le connaître pour l'attacher.
    known = BookResolution(
        best=BookMetadata(
            title="La couleur des choses",
            author="Martin Panchaud",
            isbn13="9782369903086",
        ),
        alternatives=[BookMetadata(title="La couleur des choses", isbn13="9782369904151")],
    )
    with patch(
        "app.bot.handlers.alerts_create_confirm.BookSearchUseCase",
        fake_book_search(fake_bnf(known)),
    ):
        await select_alert_creation_edition(callback, state)

    assert stored["isbn"] == "9782369904151"
    assert stored["publisher"] == "Futuropolis"
    assert stored["is_series"] is False
    state.set_state.assert_awaited_with(AlertCreationState.confirm_creation)

    text = message.edit_text.call_args[0][0]
    assert "<b>Éditeur :</b> Futuropolis" in text
    assert "<b>ISBN :</b> <code>9782369904151</code>" in text
    # Sans millésime ni format, la ligne « Édition » n'aurait rien de plus à dire
    # que « Éditeur » : elle est omise plutôt que dupliquée.
    assert "<b>Édition :</b>" not in text

    keyboard = message.edit_text.call_args[1]["reply_markup"]
    edition_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("al_create_edition")
    ]
    assert [button.text for button in edition_buttons] == [
        "1️⃣ Çà et là",
        "2️⃣ Futuropolis",
    ]


@pytest.mark.asyncio
async def test_select_alert_creation_edition_rejects_out_of_range_index():
    """Un index hors limites ne doit jamais modifier le brouillon."""
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import CallbackQuery, Message

    from app.bot.handlers.alerts_create_confirm import select_alert_creation_edition

    draft = {"isbn": "9782369903086", "edition_alternatives": [{"isbn13": "9782369903086"}]}
    state = AsyncMock()
    state.get_data = AsyncMock(return_value=draft)
    state.update_data = AsyncMock()

    callback = MagicMock(spec=CallbackQuery)
    callback.data = "al_create_edition:7"
    callback.message = MagicMock(spec=Message)
    callback.answer = AsyncMock()

    await select_alert_creation_edition(callback, state)

    state.update_data.assert_not_called()
    callback.answer.assert_awaited_once_with("Édition inconnue.", show_alert=True)


@pytest.mark.asyncio
async def test_finalize_loading_message_falls_back_to_plain_text():
    """Un refus de formatage Telegram ne doit pas remonter au webhook.

    Un handler qui lève vaut « update non traité » pour Telegram, qui rejoue alors le
    même update : l'utilisateur voit « Recherche IA en cours » en boucle.
    """
    from aiogram.exceptions import TelegramBadRequest

    from app.bot.handlers.alerts_create import _finalize_loading_message

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    # Dans l'incident, l'édition du message d'attente échoue puis l'envoi aussi :
    # c'est ce second échec qui faisait remonter l'exception au webhook.
    loading_msg = AsyncMock()
    loading_msg.edit_text = AsyncMock(side_effect=TelegramBadRequest(
        method=None, message="message is not modified"
    ))

    event = AsyncMock()
    event.answer = AsyncMock(
        side_effect=[
            TelegramBadRequest(method=None, message="can't parse entities"),
            None,
        ]
    )

    await _finalize_loading_message(
        loading_msg,
        event,
        "🎯 <b>Récapitulatif</b>\n📌 <b>Nom :</b> ASTON & PS5",
        keyboard,
    )

    assert event.answer.await_count == 2
    final_call = event.answer.await_args_list[1]
    assert "<b>" not in final_call.args[0]
    assert "&amp;" in final_call.args[0] or "&" in final_call.args[0]
    assert final_call.kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_finalize_loading_message_does_not_swallow_other_errors():
    """Un échec réseau reste une panne : il doit remonter, pas être masqué en texte brut."""
    from app.bot.handlers.alerts_create import _finalize_loading_message

    loading_msg = AsyncMock()
    loading_msg.edit_text = AsyncMock(side_effect=RuntimeError("non éditable"))
    event = AsyncMock()
    event.answer = AsyncMock(side_effect=ConnectionError("réseau indisponible"))

    with pytest.raises(ConnectionError):
        await _finalize_loading_message(
            loading_msg, event, "texte", InlineKeyboardMarkup(inline_keyboard=[])
        )

"""Parcours de clavier : ce que les boutons proposent, ce qu'ils ne font jamais.

Deux familles. Les claviers vivants d'abord : les œuvres homonymes sont proposées
avant d'être rattachées, et rien n'est choisi tant que l'utilisateur n'a pas désigné
la sienne. Les écrans dégénérés ensuite : « Modifier » et « Retour au récapitulatif »
sur un brouillon vidé disent qu'il est terminé, au lieu de proposer un menu sans objet
ou de re-rendre une carte par défaut.

Le harnais (doubles, câblage, lecture des écrans) est partagé : `tests/add_fakes.py`,
`tests/add_journey.py`, `tests/screens.py`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.bot.handlers.alerts_create import start_add_alert
from app.bot.handlers.alerts_create_confirm import select_alert_creation_work
from app.services.book_metadata_service import BookMetadata, BookResolution
from tests.add_fakes import DraftState as _DraftState
from tests.add_fakes import fake_bnf, fake_book_search, fake_deepseek
from tests.add_journey import drain, patched, repositories
from tests.screens import message_with_recap, recap_text, work_buttons


class DraftState(_DraftState):
    """Identité FSM locale pour exercer la coordination, sans modifier le double partagé."""

    def __init__(self):
        super().__init__()
        self.storage = MemoryStorage()
        self.key = StorageKey(bot_id=1, chat_id=42, user_id=42)


@pytest.mark.asyncio
async def test_add_homonym_title_attaches_nothing_until_chosen():
    """Cas réel : la BNF ne connaît que l'homonyme de 1989 du titre demandé.

    Le récap ne rattache ni ISBN, ni auteur, ni éditeur — la fiche du roman
    disparaissait sous un ISBN de 2025 et ne se corrigeait plus ensuite. Les deux
    œuvres sont proposées, et choisir celle qu'on veut réaligne tout.
    """
    homonym = BookResolution(
        best=BookMetadata(
            title="Les Enfants perdus",
            author="Rosemonde Ahou de Saintange",
            publisher="La Pensée universelle",
            isbn13="9782214080627",
            source="bnf",
            publication_year=1989,
        ),
        other_works=[
            BookMetadata(
                title="Les Enfants perdus",
                author="Nef",
                publisher="Éditions du Chêne",
                isbn13="9782810206087",
                source="bnf",
                publication_year=2025,
            )
        ],
    )
    client = fake_deepseek(
        {
            "name": "Les Enfants perdus",
            "category_slug": "bd_comics",
            "isbn13": None,
            "include_keywords": ["Les Enfants perdus"],
            "max_price_cents": 1200,
            "is_series": False,
        }
    )
    repos = repositories("9782810206087")

    message, loading_message = message_with_recap("/add les enfants perdus. Max 12 €")
    state = DraftState()

    patches = patched(repos, fake_bnf(homonym), client)
    # Le choix d'une œuvre réinterroge la BNF pour ses éditions concurrentes.
    with patch(
        "app.bot.handlers.alerts_helpers.BookSearchUseCase",
        fake_book_search(fake_bnf(homonym)),
    ):
        for entered in patches:
            entered.start()
        try:
            await start_add_alert(message, state)
            await drain(repos["tasks"])

            recap = recap_text(loading_message)
            assert "<b>ISBN :</b> À confirmer" in recap
            # La note est échappée par le récap : comparaison sur un fragment sans apostrophe.
            assert "⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre." in recap
            # Aucune donnée du roman : ni l'ISBN, ni l'auteur, ni l'éditeur.
            assert "9782214080627" not in recap
            assert "Rosemonde Ahou de Saintange" not in recap
            assert "La Pensée universelle" not in recap

            keyboard = loading_message.edit_text.call_args[1]["reply_markup"]
            buttons = work_buttons(keyboard)
            # Nom de famille et millésime : l'éditeur complet se fait tronquer par Telegram.
            assert [button.text for button in buttons] == [
                "1️⃣ Saintange · 1989",
                "2️⃣ Nef · 2025",
            ]

            switch_message = MagicMock(spec=Message)
            switch_message.edit_text = AsyncMock()
            switch_callback = MagicMock()
            switch_callback.data = "al_create_work:1"
            switch_callback.message = switch_message
            switch_callback.answer = AsyncMock()

            await select_alert_creation_work(switch_callback, state)

            assert state.data["isbn"] == "9782810206087"
            assert state.data["author"] == "Nef"
            assert state.data["publisher"] == "Éditions du Chêne"

            switch_recap = switch_message.edit_text.call_args[0][0]
            assert "<b>ISBN :</b> <code>9782810206087</code>" in switch_recap
            assert "<b>Auteur :</b> Nef" in switch_recap
            assert "<b>Éditeur :</b> Éditions du Chêne" in switch_recap
            assert "9782214080627" not in switch_recap
            # L'œuvre est choisie : les propositions d'homonymes disparaissent.
            assert not work_buttons(switch_message.edit_text.call_args[1]["reply_markup"])

            # Les mots-clés sont recalculés sur la fiche choisie, en arrière-plan.
            await drain(repos["tasks"])
            assert state.data["keywords_finalized"] is True
        finally:
            for entered in reversed(patches):
                entered.stop()


@pytest.mark.asyncio
async def test_returning_to_a_draft_that_is_gone_never_shows_a_default_recap():
    """Un brouillon vidé ne se relit pas comme une création en cours.

    Après une création, l'état est vidé ; l'ancien clavier restait affiché et
    « Retour au récapitulatif » re-rendait « Titre : Alerte » sans aucune donnée.
    """
    from app.bot.handlers.alerts_create_confirm import DRAFT_GONE_MESSAGE, back_to_recap

    state = DraftState()
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.data = "al_create_edit:back"
    callback.message = message
    callback.answer = AsyncMock()

    await back_to_recap(callback, state)

    text = message.edit_text.call_args[0][0]
    assert text == DRAFT_GONE_MESSAGE
    assert "Titre :" not in text
    keyboard = message.edit_text.call_args[1]["reply_markup"]
    assert keyboard.inline_keyboard == []


@pytest.mark.asyncio
async def test_edit_menu_on_a_gone_draft_says_it_is_finished():
    """« Modifier » sur un brouillon vidé ne propose pas un menu sans objet."""
    from app.bot.handlers.alerts_create_confirm import (
        DRAFT_GONE_MESSAGE,
        edit_alert_creation_prompt,
    )

    state = DraftState()
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.data = "alert_create_edit"
    callback.message = message
    callback.answer = AsyncMock()

    await edit_alert_creation_prompt(callback, state)

    assert message.edit_text.call_args[0][0] == DRAFT_GONE_MESSAGE
    assert message.edit_text.call_args[1]["reply_markup"].inline_keyboard == []

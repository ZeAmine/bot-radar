"""Parcours d'identité : quel ISBN la création finit par retenir.

Ces tests enchaînent les vrais handlers (`start_add_alert` → bascule d'édition ou
saisie manuelle de l'ISBN → `save_alert`) et ne simulent que les frontières : le
moteur IA, la BNF, l'agrégateur de marché et les repositories. Ce qu'ils verrouillent
est le contrat produit de l'ADR-014 puis ADR-017 :

- l'ISBN du modèle n'est jamais persisté sans confirmation BNF ;
- les éditions concurrentes sont proposées dans le récap avant la création ;
- l'ISBN finalement retenu est celui qui pilote Vinted, le matching et la cote ;
- un ISBN saisi à la main prime, même quand le modèle en propose un autre.

Le harnais (doubles, câblage, lecture des écrans) est partagé : `tests/add_fakes.py`,
`tests/add_journey.py`, `tests/screens.py`.
"""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.bot.handlers.alerts_create import save_alert, start_add_alert
from app.bot.handlers.alerts_create_confirm import (
    process_creation_edit_input,
    select_alert_creation_edition,
)
from app.db.models.user import User
from app.services.book_metadata_service import (
    BookMetadata,
    BookMetadataService,
    BookResolution,
    parse_tome_notices,
)
from tests.add_fakes import (
    BNF_ALTERNATIVE,
    BNF_EDITION,
    ISBN_AUTRE_LIVRE,
    ISBN_INCONNU,
    fake_bnf,
    fake_book_search,
    fake_llm,
    llm_payload,
)
from tests.add_fakes import DraftState as _DraftState
from tests.add_journey import drain, patched, repositories
from tests.screens import (
    confirm_press,
    edition_buttons,
    incoming_message,
    message_with_recap,
    open_isbn_edit_field,
    recap_text,
)


class DraftState(_DraftState):
    """Identité FSM locale pour exercer la coordination, sans modifier le double partagé."""

    def __init__(self):
        super().__init__()
        self.storage = MemoryStorage()
        self.key = StorageKey(bot_id=1, chat_id=42, user_id=42)


@pytest.mark.asyncio
async def test_add_model_isbn_of_another_book_then_edition_switch():
    """Le modèle propose l'ISBN d'un autre livre : la BNF tranche, l'utilisateur choisit.

    Chaîne complète : `/add` → récap avec éditions concurrentes → clic sur l'édition
    voulue → création de l'alerte avec l'ISBN choisi, l'URL Vinted ISBN et la cote.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    bnf = fake_bnf(resolution)
    client = fake_llm(llm_payload())
    repos = repositories(BNF_ALTERNATIVE.isbn13)

    message, loading_message = message_with_recap(
        "/add la couleur des choses. Max 12 €"
    )
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    patches = patched(repos, bnf, client)
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state, db_session, db_user)
        await drain(repos["tasks"])

        # L'ISBN du modèle est refusé : la BNF le rattache à un autre ouvrage.
        bnf.confirm_isbn_for_title.assert_not_awaited()

        recap = recap_text(loading_message)
        assert "<b>ISBN :</b> <code>9782369903086</code>" in recap
        assert "<b>Titre :</b> La couleur des choses" in recap
        assert "<b>Éditeur :</b> Çà et là" in recap
        # L'ISBN n'apparaît qu'une fois : plus de ligne dédiée redondante.
        assert recap.count("9782369903086") == 1
        assert ISBN_AUTRE_LIVRE not in recap
        assert (
            "<b>Édition :</b> 2022 · Çà et là · 224 p. · 24 cm" in recap
        )

        buttons = edition_buttons(loading_message)
        assert [button.text for button in buttons] == [
            "1️⃣ Çà et là - 2022",
            "2️⃣ çà et là",
        ]

        switch_message = MagicMock(spec=Message)
        switch_message.edit_text = AsyncMock()
        switch_callback = MagicMock()
        switch_callback.data = "al_create_edition:1"
        switch_callback.message = switch_message
        switch_callback.answer = AsyncMock()

        await select_alert_creation_edition(switch_callback, state)

        assert state.data["isbn"] == "9782369904151"
        switch_recap = switch_message.edit_text.call_args[0][0]
        assert (
            "<b>Édition :</b> Editions çà et là · 224 p. · 19 cm"
            in switch_recap
        )
        # La bascule met à jour la fiche et l'édition décrite, pas le clavier.
        assert "<b>Éditeur :</b> Editions çà et là" in switch_recap
        switch_keyboard = switch_message.edit_text.call_args[1]["reply_markup"]
        assert [
            button.text
            for row in switch_keyboard.inline_keyboard
            for button in row
            if button.callback_data.startswith("al_create_edition")
        ] == ["1️⃣ Çà et là - 2022", "2️⃣ çà et là"]

        # Création : l'ISBN choisi pilote l'URL Vinted, le livre et la cote.
        confirm_callback, confirm_message = confirm_press()

        await save_alert(confirm_callback, state, db_user, db_session)

        create_kwargs = repos["alert_repo"].create.call_args.kwargs
        assert create_kwargs["isbn"] == "9782369904151"
        assert "search_text=9782369904151" in create_kwargs["vinted_search_url"]
        assert repos["book_repo"].create.call_args.kwargs["isbn13"] == "9782369904151"

        # La création attend la cote du livre avant d'activer la surveillance.
        repos["price_now"].assert_awaited_once_with(
            repos["book_repo"].create.return_value.id,
            reason="alert_created",
        )

        confirmation = confirm_message.edit_text.call_args[0][0]
        assert "<b>ISBN :</b> <code>9782369904151</code>" in confirmation
        # L'édition choisie se relit dans la confirmation : même carte, donc même ligne.
        assert "<b>Édition :</b> Editions çà et là · 224 p. · 19 cm" in confirmation
        # La collecte simulée a échoué : la confirmation nomme l'échec au lieu
        # d'annoncer une cote à venir.
        assert "<b>Cote :</b> récupération impossible" in confirmation
    finally:
        for entered in reversed(patches):
            entered.stop()


@pytest.mark.asyncio
async def test_add_user_types_correct_isbn_when_model_is_wrong():
    """Aucun ISBN confirmé : l'utilisateur saisit le bon et l'alerte part dessus.

    Chaîne complète : `/add` avec un ISBN du modèle rejeté et une BNF muette →
    récap en alerte → menu ✏️ Modifier → champ ISBN → saisie du bon ISBN →
    récap à jour → création sur cet ISBN.
    """
    bnf = fake_bnf(resolution=None)
    client = fake_llm(llm_payload())
    repos = repositories(BNF_EDITION.isbn13)
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])

    message, loading_message = message_with_recap(
        "/add la couleur des choses. Max 12 €"
    )
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    patches = patched(

        repos,
        bnf,
        client,
        isbn_bnf=fake_bnf(resolution, verified_isbn=BNF_EDITION.isbn13),
    )
    with patch(
        "app.services.book_metadata_service.BookMetadataService",
        return_value=fake_bnf(resolution, verified_isbn=BNF_EDITION.isbn13),

    ):
        for entered in patches:
            entered.start()
        try:
            await start_add_alert(message, state, db_session, db_user)
            await drain(repos["tasks"])

            # Sans confirmation BNF, l'alerte part en surveillance par titre.
            recap = recap_text(loading_message)
            assert "<b>ISBN :</b> introuvable" in recap
            assert "Ouvrage non identifié" in recap
            assert not edition_buttons(loading_message)

            await open_isbn_edit_field(state)

            # Saisie du bon ISBN : fiche réécrite par la notice, éditions réalignées.
            typed_message = incoming_message("9782369903086")
            await process_creation_edit_input(typed_message, state)

            assert state.data["isbn"] == "9782369903086"
            assert state.data["is_series"] is False
            assert state.data["author"] == BNF_EDITION.author
            assert [edition["isbn13"] for edition in state.data["edition_alternatives"]] == [
                BNF_EDITION.isbn13,
                BNF_ALTERNATIVE.isbn13,
            ]

            typed_answer = typed_message.answer.call_args[0][0]
            assert "<b>ISBN :</b> <code>9782369903086</code>" in typed_answer
            assert (
                "<b>Édition :</b> 2022 · Çà et là · 224 p. · 24 cm"
                in typed_answer
            )
            # La note de provenance de l'ancien ISBN ne survit pas à la correction.
            assert "surveillance par titre" not in typed_answer

            # Mots-clés recalculés : récap sans les anciens, recherche relancée en arrière-plan.
            await drain(repos["tasks"])
            recap_answer = typed_message.answer.call_args[0][0]
            assert "<b>Mots-clés inclus :</b> couleur des choses" in recap_answer

            # Création sur l'ISBN saisi, avec l'URL Vinted par ISBN.
            confirm_callback, confirm_message = confirm_press()

            await save_alert(confirm_callback, state, db_user, db_session)

            create_kwargs = repos["alert_repo"].create.call_args.kwargs
            assert create_kwargs["isbn"] == "9782369903086"
            assert "search_start_type=item_page_isbn" in create_kwargs["vinted_search_url"]
            assert "search_text=9782369903086" in create_kwargs["vinted_search_url"]
            assert repos["book_repo"].create.call_args.kwargs["isbn13"] == "9782369903086"
            repos["price_now"].assert_awaited_once_with(
                repos["book_repo"].create.return_value.id,
                reason="alert_created",
            )
        finally:
            for entered in reversed(patches):
                entered.stop()


@pytest.mark.asyncio
async def test_add_typed_isbn_unknown_to_bnf_keeps_it_and_drops_the_previous_work():
    """Un ISBN que personne ne connaît est gardé, mais l'identité d'avant part avec lui.

    L'ouvrage n'est pas encore catalogué : garder l'auteur et l'éditeur
    rapportés par l'ISBN précédent afficherait une fiche qui décrit un autre livre sous
    le code que l'utilisateur vient de saisir.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    client = fake_llm(llm_payload())
    repos = repositories(ISBN_INCONNU)

    message, loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()

    patches = patched(repos, fake_bnf(resolution), client)
    with patch(
        "app.bot.handlers.alerts_helpers.BookSearchUseCase",
        fake_book_search(fake_bnf(resolution)),
    ):
        for entered in patches:
            entered.start()
        try:
            await start_add_alert(message, state)
            await drain(repos["tasks"])

            await open_isbn_edit_field(state)
            typed_message = incoming_message(ISBN_INCONNU)
            await process_creation_edit_input(typed_message, state)

            assert state.data["isbn"] == ISBN_INCONNU
            # Rien ne rattache plus l'ancien auteur au nouvel ISBN : le récap le dit.
            assert state.data["author"] is None
            assert state.data["publisher"] is None
            assert state.data["edition_alternatives"] == []
            typed_answer = typed_message.answer.call_args[0][0]
            assert "<b>ISBN :</b> <code>" + ISBN_INCONNU + "</code>" in typed_answer
            assert "ISBN saisi, non confirmé : aucune notice exploitable" in typed_answer
            assert "Martin Panchaud" not in typed_answer
            await drain(repos["tasks"])
        finally:
            for entered in reversed(patches):
                entered.stop()


@pytest.mark.asyncio
async def test_add_typed_isbn_of_another_book_is_refused():
    """Un ISBN que la BNF rattache à un autre ouvrage n'est jamais retenu.

    Le garder ferait matcher l'alerte sur un livre qu'on ne cherche pas et coterait
    celui-ci à la place de l'autre.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    client = fake_llm(llm_payload())
    repos = repositories(BNF_EDITION.isbn13)

    message, loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()

    patches = patched(

        repos,
        fake_bnf(resolution),
        client,
        isbn_bnf=fake_bnf(resolution, conflicting_isbn=ISBN_AUTRE_LIVRE),
    )
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state)
        await drain(repos["tasks"])


        await open_isbn_edit_field(state)
        typed_message = incoming_message(ISBN_AUTRE_LIVRE)
        await process_creation_edit_input(typed_message, state)

        # Le brouillon garde l'ISBN d'avant : rien n'a été changé.
        assert state.data["isbn"] != ISBN_AUTRE_LIVRE
        refusal = typed_message.answer.call_args[0][0]
        assert "Keeping two" in refusal
        assert "Jordan Crane" in refusal
        assert "Réessaie" in refusal
    finally:
        for entered in reversed(patches):
            entered.stop()


@pytest.mark.asyncio
async def test_add_typed_isbn_beats_model_proposal():
    """Un ISBN saisi dans la commande prime sur celui proposé par le modèle."""
    resolution = BookResolution(best=BNF_ALTERNATIVE)
    bnf = fake_bnf(resolution, verified_isbn=BNF_EDITION.isbn13)
    client = fake_llm(llm_payload())
    repos = repositories(BNF_EDITION.isbn13)

    message, loading_message = message_with_recap(
        "/add 9782369903086 la couleur des choses max 12€"
    )
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    patches = patched(repos, bnf, client)
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state, db_session, db_user)
        await drain(repos["tasks"])

        bnf.confirm_isbn_for_title.assert_awaited_once_with("9782369903086", None)

        recap = recap_text(loading_message)
        assert "<b>ISBN :</b> <code>9782369903086</code>" in recap
        assert ISBN_AUTRE_LIVRE not in recap
    finally:
        for entered in reversed(patches):
            entered.stop()


BNF_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bnf"
ADAPTATION_ISBN = "9782075163743"


def adaptation_notice() -> BookMetadata:
    """La notice d'adaptation que la BNF rend pour l'ISBN du tome 1, lue telle quelle.

    L'ISBN `9782075163743` porte deux notices : le roman de « La Passe-miroir » et la
    bande dessinée qui l'adapte (« Gallimard bande dessinée », `ill. en coul.`,
    `29 cm`, « d'après l'oeuvre de Christelle Dabos »). C'est la seconde que ce test
    ajoute, et elle vient de la fixture réelle : rien n'est réécrit à la main.
    """
    notices = parse_tome_notices(
        (BNF_FIXTURES / f"marcxchange_{ADAPTATION_ISBN}.xml").read_bytes()
    )
    adapted = next(notice for notice in notices if notice.adaptation_statement)
    return BookMetadata(
        title=adapted.title,
        author=adapted.authors[0],
        publisher=adapted.publisher,
        isbn13=adapted.isbn13,
        publication_year=adapted.declared_publication_year,
        format_detail=f"1 vol. : {adapted.illustration} ; {adapted.dimensions}",
    )


@pytest.mark.asyncio
async def test_addisbn_still_adds_the_adaptation_the_series_flow_refuses():
    """L'ajout par ISBN seul reste possible, adaptation comprise.

    Le flux de série refuse désormais cet ISBN (`different_edition`) : ses deux
    notices décrivent deux éditions, et aucune n'est celle que la série atteste. Ce
    refus ne dit rien d'une demande explicite — `/addisbn 9782075163743` nomme
    l'ouvrage que l'utilisateur veut suivre, et la création aboutit.

    C'est un garde-fou contre nous-mêmes : l'ajout explicite ne consulte **pas** la
    règle d'admission de tome. Si elle se remet un jour sur ce chemin, l'espion la
    fait échouer ici au lieu de laisser le comportement changer en silence.
    """
    adaptation = adaptation_notice()
    bnf = fake_bnf(BookResolution(best=adaptation))
    client = fake_llm(llm_payload())
    repos = repositories(adaptation.isbn13)

    message, loading_message = message_with_recap(f"/addisbn {ADAPTATION_ISBN}")
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    def _forbidden(name: str) -> MagicMock:
        return MagicMock(side_effect=AssertionError(
            f"{name} : l'ajout par ISBN explicite ne juge pas l'admission d'un tome"
        ))

    admission = _forbidden("admit_tome_notices")
    series_lookup = _forbidden("lookup_series_notices")

    patches = patched(repos, bnf, client) + (
        patch("app.services.book_metadata_service.admit_tome_notices", admission),
        patch.object(BookMetadataService, "lookup_series_notices", series_lookup),
    )
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state, db_session, db_user)
        await drain(repos["tasks"])

        # L'écran décrit bien la BD, pas le roman : c'est elle que la demande nomme.
        recap = recap_text(loading_message)
        assert f"<b>ISBN :</b> <code>{ADAPTATION_ISBN}</code>" in recap
        assert "<b>Éditeur :</b> Gallimard bande dessinée" in recap
        assert state.data["isbn"] == ADAPTATION_ISBN

        confirm_callback, confirm_message = confirm_press()
        await save_alert(confirm_callback, state, db_user, db_session)

        # L'ajout a eu lieu, sur cet ISBN-là, et rien n'a consulté la règle de tome.
        assert repos["alert_repo"].create.call_args.kwargs["isbn"] == ADAPTATION_ISBN
        assert (
            f"search_text={ADAPTATION_ISBN}"
            in repos["alert_repo"].create.call_args.kwargs["vinted_search_url"]
        )
        assert repos["book_repo"].create.call_args.kwargs["isbn13"] == ADAPTATION_ISBN
        assert admission.call_count == 0
        assert series_lookup.call_count == 0

        confirmation = confirm_message.edit_text.call_args[0][0]
        assert f"<b>ISBN :</b> <code>{ADAPTATION_ISBN}</code>" in confirmation
    finally:
        for entered in reversed(patches):
            entered.stop()

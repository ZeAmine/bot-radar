"""Tests bout en bout du flux `/add` en langage naturel, DeepSeek simulé.

Ces tests enchaînent les vrais handlers (`start_add_alert` → sélection d'édition ou
saisie manuelle de l'ISBN → `save_alert`) et ne simulent que les frontières :
DeepSeek, la BNF, l'agrégateur de marché et les repositories. Ce qu'ils verrouillent
est le contrat produit de l'ADR-014 puis ADR-017 :

- l'ISBN du modèle n'est jamais persisté sans confirmation BNF ;
- les éditions concurrentes sont proposées dans le récap avant la création ;
- l'ISBN finalement retenu est celui qui pilote Vinted, le matching et la cote ;
- un ISBN saisi à la main prime, même quand le modèle en propose un autre.
"""

import html
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

from app.bot.handlers.alerts_create import (
    handle_category_selected,
    handle_query_input,
    process_max_price_input,
    save_alert,
    start_add_alert,
)
from app.bot.handlers.alerts_create_confirm import (
    process_creation_edit_input,
    select_alert_creation_edition,
    select_alert_creation_work,
)
from app.bot.handlers.alerts_helpers import RECAP_TITLE, build_creation_recap
from app.db.models.user import User
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from app.services.book_metadata_service import BookMetadata, BookResolution
from app.services.market_price_service import PENDING_COTE_LINE

# Le harnais du parcours `/add` est partagé, jamais importé d'un module de test : les
# doubles et leurs données dans `tests/add_fakes.py`, leur câblage et les espions dans
# `tests/add_journey.py`, la lecture de ce qu'un écran publie dans `tests/screens.py`.
from tests.add_fakes import (
    BNF_ALTERNATIVE,
    BNF_EDITION,
    ISBN_AUTRE_LIVRE,
    ISBN_INCONNU,
    ROUTE_BANNERS,
    DraftState,
    FakeMessage,
    deepseek_payload,
    fake_bnf,
    fake_deepseek,
)
from tests.add_journey import drain, patched, repositories
from tests.screens import (
    confirm_press,
    edition_buttons,
    incoming_message,
    index_of_line,
    message_with_recap,
    open_isbn_edit_field,
    recap_text,
    work_buttons,
)


@pytest.mark.asyncio
async def test_add_end_to_end_model_isbn_of_another_book_then_edition_switch():
    """Le modèle propose l'ISBN d'un autre livre : la BNF tranche, l'utilisateur choisit.

    Chaîne complète : `/add` → récap avec éditions concurrentes → clic sur l'édition
    voulue → création de l'alerte avec l'ISBN choisi, l'URL Vinted ISBN et la cote.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    bnf = fake_bnf(resolution)
    client = fake_deepseek(deepseek_payload())
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

        # 1. L'ISBN du modèle est refusé : la BNF le rattache à un autre ouvrage.
        bnf.verify_isbn.assert_awaited_once_with(ISBN_AUTRE_LIVRE, "La couleur des choses")

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

        # 2. L'utilisateur choisit l'autre édition avant de valider.
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

        # 3. Création : l'ISBN choisi pilote l'URL Vinted, le livre et la cote.
        confirm_message = MagicMock(spec=Message)
        confirm_message.edit_text = AsyncMock()
        confirm_callback = MagicMock()
        confirm_callback.data = "alert_create_confirm"
        confirm_callback.message = confirm_message
        confirm_callback.answer = AsyncMock()

        await save_alert(confirm_callback, state, db_user, db_session)

        create_kwargs = repos["alert_repo"].create.call_args.kwargs
        assert create_kwargs["isbn"] == "9782369904151"
        assert "search_text=9782369904151" in create_kwargs["vinted_search_url"]
        assert repos["book_repo"].create.call_args.kwargs["isbn13"] == "9782369904151"

        # 4. La cote part en arrière-plan sur le livre créé, sans retarder la
        # confirmation : l'utilisateur ne fait pas attendre son alerte aux marchands.
        repos["schedule_pricing"].assert_called_once_with(
            repos["book_repo"].create.return_value.id,
            reason="alert_created",
        )

        confirmation = confirm_message.edit_text.call_args[0][0]
        assert "<b>ISBN :</b> <code>9782369904151</code>" in confirmation
        # L'édition qu'il vient de choisir se relit dans la confirmation, à la place du
        # récap où elle la montrait : c'est la même carte, donc la même ligne.
        assert "<b>Édition :</b> Editions çà et là · 224 p. · 19 cm" in confirmation
        # Le livre vient d'être créé : sa cote est annoncée, pas encore chiffrée.
        assert PENDING_COTE_LINE in confirmation
    finally:
        for entered in reversed(patches):
            entered.stop()


@pytest.mark.asyncio
async def test_add_end_to_end_user_types_correct_isbn_when_model_is_wrong():
    """Aucun ISBN confirmé : l'utilisateur saisit le bon et l'alerte part dessus.

    Chaîne complète : `/add` avec un ISBN du modèle rejeté et une BNF muette →
    récap en alerte → menu ✏️ Modifier → champ ISBN → saisie du bon ISBN →
    récap à jour → création sur cet ISBN.
    """
    bnf = fake_bnf(resolution=None)
    client = fake_deepseek(deepseek_payload())
    repos = repositories(BNF_EDITION.isbn13)
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])

    message, loading_message = message_with_recap(
        "/add la couleur des choses. Max 12 €"
    )
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    patches = patched(repos, bnf, client)
    with (
        patch(
            "app.bot.handlers.alerts_helpers.BookMetadataService",
            return_value=fake_bnf(resolution, verified_isbn=BNF_EDITION.isbn13),
        ),
        patch(
            "app.services.book_metadata_service.BookMetadataService",
            return_value=fake_bnf(resolution, verified_isbn=BNF_EDITION.isbn13),
        ),
    ):
        for entered in patches:
            entered.start()
        try:
            await start_add_alert(message, state, db_session, db_user)
            await drain(repos["tasks"])

            # 1. Sans confirmation BNF, l'alerte part en surveillance par titre.
            recap = recap_text(loading_message)
            assert "<b>ISBN :</b> À confirmer" in recap
            assert "surveillance par titre" in recap
            assert not edition_buttons(loading_message)

            # 2. L'utilisateur corrige via le menu de modification.
            await open_isbn_edit_field(state)

            # 3. Saisie du bon ISBN : toute la fiche d'identité est réécrite par la
            #    notice qui l'atteste, et la liste d'éditions est réalignée.
            typed_message = FakeMessage()
            typed_message.text = "9782369903086"
            typed_message.answer = AsyncMock()
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

            # 4. Les mots-clés de l'ancien ouvrage sont recalculés : le récap repart
            #    sans eux, la recherche repart seule en arrière-plan.
            await drain(repos["tasks"])
            recap_answer = typed_message.answer.call_args[0][0]
            assert "<b>Mots-clés inclus :</b> La couleur des choses" in recap_answer

            # 5. Création sur l'ISBN saisi, avec l'URL Vinted par ISBN.
            confirm_message = MagicMock(spec=Message)
            confirm_message.edit_text = AsyncMock()
            confirm_callback = MagicMock()
            confirm_callback.data = "alert_create_confirm"
            confirm_callback.message = confirm_message
            confirm_callback.answer = AsyncMock()

            await save_alert(confirm_callback, state, db_user, db_session)

            create_kwargs = repos["alert_repo"].create.call_args.kwargs
            assert create_kwargs["isbn"] == "9782369903086"
            assert "search_start_type=item_page_isbn" in create_kwargs["vinted_search_url"]
            assert "search_text=9782369903086" in create_kwargs["vinted_search_url"]
            assert repos["book_repo"].create.call_args.kwargs["isbn13"] == "9782369903086"
            repos["schedule_pricing"].assert_called_once_with(
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
    client = fake_deepseek(deepseek_payload())
    repos = repositories(ISBN_INCONNU)

    message, loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()

    patches = patched(repos, fake_bnf(resolution), client)
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state)
        await drain(repos["tasks"])

        await open_isbn_edit_field(state)
        typed_message = FakeMessage()
        typed_message.text = ISBN_INCONNU
        typed_message.answer = AsyncMock()
        await process_creation_edit_input(typed_message, state)

        assert state.data["isbn"] == ISBN_INCONNU
        # Rien ne rattache plus l'ancien auteur au nouvel ISBN : le récap le dit.
        assert state.data["author"] is None
        assert state.data["publisher"] is None
        assert state.data["edition_alternatives"] == []
        typed_answer = typed_message.answer.call_args[0][0]
        assert "<b>ISBN :</b> <code>" + ISBN_INCONNU + "</code>" in typed_answer
        assert "ISBN saisi, non confirmé par la BNF" in typed_answer
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
    client = fake_deepseek(deepseek_payload())
    repos = repositories(BNF_EDITION.isbn13)

    message, loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()

    patches = patched(repos, fake_bnf(resolution), client)
    with patch(
        "app.bot.handlers.alerts_helpers.BookMetadataService",
        return_value=fake_bnf(resolution, conflicting_isbn=ISBN_AUTRE_LIVRE),
    ):
        for entered in patches:
            entered.start()
        try:
            await start_add_alert(message, state)
            await drain(repos["tasks"])

            await open_isbn_edit_field(state)
            typed_message = FakeMessage()
            typed_message.text = ISBN_AUTRE_LIVRE
            typed_message.answer = AsyncMock()
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
async def test_add_end_to_end_homonym_title_attaches_nothing_until_chosen():
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
        "app.bot.handlers.alerts_helpers.BookMetadataService",
        return_value=fake_bnf(homonym),
    ):
        for entered in patches:
            entered.start()
        try:
            await start_add_alert(message, state)
            await drain(repos["tasks"])

            recap = recap_text(loading_message)
            assert "<b>ISBN :</b> À confirmer" in recap
            # La note est échappée par le récap : on compare sur un fragment sans apostrophe.
            assert "⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre." in recap
            # Aucune donnée du roman : ni l'ISBN, ni l'auteur, ni l'éditeur.
            assert "9782214080627" not in recap
            assert "Rosemonde Ahou de Saintange" not in recap
            assert "La Pensée universelle" not in recap

            keyboard = loading_message.edit_text.call_args[1]["reply_markup"]
            buttons = work_buttons(keyboard)
            # Un bouton tient le nom de famille et le millésime : l'éditeur complet
            # ne discrimine pas deux homonymes et se faisait tronquer par Telegram.
            assert [button.text for button in buttons] == [
                "1️⃣ Saintange · 1989",
                "2️⃣ Nef · 2025",
            ]

            # L'utilisateur désigne la BD : la fiche bascule sur elle.
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
async def test_add_end_to_end_typed_isbn_beats_model_proposal():
    """Un ISBN saisi dans la commande prime sur celui proposé par le modèle."""
    resolution = BookResolution(best=BNF_ALTERNATIVE)
    bnf = fake_bnf(resolution, verified_isbn=BNF_EDITION.isbn13)
    client = fake_deepseek(deepseek_payload())
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

        bnf.verify_isbn.assert_awaited_once_with("9782369903086", "La couleur des choses")

        recap = recap_text(loading_message)
        assert "<b>ISBN :</b> <code>9782369903086</code>" in recap
        assert ISBN_AUTRE_LIVRE not in recap
    finally:
        for entered in reversed(patches):
            entered.stop()


@pytest.mark.asyncio
async def test_add_natural_add_shows_the_book_found_before_looking_for_variants():
    """`/add` progresse en deux temps : l'ouvrage trouvé, puis les variantes.

    Le message d'attente est mis à jour sur place : l'utilisateur voit l'étape en
    cours au lieu d'un silence entre la recherche et le récap.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    bnf = fake_bnf(resolution)
    client = fake_deepseek(deepseek_payload())
    repos = repositories(BNF_EDITION.isbn13)

    message, loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()

    patches = patched(repos, bnf, client)
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state, AsyncMock(), User(
            id=uuid.uuid4(), telegram_user_id=123
        ))

        # Le récap n'est pas encore publié : le handler a rendu la main sur le jalon.
        # Le récap complet attend la recherche, il ne bloque pas le retour.
        assert loading_message.edit_text.await_count == 1
        assert "🔎 <b>La couleur des choses</b> identifié" in (
            loading_message.edit_text.await_args[0][0]
        )

        await drain(repos["tasks"])

        # Deux éditions successives : le jalon, puis le récap final.
        assert loading_message.edit_text.await_count == 2
        milestone = loading_message.edit_text.await_args_list[0][0][0]
        assert "🔎 <b>La couleur des choses</b> identifié" in milestone
        assert "<i>Martin Panchaud · Çà et là</i>" in milestone
        assert "Recherche des variantes de frappe et des exclusions" in milestone

        milestone_kb = loading_message.edit_text.await_args_list[0][1]["reply_markup"]
        assert [
            button.text for row in milestone_kb.inline_keyboard for button in row
        ] == ["🔍 Variantes & exclusions en cours..."]

        recap = recap_text(loading_message)
        # La carte est celle du brouillon : un titre unique, aucun en-tête de parcours.
        # C'est ce qui permet de la ré-afficher à l'identique après un changement
        # d'édition.
        assert recap.startswith(f"{RECAP_TITLE}\n\n<b>ISBN :</b> <code>9782369903086</code>")
        assert not any(banner in recap for banner in ROUTE_BANNERS)
    finally:
        for entered in reversed(patches):
            entered.stop()


@pytest.mark.asyncio
async def test_add_natural_add_applies_variants_without_asking_for_a_click():
    """Les variantes trouvées sont déjà dans le récap : aucun bouton ne les relance.

    Le recalcul manuel a disparu du clavier parce qu'il ne peut plus rien apporter
    que le brouillon ne contienne déjà, et qu'il douterait de l'automatisme.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    bnf = fake_bnf(resolution)
    client = fake_deepseek(deepseek_payload())
    repos = repositories(BNF_EDITION.isbn13)

    message, loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()

    patches = patched(repos, bnf, client)
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state, AsyncMock(), User(
            id=uuid.uuid4(), telegram_user_id=123
        ))
        await drain(repos["tasks"])

        recap = recap_text(loading_message)
        assert "<b>Variantes :</b> couleurdeschoses" in recap
        assert "<b>Mots-clés exclus :</b> keeping two" in recap
        assert "Valide les critères ci-dessus avant de créer l'alerte." in recap

        keyboard = loading_message.edit_text.call_args[1]["reply_markup"]
        assert [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ].count("alert_create_ai_variants") == 0
    finally:
        for entered in reversed(patches):
            entered.stop()


@pytest.mark.asyncio
async def test_confirmation_shows_market_price_without_the_marketplace_name():
    """La confirmation annonce la cote, pas la plateforme qui l'a servie.

    Trois choix-tenus de l'affichage de création :

    - le prix du marché est donné seul, sans le nom du fournisseur ;
    - le prix bas constaté est donné sans date — `historical_low_at` est réécrit à
      chaque rafraîchissement de la cote, elle daterait de la création et non du prix ;
    - le pied annonce la notification, sans la promesse « 24/24 sur Vinted ».

    Le livre est déjà au catalogue et déjà coté : la cotation tourne en arrière-plan,
    la confirmation n'affiche donc que ce qui est déjà en base.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    bnf = fake_bnf(resolution)
    client = fake_deepseek(deepseek_payload())
    repos = repositories(BNF_EDITION.isbn13, already_catalogued=True)

    message, loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    patches = patched(repos, bnf, client)
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state, db_session, db_user)
        await drain(repos["tasks"])

        confirm_message = MagicMock(spec=Message)
        confirm_message.edit_text = AsyncMock()
        confirm_callback = MagicMock()
        confirm_callback.data = "alert_create_confirm"
        confirm_callback.message = confirm_message
        confirm_callback.answer = AsyncMock()

        await save_alert(confirm_callback, state, db_user, db_session)
        confirmation = confirm_message.edit_text.call_args[0][0]

        # La cote et le bas constaté, tous deux déjà connus du catalogue.
        assert "<b>Prix du marché :</b> 18,00 €" in confirmation
        assert "<b>Prix bas constaté :</b> 16,50 €" in confirmation
        # Ni la plateforme, ni une date qui daterait du moment de la création.
        assert "Momox" not in confirmation
        assert "chasse_aux_livres" not in confirmation
        assert "Chasse" not in confirmation
        assert "Surveillance active" not in confirmation
        assert "24/24" not in confirmation
        # Le pied conservé.
        assert confirmation.endswith(
            "<i>Les annonces correspondant à ces critères déclencheront une alerte "
            "Telegram.</i>"
        )

        # La confirmation reprend la fiche, même ordre et mêmes émojis que le récap.
        assert confirmation.startswith("✅ <b>Alerte créée</b>")
        assert "<b>Titre :</b> La couleur des choses" in confirmation
        assert f"<b>Auteur :</b> {BNF_EDITION.author}" in confirmation
        assert f"<b>Éditeur :</b> {BNF_EDITION.publisher}" in confirmation
        assert "<b>Prix maximum :</b> 12,00 €" in confirmation
        # Les états complets, libellés du référentiel ItemCondition.
        conditions_ligne = "<b>États acceptés :</b> NAE, NSE, TBE"
        assert conditions_ligne in confirmation
    finally:
        for entered in reversed(patches):
            entered.stop()


@pytest.mark.asyncio
async def test_add_natural_add_persists_variants_and_exclusions_with_the_alert():
    """Les variantes trouvées en arrière-plan atteignent bien la ligne créée.

    La recherche part après l'affichage du jalon : si son résultat ne redescendait pas
    dans l'état du brouillon, l'alerte serait enregistrée sans variantes — un travail
    d'IA payé puis perdu, sans que rien ne le signale.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    bnf = fake_bnf(resolution)
    client = fake_deepseek(deepseek_payload())
    repos = repositories(BNF_EDITION.isbn13)

    message, _loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    patches = patched(repos, bnf, client)
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state, db_session, db_user)
        await drain(repos["tasks"])

        confirm_message = MagicMock(spec=Message)
        confirm_message.edit_text = AsyncMock()
        confirm_callback = MagicMock()
        confirm_callback.data = "alert_create_confirm"
        confirm_callback.message = confirm_message
        confirm_callback.answer = AsyncMock()

        await save_alert(confirm_callback, state, db_user, db_session)

        create_kwargs = repos["alert_repo"].create.call_args.kwargs
        assert create_kwargs["name"] == "La couleur des choses"
        assert create_kwargs["isbn"] == BNF_EDITION.isbn13
        assert create_kwargs["include_keywords"] == ["La couleur des choses"]
        assert create_kwargs["variants"] == ["couleurdeschoses"]
        assert create_kwargs["exclude_keywords"] == ["keeping two"]

        # La confirmation rejoue les mots-clés qu'elle affiche : ce qui est annoncé est
        # ce qui est stocké. Elle est la carte de récap relue, donc elle porte les mêmes
        # lignes que lui — variantes et exclusions comprises : c'est une seule
        # construction, elles ne peuvent plus manquer d'un côté et pas de l'autre. Les
        # valeurs sont celles écrites en base (passées par le nettoyeur), pas celles du
        # moteur : le brouillon est relu tel qu'il vient d'être enregistré.
        confirmation = confirm_message.edit_text.call_args[0][0]
        keywords_line = f"<b>Mots-clés inclus :</b> {', '.join(create_kwargs['include_keywords'])}"
        assert keywords_line in confirmation
        assert (
            "<b>Variantes :</b> "
            f"{', '.join(create_kwargs['variants'])}" in confirmation
        )
        assert (
            "<b>Mots-clés exclus :</b> "
            f"{', '.join(create_kwargs['exclude_keywords'])}" in confirmation
        )
    finally:
        for entered in reversed(patches):
            entered.stop()


@pytest.mark.asyncio
async def test_add_recap_is_the_same_card_whatever_the_route():
    """Changer d'édition ré-affiche la même carte, pas une autre variante de carte.

    C'est l'invariant demandé : la carte a une mise en forme, unique, et seuls ses
    contenus changent. Le test ne compare donc pas deux rendus « ressemblants » : il
    exige que chaque geste publie exactement `build_creation_recap(état)`, sans
    en-tête de parcours autour.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    bnf = fake_bnf(resolution)
    client = fake_deepseek(deepseek_payload())
    repos = repositories(BNF_EDITION.isbn13)

    message, loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    patches = patched(repos, bnf, client)
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state, db_session, db_user)
        await drain(repos["tasks"])

        recap = recap_text(loading_message)
        assert recap == build_creation_recap(state.data)[0]

        # L'utilisateur bascule sur l'autre édition.
        switch_message = MagicMock(spec=Message)
        switch_message.edit_text = AsyncMock()
        switch_callback = MagicMock()
        switch_callback.data = "al_create_edition:1"
        switch_callback.message = switch_message
        switch_callback.answer = AsyncMock()

        await select_alert_creation_edition(switch_callback, state)

        switch_recap = switch_message.edit_text.call_args[0][0]
        assert switch_recap == build_creation_recap(state.data)[0]

        # Le contenu a bien changé — c'est l'édition suivie qui diffère.
        assert switch_recap != recap
        assert "<code>9782369904151</code>" in switch_recap
        assert "<b>Édition :</b> Editions çà et là · 224 p. · 19 cm" in switch_recap

        # Et rien d'autre : même structure, aucun bandeau, même pied de confirmation.
        for text in (recap, switch_recap):
            assert text.startswith(RECAP_TITLE)
            assert "ISBN :" in text
            assert text.endswith(
                "\n\n<i>Valide les critères ci-dessus avant de créer l'alerte.</i>"
            )
            for banner in ROUTE_BANNERS:
                assert banner not in text

        # Le clavier suit l'état, lui aussi : mêmes boutons, dans le même ordre.
        recap_kb = loading_message.edit_text.call_args[1]["reply_markup"]
        switch_kb = switch_message.edit_text.call_args[1]["reply_markup"]
        assert recap_kb == switch_kb

        # La note de la recherche de variantes survit au changement d'édition.
        assert "Valide les critères ci-dessus" in switch_recap
    finally:
        for entered in reversed(patches):
            entered.stop()


@pytest.mark.asyncio
async def test_series_alert_starts_book_resolution_at_creation():
    """Une série sans ISBN déclenche la résolution de l'ouvrage dès la création.

    Une série surveillée par son nom n'a aucune fiche tant qu'aucun tome n'a été
    identifié : sans cette résolution, sa première annonce partait sans aucune ligne
    de cote. La résolution part par le **nom de l'alerte** (+ auteur), et cote le
    livre qu'elle lie — la prochaine annonce aura de quoi comparer. La confirmation,
    elle, n'annonce toujours aucune cote : il n'y a encore rien à coter. Elle relit en
    revanche la carte de récap telle quelle, indication de série comprise : c'est le
    même drapeau du brouillon qui décide, donc les deux écrans ne peuvent pas se
    contredire.
    """
    repos = repositories("9782369904151")
    state = DraftState()
    state.data = {
        "category_slug": "bd_comics",
        "name": "Blacksad",
        "author": "Juan Díaz Canales",
        "isbn": None,
        "is_series": True,
        "include_keywords": ["blacksad"],
        "max_item_price_cents": 1500,
    }
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    confirm_message = MagicMock(spec=Message)
    confirm_message.edit_text = AsyncMock()
    confirm_callback = MagicMock()
    confirm_callback.data = "alert_create_confirm"
    confirm_callback.message = confirm_message
    confirm_callback.answer = AsyncMock()

    with patch(
        "app.use_cases.ingest_listing.link_books_from_bnf", new_callable=AsyncMock
    ) as bnf:
        patches = patched(repos, MagicMock(), MagicMock())
        for entered in patches:
            entered.start()
        try:
            await save_alert(confirm_callback, state, db_user, db_session)
            await drain(repos["tasks"])
        finally:
            for entered in reversed(patches):
                entered.stop()

    repos["book_repo"].create.assert_not_called()
    # La cotation se déclenche dans la résolution de fond, pas directement ici.
    repos["schedule_pricing"].assert_not_called()
    bnf.assert_awaited_once()
    args, kwargs = bnf.call_args
    assert args[0] == "Blacksad"
    assert kwargs.get("author") == "Juan Díaz Canales"
    # La série est demandée en entier : ses tomes sont liés et cotés d'un coup.
    assert kwargs.get("series") is True

    confirmation = confirm_message.edit_text.call_args[0][0]
    assert "✅ <b>Alerte créée</b>" in confirmation
    assert "<b>Titre :</b> Blacksad" in confirmation
    # La confirmation reprend le récap ligne pour ligne : l'indication de série est la
    # même phrase, à la même place — sous le titre, en italique, isolée.
    confirm_lines = confirmation.splitlines()
    assert confirm_lines[0] == "✅ <b>Alerte créée</b>"
    assert confirm_lines[1] == ""
    assert confirm_lines[2] == "<i>Série multi-tomes (veille sur tous les tomes)</i>"
    assert confirm_lines[3] == ""
    assert confirm_lines[4].startswith("<b>Titre :</b>")
    assert "<b>ISBN :</b>" not in confirmation
    assert PENDING_COTE_LINE not in confirmation
    assert "Prix du marché" not in confirmation
    assert "Prix bas constaté" not in confirmation


@pytest.mark.asyncio
async def test_a_missing_identifier_is_told_in_the_confirmation_too():
    """Sans ISBN, la confirmation dit « introuvable » comme le récap, au lieu de se taire.

    La ligne existait dans le récap et pas dans la confirmation, qui écrivait ses lignes
    à la main : son silence se lisait « l'identifiant a été trouvé ». La carte n'est plus
    qu'une construction, donc les deux écrans portent la même ligne — et aucun livre
    n'ayant été créé, il n'y a rien à coter.
    """
    repos = repositories("")
    state = DraftState()
    state.data = {
        "category_slug": "bd_comics",
        "name": "Dailyeux",
        "author": "Whoogy's",
        "isbn": None,
        "include_keywords": ["dailyeux"],
        "max_item_price_cents": 3000,
    }
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    confirm_message = MagicMock(spec=Message)
    confirm_message.edit_text = AsyncMock()
    confirm_callback = MagicMock()
    confirm_callback.data = "alert_create_confirm"
    confirm_callback.message = confirm_message
    confirm_callback.answer = AsyncMock()

    with patch("app.use_cases.ingest_listing.link_books_from_bnf", new_callable=AsyncMock):
        patches = patched(repos, MagicMock(), MagicMock())
        for entered in patches:
            entered.start()
        try:
            await save_alert(confirm_callback, state, db_user, db_session)
            await drain(repos["tasks"])
        finally:
            for entered in reversed(patches):
                entered.stop()

    confirmation = confirm_message.edit_text.call_args[0][0]
    assert "<b>ISBN :</b> introuvable" in confirmation
    assert "<b>Titre :</b> Dailyeux" in confirmation
    assert PENDING_COTE_LINE not in confirmation
    repos["book_repo"].create.assert_not_called()


@pytest.mark.asyncio
async def test_the_draft_note_stays_on_the_recap_and_never_reaches_the_confirmation():
    """Le brouillon peut nommer son bouton : l'écran d'après ne repropose pas ce geste.

    C'est la seule information que le récap et la confirmation ne partagent pas, et elle
    l'est par décision : la note renvoie au bouton « ✨ Variantes & Exclusions IA », qui
    n'est plus sous la carte une fois l'alerte créée. La carte, elle, reste la même — la
    confirmation écarte une donnée du brouillon, elle ne réécrit pas son rendu.

    La note est celle que le code produit pour un moteur muet, pas une copie de son
    texte : une phrase recopiée ici continuerait de passer alors que le récap affiche
    autre chose.
    """
    from app.bot.handlers.alerts_helpers import keyword_note

    note = keyword_note(KeywordVariantsOutcome.unanswered())
    assert note is not None
    repos = repositories(BNF_EDITION.isbn13)
    state = DraftState()
    state.data = {
        "category_slug": "bd_comics",
        "name": "La couleur des choses",
        "author": "Martin Panchaud",
        "isbn": BNF_EDITION.isbn13,
        "include_keywords": ["la couleur des choses"],
        "max_item_price_cents": 1200,
        "keyword_note": note,
    }
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    recap, _keyboard = build_creation_recap(state.data)
    assert note in recap

    confirm_message = MagicMock(spec=Message)
    confirm_message.edit_text = AsyncMock()
    confirm_callback = MagicMock()
    confirm_callback.data = "alert_create_confirm"
    confirm_callback.message = confirm_message
    confirm_callback.answer = AsyncMock()

    patches = patched(repos, MagicMock(), MagicMock())
    for entered in patches:
        entered.start()
    try:
        await save_alert(confirm_callback, state, db_user, db_session)
    finally:
        for entered in reversed(patches):
            entered.stop()

    confirmation = confirm_message.edit_text.call_args[0][0]
    assert note not in confirmation
    assert "Variantes & Exclusions IA" not in confirmation
    # La fiche, elle, est bien relue : les deux écrans restent la même carte.
    assert "<b>Titre :</b> La couleur des choses" in confirmation


@pytest.mark.asyncio
@pytest.mark.parametrize("draft_name", [None, ""])
async def test_the_card_names_the_alert_exactly_as_the_row_is_named(draft_name):
    """Un brouillon sans nom ne fait pas tomber la confirmation, et ne ment pas sur le nom.

    `save_alert` enregistre « Alerte » quand le brouillon n'en porte aucun ; la carte, elle,
    lisait la clé brute — le repli de `dict.get` ne couvre que la clé absente. Un nom nul
    levait donc dans `html.escape`, et un nom vide annonçait `Titre :` sans titre sous une
    alerte nommée « Alerte ». La carte décrit la ligne qui vient d'être écrite : elle suit
    le même repli, et le récap comme la confirmation disent le nom enregistré.
    """
    repos = repositories(BNF_EDITION.isbn13)
    state = DraftState()
    state.data = {
        "category_slug": "bd_comics",
        "name": draft_name,
        "isbn": BNF_EDITION.isbn13,
        "include_keywords": ["la couleur des choses"],
        "max_item_price_cents": 1200,
    }
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    confirm_message = MagicMock(spec=Message)
    confirm_message.edit_text = AsyncMock()
    confirm_callback = MagicMock()
    confirm_callback.data = "alert_create_confirm"
    confirm_callback.message = confirm_message
    confirm_callback.answer = AsyncMock()

    # Le récap, affiché juste avant la validation, dit déjà le nom qui sera enregistré.
    recap, _keyboard = build_creation_recap(state.data)
    assert "<b>Titre :</b> Alerte" in recap

    patches = patched(repos, MagicMock(), MagicMock())
    for entered in patches:
        entered.start()
    try:
        await save_alert(confirm_callback, state, db_user, db_session)
    finally:
        for entered in reversed(patches):
            entered.stop()

    assert repos["alert_repo"].create.call_args.kwargs["name"] == "Alerte"
    confirmation = confirm_message.edit_text.call_args[0][0]
    assert "<b>Titre :</b> Alerte" in confirmation


@pytest.mark.asyncio
async def test_a_just_created_book_still_gets_its_confirmation():
    """Un livre neuf n'a pas de cache chargé : la confirmation ne doit pas en dépendre.

    `BookRepository.create` rend un objet dont `market_cache` n'est pas chargé. Le
    lire levait `MissingGreenlet` **après** le commit et le vidage du brouillon :
    l'alerte existait, mais aucune confirmation n'était jamais envoyée. La lecture
    passe donc par `get_by_id`, qui charge la relation.
    """
    repos = repositories("9782369903086", lazy_created_book=True)
    state = DraftState()
    state.data = {
        "category_slug": "bd_comics",
        "name": "Le démon de la colline au loup",
        "isbn": "9782369903086",
        "author": "Nicolas Fébule",
        "include_keywords": ["le démon de la colline au loup"],
        "max_item_price_cents": 2000,
    }
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    confirm_message = MagicMock(spec=Message)
    confirm_message.edit_text = AsyncMock()
    confirm_callback = MagicMock()
    confirm_callback.data = "alert_create_confirm"
    confirm_callback.message = confirm_message
    confirm_callback.answer = AsyncMock()

    patches = patched(repos, MagicMock(), MagicMock())
    for entered in patches:
        entered.start()
    try:
        await save_alert(confirm_callback, state, db_user, db_session)
    finally:
        for entered in reversed(patches):
            entered.stop()

    confirmation = confirm_message.edit_text.call_args[0][0]
    assert "✅ <b>Alerte créée</b>" in confirmation
    assert "<b>Titre :</b> Le démon de la colline au loup" in confirmation
    assert PENDING_COTE_LINE in confirmation
    # Le livre de la confirmation est relu par son id, jamais l'objet rendu par `create`.
    repos["book_repo"].get_by_id.assert_awaited_once()


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


# ---------------------------------------------------------------------------
# Parcours reel, ecrans observes : ce que l'utilisateur lit, dans l'ordre.
#
# Les verrous ci-dessus regardent la carte (`creation_card`) ; ceux-ci regardent les
# **messages Telegram**, en traversant les vrais handlers du premier message à la
# confirmation. Les attentes y sont ecrites telles que la demande les formule, pas
# telles que le rendu les produit : deux avertissements cote a cote, un ⚠️ a la place
# de la loupe, aucun blanc avant « Prix maximum », et « Non renseigne » seulement
# quand l'ouvrage n'est identifie par personne.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_two_warnings_arrive_side_by_side_on_the_recap_and_the_confirmation():
    """Capture 1, demande d'origine : les deux avertissements en tête, côte à côte.

    « Le message “L'IA n'a trouvé aucune variante de frappe” le mets donc en dessous de
    l'information de la BNF » et « un emoji warning plutôt qu'une loupe ». Le parcours
    est celui d'un premier utilisateur — `/add` en langage naturel, la recherche de
    mots-clés part en arrière-plan et le récap se complète sur place — et l'écran
    « Alerte créée » relit la même carte. Ce qui est comparé ici est **le message
    envoyé**, dans l'ordre exact de ses lignes.
    """
    from app.bot.handlers.alerts_helpers import RECAP_TITLE

    roman = BookMetadata(
        title="Les Enfants perdus",
        author="Nadine Diamant",
        publisher="La Pensée universelle",
        isbn13="9782214080627",
        publication_year=1992,
        recommended_includes=["les enfants perdus"],
        recommended_excludes=[],
        recommended_variants=[],
    )
    bande = BookMetadata(
        title="Les Enfants perdus",
        author="Nef",
        publisher="Éditions du Chêne",
        isbn13="9782810206087",
        publication_year=2025,
    )
    homonyme = BookResolution(best=roman, other_works=[bande])
    client = fake_deepseek(
        {
            "name": "Les Enfants perdus",
            "category_slug": "bd_comics",
            "isbn13": None,
            "author": "Neyef",
            "include_keywords": ["Les Enfants perdus"],
            "max_items": None,
            "max_price_cents": 1200,
            "is_series": False,
        }
    )
    repos = repositories(ISBN_INCONNU)
    message, loading_message = message_with_recap("/add les enfants perdus neyef, 12€")
    state = DraftState()

    # L'avertissement d'ambiguïté produit par l'autorité (contrat d'ADR-028, déjà
    # vérifié sur `ambiguity_note`), puis celui du moteur : ce que l'utilisateur lit.
    note_bnf = "⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre."
    note_ia = "⚠️ L'IA n'a trouvé aucune variante de frappe pour cet ouvrage."

    patches = patched(repos, fake_bnf(homonyme), client)
    # Le moteur apporte des exclusions mais aucune variante de frappe : c'est la
    # deuxième ligne de la capture.
    keywords = patch(
        "app.bot.handlers.alerts_create.keyword_variants_module."
        "generate_variants_and_exclusions",
        new_callable=AsyncMock,
        return_value=KeywordVariantsOutcome(
            variants=[], exclusions=["coffret"], answered=True
        ),
    )
    for entered in patches:
        entered.start()
    keywords.start()
    try:
        await start_add_alert(message, state)
        await drain(repos["tasks"])
    finally:
        keywords.stop()
        for entered in reversed(patches):
            entered.stop()

    ecrans = [appel[0][0] for appel in loading_message.edit_text.call_args_list]
    recap = ecrans[-1]
    lines = recap.splitlines()

    assert lines[0] == RECAP_TITLE
    assert lines[1] == ""
    # Les deux avertissements se suivent : aucun blanc ne les sépare.
    assert lines[2] == f"<i>{html.escape(note_bnf)}</i>"
    assert lines[3] == f"<i>{note_ia}</i>"
    # … et chacune **une seule fois**. Elles ont d'abord vécu sous les critères, et la
    # demande était de ne plus avoir à faire l'aller-retour. Un second passage en bas de
    # carte laisserait ce bloc-ci intact : seule l'unicité dit qu'il n'y en a pas deux.
    assert recap.count(f"<i>{html.escape(note_bnf)}</i>") == 1
    assert recap.count(f"<i>{note_ia}</i>") == 1
    # … et un seul blanc les sépare de la liste des informations.
    assert lines[4] == ""
    assert lines[5].startswith("<b>ISBN :</b>")
    assert "<b>Auteur :</b> Neyef" in lines
    # L'ouvrage est décrit par le modèle : aucun trou d'identité n'est annoncé.
    # « Non renseigné » ne se dit que quand **personne** n'identifie l'ouvrage.
    assert "Non renseigné" not in recap

    # « supprime l'espace entre auteur et prix maximum »
    prix = index_of_line(lines, "<b>Prix maximum :</b>")
    assert lines[prix - 1] != "", "aucune ligne vide avant « Prix maximum »"
    assert lines[prix - 1].startswith("<b>")

    # La loupe ne signale plus un manque ; le jalon d'identification, lui, la garde.
    assert "🔎" not in recap
    assert any("🔎" in ecran for ecran in ecrans[:-1]), "le jalon garde sa loupe"

    # « Alerte créée » relit la même carte : les **deux** avertissements, à la même
    # place. Le verdict du moteur a été rendu : il ne nomme aucun bouton et ne parle
    # d'aucune création à venir, donc rien ne justifie de le taire ici.
    confirm_callback, confirm_message = confirm_press()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    confirmation_repos = repositories(ISBN_INCONNU)
    patches = patched(confirmation_repos, MagicMock(), MagicMock())
    for entered in patches:
        entered.start()
    try:
        await save_alert(confirm_callback, state, db_user, AsyncMock())
        # La création programme la liaison BNF : la laisser pendante ferait d'une
        # coroutine jamais attendue un avertissement qui pollue les tests voisins.
        await drain(confirmation_repos["tasks"])
    finally:
        for entered in reversed(patches):
            entered.stop()

    confirmation = confirm_message.edit_text.call_args[0][0]
    confirm_lines = confirmation.splitlines()
    assert confirm_lines[0] == "✅ <b>Alerte créée</b>"
    assert confirm_lines[1] == ""
    # Le même bloc de notes, dans le même ordre, séparé de la liste par un seul blanc.
    assert confirm_lines[2] == f"<i>{html.escape(note_bnf)}</i>"
    assert confirm_lines[3] == f"<i>{note_ia}</i>"
    assert confirmation.count(f"<i>{html.escape(note_bnf)}</i>") == 1
    assert confirmation.count(f"<i>{note_ia}</i>") == 1
    assert confirm_lines[4] == ""
    assert confirm_lines[5].startswith("<b>ISBN :</b>")
    assert "Non renseigné" not in confirmation
    prix = index_of_line(confirm_lines, "<b>Prix maximum :</b>")
    assert confirm_lines[prix - 1] != "", "aucune ligne vide avant « Prix maximum »"
    assert "🔎" not in confirmation


@pytest.mark.asyncio
async def test_the_manual_journey_creates_the_alert_when_no_source_knows_the_work():
    """Capture 2, de bout en bout : `/add` → Livres → titre → prix → alerte créée.

    Aucune source ne connaît l'ouvrage (un livre sorti la semaine dernière n'est dans
    aucun catalogue). Le parcours reste complet — la saisie manuelle n'est plus un
    cul-de-sac —, la carte ouvre sur l'avertissement et montre les trous au lieu de
    les laisser disparaître. Les six messages du parcours sont de vrais handlers.
    """
    from app.bot.handlers.alerts_helpers import UNIDENTIFIED_WORK_NOTE

    class _Service:
        """Aucune autorité ne rend de notice : ni la BNF, ni OpenLibrary, ni Google."""

        async def resolve_input(self, _query):
            return None

    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    category_repo = MagicMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    alert_repo = MagicMock()
    alert_repo.find_existing = AsyncMock(return_value=None)

    # 1. `/add` sans rien d'autre : le bot demande la catégorie.
    depart = incoming_message("/add")
    await start_add_alert(depart, state)
    assert "Choisis la catégorie" in depart.answer.call_args[0][0]

    # 2. L'utilisateur choisit « Livres » : le bot demande le titre.
    choix = MagicMock()
    choix.data = "add_cat:bd_comics"
    choix.message = MagicMock()
    choix.message.edit_text = AsyncMock()
    choix.answer = AsyncMock()
    await handle_category_selected(choix, state, db_session, db_user)
    assert "Alerte Livres & BD" in choix.message.edit_text.call_args[0][0]

    # 3. Il tape le titre : aucune source ne le connaît, la carte s'ouvre sur le prix.
    saisie = incoming_message("whoogys dailycieux")
    with (
        patch(
            "app.bot.handlers.alerts_helpers.BookMetadataService", return_value=_Service()
        ),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=alert_repo),
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=category_repo),
    ):
        await handle_query_input(saisie, state, db_session, db_user)

    ecran_prix = saisie.answer.return_value.edit_text.call_args[0][0]
    assert f"<i>{UNIDENTIFIED_WORK_NOTE}</i>" in ecran_prix
    assert "<b>Auteur :</b> Non renseigné" in ecran_prix

    # 4. Il donne son prix maximum : le récap se pose, prêt à valider.
    prix_saisi = incoming_message("30")
    await process_max_price_input(prix_saisi, state, db_session)

    recap = prix_saisi.answer.call_args[0][0]
    lines = recap.splitlines()
    assert lines[2] == f"<i>{UNIDENTIFIED_WORK_NOTE}</i>"
    assert lines[3] == ""
    assert lines[4] == "<b>ISBN :</b> introuvable"
    assert lines[5] == "<b>Titre :</b> whoogys dailycieux"
    assert lines[6] == "<b>Auteur :</b> Non renseigné"
    assert lines[7] == "<b>Éditeur :</b> Non renseigné"
    assert lines[8] == "<b>Prix maximum :</b> 30,00 €"
    assert "🔎" not in recap

    # 5. « Créer l'alerte » : la confirmation dit la même chose, et l'alerte existe.
    confirm_callback, confirm_message = confirm_press()
    repos = repositories(ISBN_INCONNU)
    patches = patched(repos, MagicMock(), MagicMock())
    for entered in patches:
        entered.start()
    try:
        await save_alert(confirm_callback, state, db_user, db_session)
        await drain(repos["tasks"])
    finally:
        for entered in reversed(patches):
            entered.stop()

    repos["alert_repo"].create.assert_awaited_once()
    assert repos["alert_repo"].create.call_args.kwargs["name"] == "whoogys dailycieux"
    assert repos["alert_repo"].create.call_args.kwargs["isbn"] is None
    # Aucun livre n'est créé : il n'y a pas d'identifiant à rattacher.
    repos["book_repo"].create.assert_not_called()

    confirmation = confirm_message.edit_text.call_args[0][0]
    confirm_lines = confirmation.splitlines()
    assert confirm_lines[0] == "✅ <b>Alerte créée</b>"
    assert confirm_lines[2] == f"<i>{UNIDENTIFIED_WORK_NOTE}</i>"
    assert "<b>ISBN :</b> introuvable" in confirm_lines
    assert "<b>Auteur :</b> Non renseigné" in confirm_lines
    assert "<b>Éditeur :</b> Non renseigné" in confirm_lines
    assert PENDING_COTE_LINE not in confirmation


@pytest.mark.asyncio
async def test_the_three_keyword_verdicts_warn_instead_of_observing():
    """Le ⚠️ des trois libellés du moteur, observé sur le récap qui part vraiment.

    Trois fins possibles — rien du tout, aucune exclusion, aucune variante — et une
    seule demande : « change l'emoji par un emoji warning plutôt qu'une loupe ». Le
    rendu est celui du parcours réel (`_search_keywords_in_background`), pas celui
    d'une fonction de formatage appelée à la main.
    """
    from app.bot.handlers.alerts_create import _search_keywords_in_background

    attendus = [
        (
            KeywordVariantsOutcome(variants=[], exclusions=[], answered=True),
            "⚠️ L'IA n'a trouvé aucune variante ni exclusion pertinente",
        ),
        (
            KeywordVariantsOutcome(variants=["x"], exclusions=[], answered=True),
            "⚠️ L'IA n'a trouvé aucune exclusion pertinente pour cet ouvrage.",
        ),
        (
            KeywordVariantsOutcome(variants=[], exclusions=["coffret"], answered=True),
            "⚠️ L'IA n'a trouvé aucune variante de frappe pour cet ouvrage.",
        ),
    ]

    for outcome, attendu in attendus:
        state = DraftState()
        brouillon = {
            "name": "La couleur des choses",
            "category_slug": "bd_comics",
            "isbn": "9782369903086",
            "author": "Martin Panchaud",
            "include_keywords": ["la couleur des choses"],
            "exclude_keywords": [],
            "max_item_price_cents": 1200,
        }
        await state.update_data(brouillon)

        loading_message = MagicMock()
        loading_message.edit_text = AsyncMock()
        event = MagicMock()
        event.answer = AsyncMock()

        with patch(
            "app.bot.handlers.alerts_create.keyword_variants_module."
            "generate_variants_and_exclusions",
            new_callable=AsyncMock,
            return_value=outcome,
        ):
            await _search_keywords_in_background(
                loading_msg=loading_message,
                event=event,
                state=state,
                snapshot=dict(brouillon),
                name="La couleur des choses",
                category_slug="bd_comics",
                book_context=None,
            )

        recap = loading_message.edit_text.call_args[0][0]
        assert attendu in recap, outcome
        assert "🔎" not in recap, outcome
        # L'avertissement est en tête, au-dessus de l'ISBN qu'il commente.
        lines = recap.splitlines()
        index_note = next(i for i, line in enumerate(lines) if attendu in line)
        assert lines[index_note].startswith("<i>⚠️")
        assert index_note < index_of_line(lines, "<b>ISBN :</b>")


@pytest.mark.asyncio
async def test_the_confirmation_drops_the_engines_plea_but_keeps_its_verdict():
    """La confirmation n'écarte que le plaidoyer du moteur, plus son verdict.

    Quand le moteur n'a **pas** répondu, sa note nomme le bouton « ✨ Variantes &
    Exclusions IA » — absent du clavier de confirmation — et annonce une alerte « à
    créer » qui vient de l'être : elle n'a pas de sens sur cet écran et reste au
    brouillon. L'autre côté est dans
    `test_the_two_warnings_arrive_side_by_side_on_the_recap_and_the_confirmation` :
    un verdict rendu, lui, se relit partout. Ce test tient la frontière entre les deux.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    client = fake_deepseek(deepseek_payload())
    repos = repositories(BNF_EDITION.isbn13)

    message, loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()

    patches = patched(repos, fake_bnf(resolution), client)
    plea = patch(
        "app.bot.handlers.alerts_create.keyword_variants_module."
        "generate_variants_and_exclusions",
        new_callable=AsyncMock,
        return_value=KeywordVariantsOutcome.unanswered(),
    )
    for entered in patches:
        entered.start()
    plea.start()
    try:
        await start_add_alert(message, state)
        await drain(repos["tasks"])
    finally:
        plea.stop()
        for entered in reversed(patches):
            entered.stop()

    recap = loading_message.edit_text.call_args_list[-1][0][0]
    assert "L'IA n'a pas répondu après une relance" in recap
    # Le moteur n'a pas répondu : la recherche n'est pas définitive.
    assert state.data["keywords_finalized"] is False

    confirm_callback, confirm_message = confirm_press()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    confirm_repos = repositories(BNF_EDITION.isbn13)
    patches = patched(confirm_repos, MagicMock(), MagicMock())
    for entered in patches:
        entered.start()
    try:
        await save_alert(confirm_callback, state, db_user, AsyncMock())
        await drain(confirm_repos["tasks"])
    finally:
        for entered in reversed(patches):
            entered.stop()

    confirmation = confirm_message.edit_text.call_args[0][0]
    assert "L'IA n'a pas répondu" not in confirmation
    # Ni le bouton qu'elle nomme, ni l'alerte « à créer » qu'elle annonce.
    assert "Variantes & Exclusions IA" not in confirmation
    assert "cette alerte sera créée" not in confirmation
    # L'identité, elle, reste : la confirmation n'est pas devenue muette.
    assert "<b>Titre :</b> La couleur des choses" in confirmation
    assert "<b>ISBN :</b> <code>" in confirmation

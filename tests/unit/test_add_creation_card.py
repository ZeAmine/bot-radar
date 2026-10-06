"""Parcours de la carte de création : ce qu'elle annonce, ce qu'elle enregistre.

La carte a une seule construction. Ces parcours la regardent telle qu'elle est publiée
par les vrais handlers — le récap de `/add`, puis la confirmation de `save_alert` — et
vérifient trois choses : ce qu'elle annonce (identité, cote, mots-clés, indication de
série), ce qui redescend du brouillon dans la ligne créée, et ce qu'elle ne redécouvre
pas après coup (livre neuf, brouillon sans nom, identifiant absent). La mise en forme
ligne à ligne est relue par `tests/unit/test_charte_copy.py`.

Le harnais du parcours `/add` est partagé, jamais importé d'un module de test : les
doubles et leurs données dans `tests/add_fakes.py`, leur câblage et les espions dans
`tests/add_journey.py`, la lecture de ce qu'un écran publie dans `tests/screens.py`.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

from app.bot.handlers.alerts_create import save_alert, start_add_alert
from app.bot.handlers.alerts_create_confirm import select_alert_creation_edition
from app.bot.handlers.alerts_helpers import RECAP_TITLE, build_creation_recap
from app.db.models.user import User
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from app.services.book_metadata_service import BookResolution
from app.services.market_price_service import PENDING_COTE_LINE
from tests.add_fakes import (
    BNF_ALTERNATIVE,
    BNF_EDITION,
    ROUTE_BANNERS,
    DraftState,
    deepseek_payload,
    fake_bnf,
    fake_deepseek,
)
from tests.add_journey import drain, patched, repositories
from tests.screens import message_with_recap, recap_text


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

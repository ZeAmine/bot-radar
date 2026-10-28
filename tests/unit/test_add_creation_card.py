"""Parcours de la carte de création : ce qu'elle annonce, ce qu'elle enregistre.

La carte a une seule construction. Ces parcours la regardent telle qu'elle est publiée
par les vrais handlers — le récap de `/add`, puis la confirmation de `save_alert` — et
vérifient trois choses : ce qu'elle annonce (identité, cote, mots-clés, indication de
série), ce qui redescend du brouillon dans la ligne créée, et ce qu'elle ne redécouvre
pas après coup (livre neuf, brouillon sans nom, identifiant absent). La mise en forme
ligne à ligne est relue par `tests/unit/test_charte_copy.py`.

Le harnais (doubles, câblage, lecture des écrans) est partagé : `tests/add_fakes.py`,
`tests/add_journey.py`, `tests/screens.py`.
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
    fake_bnf,
    fake_llm,
    llm_payload,
)
from tests.add_fakes import (
    DraftState as _SharedDraftState,
)
from tests.add_journey import drain, patched
from tests.add_journey import repositories as _shared_repositories
from tests.screens import confirm_press, message_with_recap, recap_text


class DraftState(_SharedDraftState):
    """Contexte local identifié pour le verrou de consommation, données inchangées."""
    def __init__(self):
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage
        super().__init__()
        self.storage = MemoryStorage()
        self.key = StorageKey(bot_id=123456789, chat_id=123, user_id=123)



def cached_market_price():
    """Projection de deux relevés factices livrés comparables (1650 puis 1800)."""
    from datetime import UTC, datetime, timedelta

    from app.core.constants import ComparisonStatus
    from app.db.models.book_market_cache import BookMarketCache
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    from tests.fakes_market_source import source_history_values

    return BookMarketCache(**source_history_values(1650, now),
        id=uuid.uuid4(), book_id=uuid.uuid4(),
        lowest_delivered_price_cents=1800, checked_at=now,
        expires_at=now+timedelta(days=1),
        comparison_status=ComparisonStatus.AVAILABLE, best_provider_name="synthetic",
        historical_low_1y_cents=1650, annual_low_cents=1650, annual_low_observed_at=old,
        annual_low_snapshot_id=uuid.uuid4(), annual_low_provider="synthetic",
        annual_first_at=old, annual_last_at=now, annual_count=2,
        annual_computed_at=now, annual_expires_at=old+timedelta(days=365))


def repositories(*args, **kwargs):
    repos = _shared_repositories(*args, **kwargs)
    book = repos["book_repo"].get_by_id.return_value
    if book.market_cache is not None:
        book.market_cache = cached_market_price()
    return repos


@pytest.mark.asyncio
async def test_add_natural_add_shows_the_book_found_before_looking_for_variants():
    """`/add` progresse en deux temps : l'ouvrage trouvé, puis les variantes.

    Le message d'attente est mis à jour sur place : l'utilisateur voit l'étape en
    cours au lieu d'un silence entre la recherche et le récap.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    bnf = fake_bnf(resolution)
    client = fake_llm(llm_payload())
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

        assert loading_message.edit_text.await_count == 1
        assert "<b>Titre :</b> La couleur des choses" in (
            loading_message.edit_text.await_args[0][0]
        )

        await drain(repos["tasks"])

        assert loading_message.edit_text.await_count == 2
        milestone = loading_message.edit_text.await_args_list[0][0][0]
        assert "<b>Titre :</b> La couleur des choses" in milestone
        assert "<b>Auteur :</b> Martin Panchaud" in milestone
        assert "<b>Éditeur :</b> Çà et là" in milestone
        assert "Analyse des variantes et exclusions en cours" in milestone

        milestone_kb = loading_message.edit_text.await_args_list[0][1]["reply_markup"]
        actions = [button.callback_data.split("@")[0]
                   for row in milestone_kb.inline_keyboard for button in row]
        assert "alert_create_confirm" not in actions
        assert "alert_create_ai_variants" not in actions
        assert "alert_create_edit" in actions
        assert "alert_create_cancel" in actions

        recap = recap_text(loading_message)
        assert recap.startswith(f"{RECAP_TITLE}\n\n")
        assert "<b>ISBN :</b> <code>9782369903086</code>" in recap
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
    client = fake_llm(llm_payload())
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
    - le minimum annuel vient de relevés datés admissibles ; le prix courant
      reste affiché séparément, sans réinterpréter `historical_low_at` ;
    - le pied annonce la notification, sans la promesse « 24/24 sur Vinted ».

    Le livre est déjà au catalogue et déjà coté : la confirmation relit le prix
    conservé après une cotation simulée sans nouvelle donnée.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    bnf = fake_bnf(resolution)
    client = fake_llm(llm_payload())
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

        confirm_callback, confirm_message = confirm_press()

        await save_alert(confirm_callback, state, db_user, db_session)
        confirmation = confirm_message.edit_text.call_args[0][0]

        # Cote en base sans liste d'offres courante : le prix retenu est cité comme
        # dernière offre connue, jamais comme l'offre du jour.
        assert "(dernière offre connue : 18,00 €)" in confirmation
        assert "<b>Prix bas constaté (1 an) :</b> 16,50 € - très bon état +, port inclus" in (
            confirmation
        )
        assert "Momox" not in confirmation
        assert "chasse_aux_livres" not in confirmation
        assert "Chasse" not in confirmation
        assert "Surveillance active" not in confirmation
        assert "24/24" not in confirmation
        assert confirmation.endswith("<i>Tu seras prévenu des annonces correspondantes.</i>")

        assert confirmation.startswith("✅ <b>Alerte créée</b>")
        assert "<b>Titre :</b> La couleur des choses" in confirmation
        assert f"<b>Auteur :</b> {BNF_EDITION.author}" in confirmation
        assert f"<b>Éditeur :</b> {BNF_EDITION.publisher}" in confirmation
        assert "<b>Prix maximum :</b> 12,00 €" in confirmation
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
    client = fake_llm(llm_payload())
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

        confirm_callback, confirm_message = confirm_press()

        await save_alert(confirm_callback, state, db_user, db_session)

        create_kwargs = repos["alert_repo"].create.call_args.kwargs
        assert create_kwargs["name"] == "La couleur des choses"
        assert create_kwargs["isbn"] == BNF_EDITION.isbn13
        assert create_kwargs["include_keywords"] == ["couleur des choses"]
        assert create_kwargs["variants"] == ["couleurdeschoses"]
        assert create_kwargs["exclude_keywords"] == ["keeping two"]

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
    client = fake_llm(llm_payload())
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

        switch_message = MagicMock(spec=Message)
        switch_message.edit_text = AsyncMock()
        switch_callback = MagicMock()
        switch_callback.data = "al_create_edition:1"
        switch_callback.message = switch_message
        switch_callback.answer = AsyncMock()

        await select_alert_creation_edition(switch_callback, state)

        switch_recap = switch_message.edit_text.call_args[0][0]
        assert switch_recap == build_creation_recap(state.data)[0]

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
    livre qu'elle lie avant l'activation. Le double de résolution ne lie aucun
    livre ici : la confirmation n'invente donc aucune cote. Elle relit en
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

    confirm_callback, confirm_message = confirm_press()

    with patch(
        "app.use_cases.link_bd_catalog.link_books_from_bnf", new_callable=AsyncMock
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
    # La résolution attend les cotations de ses tomes, pas de cote unitaire ici.
    repos["price_now"].assert_not_awaited()
    bnf.assert_awaited_once()
    args, kwargs = bnf.call_args
    assert args[0] == "Blacksad"
    assert kwargs.get("author") == "Juan Díaz Canales"
    # La série est demandée en entier : ses tomes sont liés et cotés d'un coup.
    assert kwargs.get("series") is True
    assert kwargs.get("wait_for_prices") is True

    confirmation = confirm_message.edit_text.call_args[0][0]
    assert "✅ <b>Alerte créée</b>" in confirmation
    assert "<b>Titre :</b> Blacksad" in confirmation
    # Indication de série : même phrase que le récap, sous le titre, en italique, isolée.
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

    confirm_callback, confirm_message = confirm_press()

    with patch("app.use_cases.link_bd_catalog.link_books_from_bnf", new_callable=AsyncMock):
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

    confirm_callback, confirm_message = confirm_press()

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

    confirm_callback, confirm_message = confirm_press()

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

    confirm_callback, confirm_message = confirm_press()

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
    assert "<b>Cote :</b> récupération impossible" in confirmation
    # Deux lectures chargées : le livre créé, puis les liens actuels après préparation.
    assert repos["book_repo"].get_by_id.await_count == 1
    repos["book_repo"].get_all_books_for_alert.assert_awaited_once()

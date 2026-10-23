"""Parcours réel, écrans observés : ce que l'utilisateur lit, dans l'ordre.

Les fichiers voisins regardent la carte (`creation_card`) ; celui-ci regarde les
**messages Telegram**, en traversant les vrais handlers du premier message à la
confirmation. Les attentes y sont écrites telles que la demande les formule, pas
telles que le rendu les produit : deux avertissements côte à côte, un ⚠️ à la place de
la loupe, aucun blanc avant « Prix maximum », et « Non renseigné » seulement quand
l'ouvrage n'est identifié par personne.

Le harnais (doubles, câblage, lecture des écrans) est partagé : `tests/add_fakes.py`,
`tests/add_journey.py`, `tests/screens.py`.
"""

import html
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers.alerts_create import (
    handle_category_selected,
    handle_query_input,
    save_alert,
    start_add_alert,
)
from app.bot.handlers.alerts_create_confirm import (
    handle_create_edit_choice,
    process_creation_edit_input,
)
from app.db.models.user import User
from app.domain.enums import BookSearchStatus
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from app.services.book_metadata_service import BookMetadata, BookResolution
from app.services.market_price_service import PENDING_COTE_LINE
from tests.add_fakes import (
    BNF_ALTERNATIVE,
    BNF_EDITION,
    ISBN_INCONNU,
    deepseek_payload,
    fake_bnf,
    fake_deepseek,
)
from tests.add_fakes import DraftState as _DraftState
from tests.add_journey import drain, patched, repositories
from tests.screens import (
    confirm_press,
    incoming_message,
    index_of_line,
    message_with_recap,
)


class DraftState(_DraftState):
    """Identité FSM locale pour exercer la coordination, sans modifier le double partagé."""

    def __init__(self):
        super().__init__()
        self.storage = MemoryStorage()
        self.key = StorageKey(bot_id=1, chat_id=42, user_id=42)


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

    note_bnf = "⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre."
    note_ia = "⚠️ L'IA n'a trouvé aucune variante de frappe pour cet ouvrage."

    patches = patched(repos, fake_bnf(homonyme), client)
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
    assert lines[2] == f"<i>{html.escape(note_bnf)}</i>"
    assert lines[3] == f"<i>{note_ia}</i>"
    assert recap.count(f"<i>{html.escape(note_bnf)}</i>") == 1
    assert recap.count(f"<i>{note_ia}</i>") == 1
    assert lines[4] == ""
    assert lines[5].startswith("<b>ISBN :</b>")
    assert "<b>Auteur :</b> Non renseigné" in lines
    assert "<b>Langue :</b> Non renseigné" in recap
    assert "<b>Année :</b> Non renseigné" in recap

    prix = index_of_line(lines, "<b>Prix maximum :</b>")
    assert lines[prix - 1] != "", "aucune ligne vide avant « Prix maximum »"
    assert lines[prix - 1].startswith("<b>")

    assert "🔎" not in recap
    assert any("🔎" in ecran for ecran in ecrans[:-1]), "le jalon garde sa loupe"

    confirm_callback, confirm_message = confirm_press()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    confirmation_repos = repositories(ISBN_INCONNU)
    patches = patched(confirmation_repos, MagicMock(), MagicMock())
    for entered in patches:
        entered.start()
    try:
        await save_alert(confirm_callback, state, db_user, AsyncMock())
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
    assert "<b>Langue :</b> Non renseigné" in confirmation
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
    from app.services.book_metadata_service import BibliographicReport
    from app.use_cases.book_search import BookSearchResult, result_from_report

    class _Service:
        """Aucune autorité ne rend de notice : ni la BNF, ni OpenLibrary, ni Google."""

        async def search_by_title(self, _title, _author_hint=None, **_options):
            return result_from_report(
                BibliographicReport(kind="title", resolution=None)
            )

        async def search_by_isbn(self, _isbn, _expected_title=None):
            return BookSearchResult(status=BookSearchStatus.NOT_FOUND)

    state = DraftState()
    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    category_repo = MagicMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    alert_repo = MagicMock()
    alert_repo.find_existing = AsyncMock(return_value=None)

    depart = incoming_message("/add")
    await start_add_alert(depart, state)
    assert "Choisis la catégorie" in depart.answer.call_args[0][0]

    choix = MagicMock()
    choix.data = f"add_cat:bd_comics@{(await state.get_data())['creation_token']}"
    choix.message = MagicMock()
    choix.message.edit_text = AsyncMock()
    choix.answer = AsyncMock()
    await handle_category_selected(choix, state, db_session, db_user)
    assert "Alerte Livres & BD" in choix.message.edit_text.call_args[0][0]

    saisie = incoming_message("whoogys dailycieux")
    with (
        patch(
            "app.bot.handlers.alerts_helpers.BookSearchUseCase", return_value=_Service()
        ),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=alert_repo),
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=category_repo),
    ):
        await handle_query_input(saisie, state, db_session, db_user)

    ecran_prix = saisie.answer.return_value.edit_text.call_args[0][0]
    assert f"<i>{UNIDENTIFIED_WORK_NOTE}</i>" in ecran_prix
    assert "<b>Auteur :</b> Non renseigné" in ecran_prix

    edit_callback, _ = confirm_press()
    edit_callback.data = "al_create_edit:price"
    await handle_create_edit_choice(edit_callback, state)
    prix_saisi = incoming_message("30")
    await process_creation_edit_input(prix_saisi, state)

    recap = prix_saisi.answer.call_args[0][0]
    lines = recap.splitlines()
    assert lines[2] == f"<i>{UNIDENTIFIED_WORK_NOTE}</i>"
    assert lines[3] == ""
    assert lines[4] == "<b>ISBN :</b> introuvable"
    assert lines[5] == "<b>Titre :</b> whoogys dailycieux"
    assert lines[6] == "<b>Langue :</b> Non renseigné"
    assert lines[7] == "<b>Auteur :</b> Non renseigné"
    assert lines[8] == "<b>Éditeur :</b> Non renseigné"
    assert lines[9] == "<b>Année :</b> Non renseigné"
    assert lines[10] == "<b>Format :</b> Non renseigné"
    assert lines[11] == "<b>Source :</b> Non renseigné"
    assert lines[12] == "<b>Prix maximum :</b> 30,00 €"
    assert "🔎" not in recap

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
    assert "L'IA n'a pas répondu" in recap
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

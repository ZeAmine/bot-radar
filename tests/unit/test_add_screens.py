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
    fake_bnf,
    fake_llm,
    llm_payload,
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
async def test_homonym_warning_precedes_analysis_and_selected_recap_matches_confirmation():
    """Le choix d'œuvre précède l'analyse et les critères confirmés restent identiques."""
    from app.bot.handlers.alerts_create_confirm import select_alert_creation_work

    roman = BookMetadata(
        title="Les Enfants perdus", author="Nadine Diamant", publisher="La Pensée universelle",
        isbn13="9782214080627", publication_year=1992,
    )
    bande = BookMetadata(
        title="Les Enfants perdus", author="Nef", publisher="Éditions du Chêne",
        isbn13="9782810206087", publication_year=2025,
    )
    resolution = BookResolution(best=roman, other_works=[bande])
    client = fake_llm({
        "name": "Les Enfants perdus", "category_slug": "bd_comics", "isbn13": None,
        "author": "Neyef", "include_keywords": ["Les Enfants perdus"],
        "max_price_cents": 1200, "is_series": False,
    })
    repos = repositories("9782810206087")
    message, screen = message_with_recap("/add les enfants perdus neyef, 12€")
    state = DraftState()
    patches = patched(repos, fake_bnf(resolution), client)
    for boundary in patches:
        boundary.start()
    try:
        await start_add_alert(message, state)
        initial = screen.edit_text.call_args.args[0]
        note = "⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre."
        assert f"<i>{html.escape(note)}</i>" in initial
        assert "<b>Auteur :</b> Non renseigné" in initial
        assert "<b>Langue :</b> Non renseigné" in initial
        assert "<b>Année :</b> Non renseigné" in initial
        assert not repos["tasks"]
        callback, selected_screen = confirm_press()
        callback.data = "al_create_work:1"
        with patch(
            "app.bot.handlers.alerts_create.keyword_variants_module.generate_variants_and_exclusions",
            AsyncMock(return_value=KeywordVariantsOutcome(exclusions=["coffret"], answered=True)),
        ):
            await select_alert_creation_work(callback, state)
            await drain(repos["tasks"])
        recap = selected_screen.edit_text.call_args.args[0]
        assert "<i>Critères enrichis.</i>" in recap
        assert "<b>Mots-clés exclus :</b> coffret" in recap
        assert "<b>Auteur :</b> Nef" in recap
        assert "<b>ISBN :</b> <code>9782810206087</code>" in recap
        assert "<b>Éditeur :</b> Éditions du Chêne" in recap
        assert note not in recap
        lines = recap.splitlines()
        price = index_of_line(lines, "<b>Prix maximum :</b>")
        assert lines[price - 1].startswith("<b>")
        assert lines[price - 1] != ""
        confirm, final_screen = confirm_press()
        await save_alert(confirm, state, User(id=uuid.uuid4(), telegram_user_id=123), AsyncMock())
        await drain(repos["tasks"])
        confirmation = final_screen.edit_text.call_args.args[0]
        for line in lines:
            if line.startswith("<b>") or "Critères enrichis." in line:
                assert line in confirmation
        assert "🔎" not in recap
        assert "🔎" not in confirmation
        assert repos["alert_repo"].create.call_args.kwargs["exclude_keywords"] == ["coffret"]
    finally:
        for boundary in reversed(patches):
            boundary.stop()


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
    assert lines[3] == "<i>Analyse IA indisponible : aucune proposition ajoutée.</i>"
    assert lines[4] == ""
    assert lines[5] == "<b>ISBN :</b> introuvable"
    assert lines[6] == "<b>Titre :</b> whoogys dailycieux"
    assert lines[7] == "<b>Langue :</b> Non renseigné"
    assert lines[8] == "<b>Auteur :</b> Non renseigné"
    assert lines[9] == "<b>Éditeur :</b> Non renseigné"
    assert lines[10] == "<b>Année :</b> Non renseigné"
    assert lines[11] == "<b>Format :</b> Non renseigné"
    assert lines[12] == "<b>Source :</b> Non renseigné"
    assert lines[13] == "<b>Prix maximum :</b> 30,00 €"
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
async def test_the_recap_announces_a_short_enrichment_only_when_something_is_kept():
    """L'indication courte du récap, observée sur le récap qui part vraiment.

    Trois fins possibles — rien du tout, aucune proposition retenue par les filtres,
    une exclusion réellement conservée — et une seule règle : la carte ne dit
    « Critères enrichis. » que si un critère a réellement été ajouté, et ne dit rien
    d'autre quand le moteur a répondu sans apport. Les détails restent dans les
    lignes « Variantes » et « Mots-clés exclus », jamais répétés dans la note.
    Le rendu est celui du parcours réel (`_search_keywords_in_background`), pas celui
    d'une fonction de formatage appelée à la main.
    """
    from app.bot.handlers.alerts_create import _search_keywords_in_background
    from app.bot.labels import CRITERIA_ENRICHED_NOTE

    cas = [
        (KeywordVariantsOutcome(variants=[], exclusions=[], answered=True), False),
        (KeywordVariantsOutcome(variants=["x"], exclusions=[], answered=True), False),
        (KeywordVariantsOutcome(variants=[], exclusions=["coffret"], answered=True), True),
        (
            KeywordVariantsOutcome(variants=["la couleur de chose"], exclusions=[], answered=True),
            True,
        ),
    ]

    for outcome, enrichi in cas:
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
        await state.set_state("AlertCreationState:confirm_creation")

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
        assert ("<i>" + CRITERIA_ENRICHED_NOTE + "</i>" in recap) == enrichi, outcome
        assert recap.count(CRITERIA_ENRICHED_NOTE) == (1 if enrichi else 0), outcome
        # Aucun compte de propositions : les détails vivent dans les lignes de critères.
        assert "supplémentaires retenues" not in recap, outcome
        if enrichi:
            lines = recap.splitlines()
            index_note = next(
                index for index, line in enumerate(lines) if CRITERIA_ENRICHED_NOTE in line
            )
            # L'indication est en tête, au-dessus de la première information de fiche.
            assert index_note < index_of_line(lines, "<b>ISBN :</b>")
            assert lines[index_note] == f"<i>{CRITERIA_ENRICHED_NOTE}</i>"
            if outcome.exclusions:
                assert "<b>Mots-clés exclus :</b> coffret" in recap
        else:
            assert "L'IA n'a pas répondu" not in recap, outcome


async def _run_keyword_analysis(state, outcome, *, snapshot=None):
    """Joue l'analyse différée sur un brouillon et rend le récap édité."""
    from app.bot.handlers.alerts_create import _search_keywords_in_background

    brouillon = snapshot or dict(state.data)
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
            name=str(brouillon.get("name") or ""),
            category_slug=str(brouillon.get("category_slug") or "bd_comics"),
            book_context=None,
        )
    return loading_message.edit_text.call_args[0][0]


def _keyword_draft(**overrides):
    return {
        "name": "La couleur des choses",
        "category_slug": "bd_comics",
        "isbn": "9782369903086",
        "author": "Martin Panchaud",
        "include_keywords": ["la couleur des choses"],
        "exclude_keywords": [],
        "max_item_price_cents": 1200,
        **overrides,
    }


@pytest.mark.asyncio
async def test_a_later_analysis_completes_the_criteria_without_repeating_the_note():
    """Deux analyses successives fusionnent les exclusions et n'annoncent qu'une fois.

    L'indication décrit l'état des critères, pas chaque passage du moteur : elle reste
    unique quand une analyse tardive complète la liste déjà retenue.
    """
    from app.bot.labels import CRITERIA_ENRICHED_NOTE

    state = DraftState()
    brouillon = _keyword_draft()
    await state.update_data(brouillon)
    await state.set_state("AlertCreationState:confirm_creation")

    await _run_keyword_analysis(
        state, KeywordVariantsOutcome(exclusions=["coffret"], answered=True)
    )
    recap = await _run_keyword_analysis(
        state,
        KeywordVariantsOutcome(exclusions=["coffret", "artbook"], answered=True),
        snapshot={**brouillon, "exclude_keywords": ["coffret"]},
    )

    assert state.data["exclude_keywords"] == ["coffret", "artbook"]
    assert recap.count(CRITERIA_ENRICHED_NOTE) == 1
    assert "<b>Mots-clés exclus :</b> coffret, artbook" in recap


@pytest.mark.asyncio
async def test_exclusions_written_by_the_user_survive_the_analysis():
    """Une liste écrite à la main n'est ni remplacée ni comptée comme un apport du moteur."""
    from app.bot.labels import CRITERIA_ENRICHED_NOTE

    state = DraftState()
    brouillon = _keyword_draft(
        exclude_keywords=["artbook"], keyword_manual_fields=["exclude_keywords"],
    )
    await state.update_data(brouillon)
    await state.set_state("AlertCreationState:confirm_creation")

    recap = await _run_keyword_analysis(
        state,
        KeywordVariantsOutcome(exclusions=["coffret", "boite vide"], answered=True),
        snapshot=brouillon,
    )

    assert state.data["exclude_keywords"] == ["artbook"]
    assert "<b>Mots-clés exclus :</b> artbook" in recap
    assert "coffret" not in recap
    assert CRITERIA_ENRICHED_NOTE not in recap


@pytest.mark.asyncio
async def test_an_empty_exclusion_list_is_an_expected_answer():
    """Zéro exclusion acceptée : le brouillon reste intact et rien ne s'annonce."""
    from app.bot.labels import CRITERIA_ENRICHED_NOTE

    state = DraftState()
    brouillon = _keyword_draft()
    await state.update_data(brouillon)
    await state.set_state("AlertCreationState:confirm_creation")

    recap = await _run_keyword_analysis(
        state, KeywordVariantsOutcome(variants=[], exclusions=[], answered=True),
        snapshot=brouillon,
    )

    assert state.data["exclude_keywords"] == []
    assert state.data["keywords_finalized"] is True
    assert CRITERIA_ENRICHED_NOTE not in recap
    assert "Mots-clés exclus" not in recap


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
    client = fake_llm(llm_payload())
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

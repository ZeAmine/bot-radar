"""Parcours réel, écrans observés : ce que l'utilisateur lit, dans l'ordre.

Les fichiers voisins regardent la carte (`creation_card`) ; celui-ci regarde les
**messages Telegram**, en traversant les vrais handlers du premier message à la
confirmation. Les attentes y sont écrites telles que la demande les formule, pas
telles que le rendu les produit : deux avertissements côte à côte, un ⚠️ à la place de
la loupe, aucun blanc avant « Prix maximum », et « Non renseigné » seulement quand
l'ouvrage n'est identifié par personne.

Le harnais du parcours `/add` est partagé, jamais importé d'un module de test : les
doubles et leurs données dans `tests/add_fakes.py`, leur câblage et les espions dans
`tests/add_journey.py`, la lecture de ce qu'un écran publie dans `tests/screens.py`.
"""

import html
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.handlers.alerts_create import (
    handle_category_selected,
    handle_query_input,
    process_max_price_input,
    save_alert,
    start_add_alert,
)
from app.db.models.user import User
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from app.services.book_metadata_service import BookMetadata, BookResolution
from app.services.market_price_service import PENDING_COTE_LINE
from tests.add_fakes import (
    BNF_ALTERNATIVE,
    BNF_EDITION,
    ISBN_INCONNU,
    DraftState,
    deepseek_payload,
    fake_bnf,
    fake_deepseek,
)
from tests.add_journey import drain, patched, repositories
from tests.screens import (
    confirm_press,
    incoming_message,
    index_of_line,
    message_with_recap,
)


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

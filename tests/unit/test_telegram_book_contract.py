"""Lot 4 : Telegram consomme le contrat `BookSearchResult` partagé.

Ce que ces tests verrouillent, et que les suites existantes ne couvrent pas :
- la projection d'un résultat vers le brouillon, pour les cinq statuts ;
- la séparation candidat / champs confirmés (aucun ISBN rattaché hors `found`) ;
- la revalidation au clic (œuvre, édition) — un clic ne confirme pas un ISBN ;
- le jeton d'ordre : une réponse bibliographique tardive n'écrase pas la saisie ;
- la même politique pour la saisie manuelle et `natural_add`.

Mocks uniquement : aucun appel réseau, aucune base, aucun LLM.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.alerts_create_confirm import (
    select_alert_creation_edition,
    select_alert_creation_work,
)
from app.bot.handlers.alerts_helpers import (
    draft_updates_from_search,
    process_bd_query_lookup,
    realign_draft_to_isbn,
)
from app.domain.enums import BookSearchStatus, ProviderQueryStatus
from app.services.book_metadata_service import (
    AUTORITE_BNF,
    AUTORITE_OPENLIBRARY,
    BibliographicReport,
    BookMetadata,
    BookResolution,
    ProviderAttempt,
)
from app.use_cases.book_search import (
    BookEditionChoice,
    BookSearchResult,
    BookWorkChoice,
    metadata_from_result,
    result_from_report,
)


def _found() -> BookSearchResult:
    return BookSearchResult(
        status=BookSearchStatus.FOUND,
        editions=(
            BookEditionChoice(
                isbn13="9782369903086", publisher="Çà et là", provenance=AUTORITE_BNF
            ),
        ),
        reference=BookEditionChoice(
            isbn13="9782369903086", publisher="Çà et là", provenance=AUTORITE_BNF
        ),
        confirmed=BookWorkChoice(
            title="La couleur des choses",
            author="Martin Panchaud",
            publisher="Çà et là",
            isbn13="9782369903086",
            provenance=AUTORITE_BNF,
        ),
    )


def _partial_sans_isbn() -> BookSearchResult:
    return BookSearchResult(
        status=BookSearchStatus.PARTIAL,
        works=(BookWorkChoice(title="Whoogy's Dailycieux", provenance=AUTORITE_BNF),),
        provenance=AUTORITE_BNF,
        note="Ouvrage nommé sans édition confirmée.",
    )


def _partial_repli_panne() -> BookSearchResult:
    return BookSearchResult(
        status=BookSearchStatus.PARTIAL,
        works=(
            BookWorkChoice(
                title="Blacksad",
                author="Canales",
                isbn13="9782369903086",
                provenance=AUTORITE_OPENLIBRARY,
            ),
        ),
        consulted=(AUTORITE_BNF, AUTORITE_OPENLIBRARY),
        unavailable=(AUTORITE_BNF,),
        provenance=AUTORITE_OPENLIBRARY,
    )


def _ambiguous() -> BookSearchResult:
    return BookSearchResult(
        status=BookSearchStatus.AMBIGUOUS,
        works=(
            BookWorkChoice(title="Les Enfants perdus", author="Roman", isbn13="9782214080627"),
            BookWorkChoice(title="Les Enfants perdus", author="Nef", isbn13="9782810206087"),
        ),
        note="⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre.",
    )


def _unavailable() -> BookSearchResult:
    return BookSearchResult(
        status=BookSearchStatus.UNAVAILABLE,
        consulted=(AUTORITE_BNF,),
        unavailable=(AUTORITE_BNF,),
        note="Aucun candidat exploitable et une autorité n'a pas répondu.",
    )


@pytest.mark.parametrize(
    ("resultat", "statut"),
    [
        (_found(), BookSearchStatus.FOUND),
        (_ambiguous(), BookSearchStatus.AMBIGUOUS),
        (_partial_sans_isbn(), BookSearchStatus.PARTIAL),
        (BookSearchResult(status=BookSearchStatus.NOT_FOUND), BookSearchStatus.NOT_FOUND),
        (_unavailable(), BookSearchStatus.UNAVAILABLE),
    ],
)
def test_projection_des_cinq_statuts(resultat: BookSearchResult, statut: BookSearchStatus) -> None:
    """Chaque statut projette un brouillon cohérent, sans jamais rattacher hors found."""
    updates = draft_updates_from_search(resultat, query_title="Blues")

    assert updates["isbn"] is None if statut != BookSearchStatus.FOUND else True
    assert updates["category_slug"] == "bd_comics"
    if statut != BookSearchStatus.FOUND:
        # Aucun champ confirmé n'est alimenté : les candidats restent des choix.
        assert updates["author"] is None
        assert updates["publisher"] is None
        assert updates["book_context"] is None
    if statut == BookSearchStatus.FOUND:
        assert updates["isbn"] == "9782369903086"
        assert updates["author"] == "Martin Panchaud"


def test_partial_sans_isbn_a_un_texte_distinct_d_une_panne() -> None:
    """Deux `partial` distincts ne disent pas la même chose : motif réel, pas statut."""
    sans_isbn = draft_updates_from_search(_partial_sans_isbn(), query_title="Blues")
    panne = draft_updates_from_search(_partial_repli_panne(), query_title="Blues")

    assert "aucune édition avec ISBN" in (sans_isbn["isbn_note"] or "")
    assert "ISBN non rattaché pour l'instant" in (panne["isbn_note"] or "")


def test_partial_nourrit_ni_champs_confirmes_ni_book_context() -> None:
    """Le candidat s'affiche, mais ne devient jamais une identité."""
    updates = draft_updates_from_search(_partial_repli_panne(), query_title="Blacksad")

    assert metadata_from_result(_partial_repli_panne()) is None
    assert updates["isbn"] is None
    assert updates["author"] is None
    assert updates["book_context"] is None
    # Le candidat reste consultable (liste de choix), pas confondu avec la fiche.
    assert updates["work_choices"] == [
        {
            "isbn13": "9782369903086",
            "title": "Blacksad",
            "author": "Canales",
            "publisher": None,
            "publication_year": None,
        }
    ]


def test_unavailable_invite_a_creer_sur_le_titre() -> None:
    """Une panne n'est pas une absence : la carte le dit et n'empêche pas la création."""
    updates = draft_updates_from_search(_unavailable(), query_title="Blues")

    assert "Vérification impossible pour l'instant" in (updates["isbn_note"] or "")
    assert updates["isbn"] is None
    assert updates["book_unidentified"] is False


def _state(data: dict[str, Any]) -> AsyncMock:
    stored = dict(data)

    async def _get() -> dict[str, Any]:
        return dict(stored)

    async def _update(payload: dict | None = None, **kwargs: Any) -> None:
        stored.update(payload or {})
        stored.update(kwargs)

    state = AsyncMock()
    state.get_data = AsyncMock(side_effect=_get)
    state.update_data = AsyncMock(side_effect=_update)
    state.set_state = AsyncMock()
    state._stored = stored
    return state


def _callback(data: str) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_choisir_une_oeuvre_ne_confirme_pas_sous_panne() -> None:
    """Le clic lève l'ambiguïté ; sans confirmation d'autorité, rien n'est rattaché."""
    works = [
        {"isbn13": "9782214080627", "title": "Les Enfants perdus", "author": "Roman"},
        {"isbn13": "9782810206087", "title": "Les Enfants perdus", "author": "Nef"},
    ]
    state = _state({"name": "Les Enfants perdus", "work_choices": works, "isbn": None})
    callback = _callback("al_create_work:1")

    silent = BookSearchResult(
        status=BookSearchStatus.UNAVAILABLE,
        consulted=(AUTORITE_BNF,),
        unavailable=(AUTORITE_BNF,),
    )

    class _UseCase:
        async def search_by_isbn(self, _isbn: str, **_kw: Any):
            return silent

    with patch(
        "app.bot.handlers.alerts_create_confirm.BookSearchUseCase", return_value=_UseCase()
    ):
        await select_alert_creation_work(callback, state)

    assert state._stored.get("isbn") is None, "une panne ne promeut pas le candidat"
    assert "confirmable" in callback.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_callback_d_edition_obsolete_ne_rattache_rien() -> None:
    """Un ISBN devenu non confirmable ne se rattache jamais par un vieux bouton."""
    editions = [{"isbn13": "9782369903086"}, {"isbn13": "9782369904151"}]
    state = _state({"name": "La couleur des choses", "edition_alternatives": editions})
    callback = _callback("al_create_edition:1")

    not_found = BookSearchResult(status=BookSearchStatus.NOT_FOUND)

    class _UseCase:
        async def search_by_isbn(self, _isbn: str, **_kw: Any):
            return not_found

    with patch(
        "app.bot.handlers.alerts_create_confirm.BookSearchUseCase", return_value=_UseCase()
    ):
        await select_alert_creation_edition(callback, state)

    assert "isbn" not in state._stored
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_d_edition_hors_liste_reste_muet() -> None:
    """Un index périmé ne fait rien : le message actuel est conservé."""
    state = _state({"name": "T", "edition_alternatives": [{"isbn13": "9782369903086"}]})
    callback = _callback("al_create_edition:9")

    await select_alert_creation_edition(callback, state)

    assert "Édition inconnue" in callback.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_reponse_bibliographique_tardive_est_ignoree() -> None:
    """Une recherche périmée n'écrit pas : une saisie plus récente garde l'écran."""
    state = _state({})
    message = MagicMock(spec=Message)
    status = MagicMock(spec=Message)
    status.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status)

    class _UseCase:
        async def search_by_title(self, _title: str, *_args: Any, **_kw: Any):
            # Pendant la recherche, une saisie plus récente prend la main.
            await state.update_data(search_token=99)
            return _found()

    category_repo = MagicMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    with (
        patch("app.bot.handlers.alerts_helpers.BookSearchUseCase", return_value=_UseCase()),
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=category_repo),
    ):
        await process_bd_query_lookup(message, state, "La couleur des choses", AsyncMock())

    # La réponse périmée n'a pas été publiée : l'écran de la nouvelle saisie tient.
    status.edit_text.assert_not_awaited()
    assert state._stored["search_token"] == 99


@pytest.mark.asyncio
async def test_realignement_isbn_partial_ne_remplit_pas_la_fiche() -> None:
    """Un repli pendant une panne garde l'ISBN saisi mais vide l'identité."""
    partial = result_from_report(
        BibliographicReport(
            kind="title",
            resolution=BookResolution(
                best=BookMetadata(
                    title="Blacksad",
                    author="Canales",
                    isbn13="9782369903086",
                    source=AUTORITE_OPENLIBRARY,
                )
            ),
            attempts=(
                ProviderAttempt(
                    provider=AUTORITE_BNF,
                    search_type="isbn",
                    status=ProviderQueryStatus.TIMEOUT,
                    has_results=False,
                    error_code="timeout",
                ),
            ),
        )
    )
    assert partial.status == BookSearchStatus.PARTIAL

    class _UseCase:
        async def search_by_title(self, _title: str, *_args: Any, **_kw: Any):
            return partial

    with patch("app.bot.handlers.alerts_helpers.BookSearchUseCase", return_value=_UseCase()):
        realignment = await realign_draft_to_isbn(
            {"name": "Un autre titre"}, "9782369903086"
        )

    assert realignment.conflict is None
    assert realignment.updates["isbn"] == "9782369903086"
    assert realignment.updates["author"] is None
    assert realignment.updates["book_context"] is None


def test_natural_add_applique_la_meme_politique_que_la_saisie_manuelle() -> None:
    """Les deux chemins lisent le même contrat : `found` seul attache un ISBN."""
    from app.services.ai.modules import natural_add as module

    assert module.BookSearchUseCase is not None
    import inspect

    source = inspect.getsource(module.NaturalAddModule._resolve_isbn)
    assert "search_by_title" in source
    assert "search_by_isbn" in source
    # Aucun second chemin d'autorité : pas de `verify_isbn` ni de `resolve_for_alert`.
    assert "verify_isbn" not in source
    assert "resolve_for_alert" not in source
    assert uuid is not None

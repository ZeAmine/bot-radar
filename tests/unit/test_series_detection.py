"""La détection de série, testée sur la règle pure puis sur le vrai chemin de création.

Deux niveaux, parce qu'ils ne prouvent pas la même chose : la règle dit ce qu'une
résolution démontre, et `process_bd_query_lookup` dit ce que le brouillon en fait —
l'ISBN écarté, le titre de la série, et le récap que l'utilisateur lit vraiment.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

from app.domain.rules.series_detection import detect_series
from tests.add_fakes import fake_book_search

VOLUME_2_TITLE = "Absolute Martian Manhunter Vol. 2"
SERIES_TITLE = "Absolute Martian Manhunter"
# Les deux codes réels du repli : l'édition de référence (sans numéro) et le tome 2.
ISBN_VOLUME_1 = "9781799505204"
ISBN_VOLUME_2 = "9781799510871"

# Cas BNF réel : les deux tomes ne se distinguent que par un numéro caché après le
# sous-titre, que `clean_bnf_title` coupe.
MANUEL_TITLE = "Manuel du cuisinier amateur"
ISBN_MANUEL_1 = "9782812321344"
ISBN_MANUEL_2 = "9782812321689"


def test_marker_word_forms_are_detected():
    """« Vol. », « Tome », « T » et le numéro collé disent tous le même tome."""
    for title in ("Vol. 2", "Tome 2", "T2", "Tome2", "Volume 2", "Book 2", "Livre 2"):
        signal = detect_series(
            query_title=SERIES_TITLE,
            notices=[f"{SERIES_TITLE} {title}"],
        )
        assert signal.is_series is True, title
        assert signal.volumes == ("Tome 2",), title


def test_trailing_number_is_detected():
    """Un numéro collé en fin de titre, sans mot-marqueur, décrit aussi un tome."""
    signal = detect_series(query_title="One Piece", notices=["One Piece 12"])

    assert signal.is_series is True
    assert signal.volumes == ("Tome 12",)


def test_explicit_tome_in_the_query_disables_the_detection():
    """Un tome demandé reste un tome : la saisie le dit, la détection s'efface."""
    for query in (
        f"{SERIES_TITLE} Vol. 2",
        f"{SERIES_TITLE} T2",
        f"{SERIES_TITLE} 2",
    ):
        assert detect_series(query_title=query, notices=[VOLUME_2_TITLE]).is_series is False


def test_single_volume_title_is_not_a_series():
    """Une notice sans numéro ne prouve pas de série."""
    signal = detect_series(query_title="Blacksad", notices=["Blacksad"])

    assert signal.is_series is False
    assert signal.volumes == ()


def test_notice_of_another_work_is_not_a_series():
    """Un titre numéroté qui ne reprend pas la saisie n'est pas la série demandée."""
    signal = detect_series(query_title="Blacksad", notices=["Absolute Batman Vol. 2"])

    assert signal.is_series is False


def test_several_notices_yield_the_volumes_sorted_and_deduplicated():
    """Plusieurs notices du même œuvre : les tomes repérés, triés, sans doublon."""
    signal = detect_series(
        query_title=SERIES_TITLE,
        notices=[
            "Absolute Martian Manhunter Vol. 3",
            "Absolute Martian Manhunter Vol. 1",
            "Absolute Martian Manhunter Tome 3",
        ],
    )

    assert signal.is_series is True
    assert signal.volumes == ("Tome 1", "Tome 3")


def test_a_publication_year_is_not_a_volume():
    """Quatre chiffres ne sont pas un numéro de tome : « Blade Runner 2049 »."""
    blade = detect_series(query_title="Blade Runner", notices=["Blade Runner 2049"])
    death = detect_series(query_title="Deathbringer", notices=["Deathbringer 2021"])

    assert blade.is_series is False
    assert death.is_series is False


def test_empty_inputs_are_handled_without_raising():
    """Saisie vide, notice vide, notice absente : « rien de prouvé », jamais une erreur."""
    assert detect_series(query_title="", notices=[VOLUME_2_TITLE]).is_series is False
    assert detect_series(query_title=SERIES_TITLE, notices=[]).is_series is False
    assert detect_series(query_title=SERIES_TITLE, notices=[""]).is_series is False
    assert detect_series(query_title=SERIES_TITLE, notices=["2"]).is_series is False


def test_a_roman_numeral_volume_is_not_claimed():
    """Limite assumée : un tome en chiffres romains n'est pas détecté, et ne casse rien.

    La règle n'affirme que ce qu'elle lit. Mieux vaut ne rien détecter qu'inventer un
    numéro à partir d'une syllabe du titre.
    """
    signal = detect_series(query_title=SERIES_TITLE, notices=[f"{SERIES_TITLE} Tome II"])

    assert signal.is_series is False


class _FakeService:
    """Résolution figée : la détection est locale, rien n'est interrogé ici."""

    def __init__(self, resolution: object) -> None:
        self._resolution = resolution

    async def resolve_input(self, _query: str) -> object:
        return self._resolution

    async def resolve_for_alert(self, _title: str, _author_hint: str | None = None) -> object:
        return self._resolution


def _message() -> MagicMock:
    message = MagicMock(spec=Message)
    message.answer = AsyncMock(return_value=MagicMock(spec=Message))
    message.answer.return_value.edit_text = AsyncMock()
    return message


def _state() -> AsyncMock:
    """État FSM simulé, **persistant** : une recherche relit son jeton après coup."""
    stored: dict[str, Any] = {}

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


async def _lookup(query: str, resolution: object) -> dict:
    """Joue le vrai `process_bd_query_lookup` et rend le brouillon réellement écrit."""
    from app.bot.handlers.alerts_helpers import process_bd_query_lookup

    state = _state()
    category_repo = MagicMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    with (
        patch(
            "app.bot.handlers.alerts_helpers.BookSearchUseCase",
            fake_book_search(_FakeService(resolution)),
        ),
        patch(
            "app.bot.handlers.alerts.CategoryRepository",
            return_value=category_repo,
        ),
    ):
        await process_bd_query_lookup(_message(), state, query, AsyncMock())

    return dict(state._stored)


def _notice(title: str, isbn13: str):
    from app.services.book_metadata_service import BookMetadata

    return BookMetadata(
        title=title,
        author="Deniz Camp",
        publisher="DC Comics",
        isbn13=isbn13,
        recommended_includes=[title.lower()],
        recommended_excludes=["roman"],
        recommended_variants=[],
    )


def _series_resolution() -> object:
    """La forme RÉELLE du repli pour cette œuvre : deux documents, dont un numéroté.

    La notice retenue — parce que son titre colle exactement à la saisie — ne porte
    aucun numéro. Le document « Vol. 2 » n'est ni une édition (`_is_same_work` compare
    les titres) ni un homonyme (`_is_homonym` exige un titre identique) : il était jeté,
    et la série devenait invisible. Il est ici, dans `notice_titles`, comme la résolution
    le rend désormais.
    """
    from app.services.book_metadata_service import BookResolution

    return BookResolution(
        best=_notice(SERIES_TITLE, ISBN_VOLUME_1),
        notice_titles=(SERIES_TITLE, VOLUME_2_TITLE),
    )


def _volume_2_resolution() -> object:
    """Une saisie qui vise le tome 2 : la notice retenue EST celle du volume."""
    from app.services.book_metadata_service import BookResolution

    return BookResolution(
        best=_notice(VOLUME_2_TITLE, ISBN_VOLUME_2),
        notice_titles=(VOLUME_2_TITLE,),
    )


def test_resolution_signal_reads_every_notice_the_authority_rendered():
    """La preuve vit dans la notice voisine, jamais transmise : elle l'est maintenant.

    C'était le défaut : `detect_series` ne recevait que le titre retenu — sans numéro —
    donc l'œuvre numérotée passait pour un one-shot et l'alerte partait avec l'ISBN d'un
    tome.
    """
    signal = _series_resolution().series_signal(SERIES_TITLE)

    assert signal.is_series is True
    assert signal.volumes == ("Tome 2",)


def test_resolution_signal_stays_silent_on_an_ambiguous_resolution():
    """Un titre qui désigne plusieurs œuvres ne prouve aucun tome : la garde est ici."""
    from app.services.book_metadata_service import BookResolution

    homonym = BookResolution(
        best=_notice(SERIES_TITLE, ISBN_VOLUME_1),
        other_works=[_notice(SERIES_TITLE, ISBN_VOLUME_2)],
        notice_titles=(SERIES_TITLE, VOLUME_2_TITLE),
    )

    assert homonym.is_ambiguous is True
    assert homonym.series_signal(SERIES_TITLE).is_series is False


@pytest.mark.asyncio
async def test_creation_builds_a_series_alert_without_isbn():
    """La saisie ne vise aucun tome : le brouillon surveille la série, et le récap le dit."""
    from app.bot.handlers.alerts_helpers import creation_card

    stored = await _lookup(SERIES_TITLE, _series_resolution())

    assert stored["is_series"] is True
    assert stored["isbn"] is None
    # Le titre de l'alerte est celui de la série, pas celui d'un volume.
    assert stored["name"] == SERIES_TITLE
    # L'indication se dérive de `is_series` : deux rendus de la même fiche ne
    # peuvent pas se contredire.
    assert "series_note" not in stored
    # Les mots-clés portent sur la série : « … vol 2 » ne verrait jamais le tome 3.
    assert stored["include_keywords"] == ["absolute martian manhunter"]
    assert "absolute martian manhunter vol 2" not in stored["include_keywords"]
    # Pas d'éditions concurrentes : ce seraient celles d'un tome.
    assert stored["edition_alternatives"] == []

    # L'indication figure hors de la liste et en italique, faute d'identifiant à
    # afficher ; ni ligne ISBN ni énumération des tomes ne la répètent.
    from app.bot.handlers.alerts_helpers import RECAP_TITLE

    recap = creation_card(stored)
    lines = recap.splitlines()
    assert lines[0] == RECAP_TITLE
    assert lines[1] == ""
    assert lines[2] == "<i>Série multi-tomes (veille sur tous les tomes)</i>"
    assert lines[3] == ""
    assert lines[4] == "<i>Analyse IA indisponible : aucune proposition ajoutée.</i>"
    assert lines[5] == ""
    assert lines[6].startswith("<b>Titre :</b>")
    assert "<b>ISBN :</b>" not in recap
    assert "introuvable" not in recap
    assert "<code>" not in recap, "aucun ISBN ne doit être rendu"


def _manuel_resolution() -> object:
    """La résolution BNF réelle de « Manuel du cuisinier amateur » : deux tomes.

    Le titre de base ne porte aucun numéro — c'est `volume_title` qui le retient, après
    que `clean_bnf_title` a coupé le sous-titre. Les deux notices sont donc éditions
    « alternatives » du même titre, et c'est la détection de série qui doit trancher.
    """
    from app.services.book_metadata_service import BookResolution

    tome_1 = _notice(MANUEL_TITLE, ISBN_MANUEL_1)
    tome_1.volume_title = f"{MANUEL_TITLE} Tome 1"
    tome_2 = _notice(MANUEL_TITLE, ISBN_MANUEL_2)
    tome_2.volume_title = f"{MANUEL_TITLE} Tome 2"
    return BookResolution(
        best=tome_1,
        alternatives=[tome_2],
        notice_titles=(tome_1.volume_title, tome_2.volume_title),
    )


@pytest.mark.asyncio
async def test_creation_builds_a_series_alert_from_tomes_hidden_in_the_subtitle():
    """Tome 1 + tome 2 sous un même titre : alerte de série, sans ISBN.

    C'est le cas réel « Manuel du cuisinier amateur » : deux notices BNF, même titre,
    même éditeur, seul le numéro de tome — coupé avec le sous-titre — les distingue.
    """
    from app.bot.handlers.alerts_helpers import creation_card

    stored = await _lookup(MANUEL_TITLE, _manuel_resolution())

    assert stored["is_series"] is True
    assert stored["isbn"] is None
    assert stored["name"] == MANUEL_TITLE
    assert "series_note" not in stored
    # Plus de tomes proposés comme éditions concurrentes : ce seraient deux volumes.
    assert stored["edition_alternatives"] == []

    # Le cas réel de la capture : deux tomes, et une seule indication — sans énumération.
    recap = creation_card(stored)
    assert "<i>Série multi-tomes (veille sur tous les tomes)</i>" in recap
    assert "Tome 1, Tome 2" not in recap
    assert "<b>ISBN :</b>" not in recap
    assert "<code>" not in recap, "aucun ISBN ne doit être rendu"


@pytest.mark.asyncio
async def test_creation_keeps_the_isbn_when_the_query_names_a_tome():
    """Une saisie qui demande « Vol. 2 » garde son tome : la détection s'efface."""
    stored = await _lookup(f"{SERIES_TITLE} Vol. 2", _volume_2_resolution())

    assert stored["is_series"] is False
    assert stored["isbn"] == ISBN_VOLUME_2
    assert stored["name"] == f"{SERIES_TITLE} - Volume 2"
    assert "series_note" not in stored

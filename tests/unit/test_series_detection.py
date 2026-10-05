"""La détection de série, testée sur la règle pure puis sur le vrai chemin de création.

Deux niveaux, parce qu'ils ne prouvent pas la même chose : la règle dit ce qu'une
résolution démontre, et `process_bd_query_lookup` dit ce que le brouillon en fait —
l'ISBN écarté, le titre de la série, et le récap que l'utilisateur lit vraiment.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

from app.domain.rules.series_detection import detect_series

VOLUME_2_TITLE = "Absolute Martian Manhunter Vol. 2"
SERIES_TITLE = "Absolute Martian Manhunter"
ISBN_VOLUME_2 = "9781799510871"

# --- Règle pure -----------------------------------------------------------------


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


# --- Chemin de création manuel ---------------------------------------------------


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
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()
    return state


async def _lookup(query: str, resolution: object) -> dict:
    """Joue le vrai `process_bd_query_lookup` et rend le brouillon réellement écrit."""
    from app.bot.handlers.alerts_helpers import process_bd_query_lookup

    state = _state()
    category_repo = MagicMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    with (
        patch(
            "app.bot.handlers.alerts_helpers.BookMetadataService",
            return_value=_FakeService(resolution),
        ),
        patch(
            "app.bot.handlers.alerts.CategoryRepository",
            return_value=category_repo,
        ),
    ):
        await process_bd_query_lookup(_message(), state, query, AsyncMock())

    return state.update_data.call_args.kwargs


def _volume_2_resolution() -> object:
    from app.services.book_metadata_service import BookMetadata, BookResolution

    volume_2 = BookMetadata(
        title=VOLUME_2_TITLE,
        author="Deniz Camp",
        publisher="DC Comics",
        isbn13=ISBN_VOLUME_2,
        recommended_includes=["absolute martian manhunter vol 2"],
        recommended_excludes=["roman"],
        recommended_variants=[],
    )
    return BookResolution(best=volume_2)


@pytest.mark.asyncio
async def test_creation_builds_a_series_alert_without_isbn():
    """La saisie ne vise aucun tome : le brouillon surveille la série, et le récap le dit."""
    from app.bot.handlers.alerts_helpers import recap_from_state

    stored = await _lookup(SERIES_TITLE, _volume_2_resolution())

    assert stored["is_series"] is True
    assert stored["isbn"] is None
    # Le titre de l'alerte est celui de la série, pas celui d'un volume.
    assert stored["name"] == SERIES_TITLE
    assert stored["series_note"] == (
        "Série multi-tomes détectée (Tome 2) — veille sur tous les tomes, sans ISBN."
    )
    # Les mots-clés portent sur la série : « … vol 2 » ne verrait jamais le tome 3.
    assert stored["include_keywords"] == ["absolute martian manhunter"]
    assert "absolute martian manhunter vol 2" not in stored["include_keywords"]
    # Pas d'éditions concurrentes : ce seraient celles d'un tome.
    assert stored["edition_alternatives"] == []

    # Le récap réellement affiché porte la trouvaille, et dit que l'ISBN n'est pas
    # utilisé : l'alerte n'a pas d'identifiant, mais elle ne fait pas croire à un oubli.
    recap = recap_from_state(stored)
    assert stored["series_note"] in recap
    assert "<b>ISBN :</b> non utilisé — série multi-tomes" in recap
    assert "<code>" not in recap, "aucun ISBN ne doit être rendu"


@pytest.mark.asyncio
async def test_creation_keeps_the_isbn_when_the_query_names_a_tome():
    """Une saisie qui demande « Vol. 2 » garde son tome : la détection s'efface."""
    stored = await _lookup(f"{SERIES_TITLE} Vol. 2", _volume_2_resolution())

    assert stored["is_series"] is False
    assert stored["isbn"] == ISBN_VOLUME_2
    assert stored["name"] == f"{SERIES_TITLE} Vol. 2"
    assert stored["series_note"] is None

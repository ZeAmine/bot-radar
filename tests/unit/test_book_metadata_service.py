"""Tests unitaires pour le service BookMetadataService (parsing, BNF SRU, recommandations)."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.book_metadata_service import (
    BookMetadata,
    BookMetadataService,
    clean_bnf_creator,
    clean_bnf_publisher,
    clean_bnf_title,
    generate_recommended_keywords,
    parse_query_title_and_author,
)


def test_parse_query_title_and_author():
    """Vérifie l'extraction du titre et de l'indice d'auteur dans différentes formulations."""
    assert parse_query_title_and_author("deathbringer de ismael le Grand") == (
        "deathbringer",
        "ismael le Grand",
    )
    assert parse_query_title_and_author("la saga de grimr de Jeremy Morrow") == (
        "la saga de grimr",
        "Jeremy Morrow",
    )
    assert parse_query_title_and_author("La bête par Chabouté") == ("La bête", "Chabouté")
    assert parse_query_title_and_author("Blacksad") == ("Blacksad", None)


def test_clean_bnf_helpers():
    """Vérifie le nettoyage des champs bruts issus de la BNF."""
    # Titres
    assert clean_bnf_title("La saga de Grimr / Jérémie Moreau") == "La saga de Grimr"
    assert clean_bnf_title("Ion mud / Amaury Bündgen") == "Ion mud"

    # Créateurs
    assert clean_bnf_creator("Moreau, Jérémie (1987-....). Auteur du texte") == "Jérémie Moreau"
    assert clean_bnf_creator("Legrand, Ismaël (Auteur du texte)") == "Ismaël Legrand"

    # Éditeurs
    assert clean_bnf_publisher("Éditions Delcourt (Paris)") == "Éditions Delcourt"
    assert clean_bnf_publisher("Casterman (Bruxelles)") == "Casterman"


def test_generate_recommended_keywords():
    """Vérifie la génération automatique des mots-clés d'inclusion, d'exclusion et variantes."""
    # Deathbringer
    inc, exc, var = generate_recommended_keywords("Deathbringer", "Ismaël Legrand")
    assert "deathbringer" in inc
    assert "figurine" in exc
    assert "warhammer" in exc
    assert "death bringer" in var

    # La saga de Grimr
    inc_g, exc_g, var_g = generate_recommended_keywords("La saga de Grimr", "Jérémie Moreau")
    assert "la saga de grimr" in inc_g
    assert "saga de grimr" in inc_g
    assert "roman" in exc_g
    assert "poche" in exc_g


@pytest.mark.asyncio
async def test_lookup_by_title_and_author_mocked():
    """Vérifie que lookup_by_title_and_author trie et renvoie le meilleur résultat."""
    service = BookMetadataService()

    fake_record = BookMetadata(
        title="Deathbringer",
        author="Ismaël Legrand",
        publisher="Guy Delcourt Productions",
        isbn13="9782413078128",
        source="bnf",
    )

    with patch.object(service, "_query_bnf_sru", new=AsyncMock(return_value=[fake_record])):
        res = await service.lookup("deathbringer de ismael le Grand")
        assert res is not None
        assert res.title == "Deathbringer"
        assert res.author == "Ismaël Legrand"
        assert res.isbn13 == "9782413078128"
        assert "deathbringer" in res.recommended_includes
        assert "death bringer" in res.recommended_variants

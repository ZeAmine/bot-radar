"""Recherche BnF par titre + auteur, et son repli strict.

Deux sujets indissociables : l'auteur entre dans la requête (sans quoi la fenêtre
de dix notices se remplit d'homonymes), et une conjonction qui ne rend rien est
relancée en titre seul — mais seulement quand elle a **réussi**. Une panne ne
déclenche rien : elle ne prouve ni présence ni absence.

Le nettoyage de l'auteur est testé ici aussi, parce qu'un défaut de cette fonction
suffisait à faire rendre zéro à une conjonction qui devait trouver 34 notices :
« Juan Díaz Canales » passait par la liste blanche latine des titres et devenait
« Juan D az Canales ».
"""

from unittest.mock import AsyncMock

import pytest

from app.core.constants import ProviderQueryStatus
from app.domain.rules.catalogue_search import is_catalogue_exhausted
from app.providers.google_books import ProviderQueryResult
from app.services.book_metadata_service import (
    AUTORITE_BNF,
    BookMetadata,
    BookMetadataService,
    CatalogueVerdict,
    clean_author_query,
    clean_title_query,
    consulted_authorities,
    failed_authorities,
)


def _notice(title: str = "Le Voyage", author: str = "Edmond Baudoin") -> BookMetadata:
    return BookMetadata(
        title=title,
        author=author,
        publisher="L'Association",
        isbn13="9782909020662",
        source=AUTORITE_BNF,
        publication_year=1998,
    )


def _ok(records: list[BookMetadata]) -> tuple[ProviderQueryResult[BookMetadata], None]:
    return (
        ProviderQueryResult(
            status=ProviderQueryStatus.SUCCESS,
            items=tuple(records),
            has_results=bool(records),
        ),
        None,
    )


def _failure(
    status: ProviderQueryStatus = ProviderQueryStatus.TIMEOUT,
) -> tuple[ProviderQueryResult[BookMetadata], str]:
    return (
        ProviderQueryResult(status=status, error_code="timeout"),
        "TimeoutError: synthetic",
    )


def _service_ok() -> BookMetadataService:
    """Le repli OpenLibrary est neutralisé : ces tests portent sur la BNF."""
    service = BookMetadataService()
    service._query_openlibrary_result = AsyncMock(  # type: ignore[method-assign]
        return_value=ProviderQueryResult(status=ProviderQueryStatus.SUCCESS, items=())
    )
    return service


class TestAuthorCleaning:
    """Un auteur ne passe pas par le nettoyage des titres."""

    def test_an_accented_author_is_not_mutilated(self):
        """Le défaut mesuré : 34 notices attendues, zéro rendue."""
        assert clean_author_query("Juan Díaz Canales") == "Juan Díaz Canales"

    def test_the_title_cleaner_would_have_mutilated_it(self):
        """La preuve du défaut, pour que la régression reste visible."""
        assert clean_title_query("Juan Díaz Canales") == "Juan D az Canales"

    @pytest.mark.parametrize(
        "raw",
        ["Whoogy's", "Pascal Rabaté", "Chabouté", "Jérémie Moreau", "Marjane Satrapi"],
    )
    def test_other_authors_keep_their_characters(self, raw):
        assert clean_author_query(raw) == raw

    def test_the_closing_quote_is_removed(self):
        """Le guillemet casserait la valeur CQL et permettrait une injection."""
        assert '"' not in clean_author_query('a" or b')

    def test_line_breaks_are_removed(self):
        assert clean_author_query("a\nb\r\nc\t") == "a b c"

    def test_surrounding_spaces_are_trimmed(self):
        assert clean_author_query("  Neyef  ") == "Neyef"


class TestTitleAuthorQuery:
    """La conjonction est émise quand un auteur est fourni."""

    @pytest.mark.asyncio
    async def test_a_successful_conjunction_issues_a_single_query(self):
        service = _service_ok()
        calls: list[str] = []

        async def recorded(cql, *_args, **_kwargs):
            calls.append(cql)
            return _ok([_notice()])

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        report = await service.resolve_for_alert_report("Le Voyage", "Edmond Baudoin")

        assert len(calls) == 1
        assert 'bib.author all "Edmond Baudoin"' in calls[0]
        assert report.resolution is not None

    @pytest.mark.asyncio
    async def test_the_author_is_preserved_in_the_emitted_query(self):
        service = _service_ok()
        calls: list[str] = []

        async def recorded(cql, *_args, **_kwargs):
            calls.append(cql)
            return _ok([_notice(title="Blacksad", author="Juan Díaz Canales")])

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        await service.resolve_for_alert_report("Blacksad", "Juan Díaz Canales")

        assert 'bib.author all "Juan Díaz Canales"' in calls[0]
        assert "D az" not in calls[0]

    @pytest.mark.asyncio
    async def test_without_an_author_the_plain_title_query_is_kept(self):
        service = _service_ok()
        calls: list[str] = []

        async def recorded(cql, *_args, **_kwargs):
            calls.append(cql)
            return _ok([_notice()])

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        await service.resolve_for_alert_report("Le Voyage")

        assert len(calls) == 1
        assert "bib.author" not in calls[0]


class TestStrictFallback:
    """Le repli n'est déclenché que par un zéro **réussi**."""

    @pytest.mark.asyncio
    async def test_a_successful_empty_conjunction_falls_back_to_the_title(self):
        service = _service_ok()
        calls: list[str] = []

        async def recorded(cql, *_args, **_kwargs):
            calls.append(cql)
            if "bib.author" in cql:
                return _ok([])
            return _ok([_notice()])

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        report = await service.resolve_for_alert_report("Le Voyage", "Edmond Baudoin")

        assert len(calls) == 2
        assert "bib.author" in calls[0]
        assert "bib.author" not in calls[1]
        assert report.resolution is not None

    @pytest.mark.asyncio
    async def test_a_failed_conjunction_triggers_no_fallback(self):
        service = _service_ok()
        calls: list[str] = []

        async def recorded(cql, *_args, **_kwargs):
            calls.append(cql)
            return _failure()

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        await service.resolve_for_alert_report("Le Voyage", "Edmond Baudoin")

        # La conjonction est en panne : aucune relance en titre seul. Le seul autre
        # appel possible est l'élargissement `anywhere` du chemin existant.
        title_only = [
            cql for cql in calls if "bib.author" not in cql and "bib.anywhere" not in cql
        ]
        assert title_only == []

    @pytest.mark.asyncio
    async def test_the_failed_conjunction_stays_visible_in_the_attempts(self):
        service = _service_ok()

        async def recorded(_cql, *_args, **_kwargs):
            return _failure()

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        report = await service.resolve_for_alert_report("Le Voyage", "Edmond Baudoin")

        failed = {a.provider for a in report.attempts if a.status != ProviderQueryStatus.SUCCESS}
        assert AUTORITE_BNF in failed

    @pytest.mark.asyncio
    async def test_two_empty_successes_do_not_exhaust_the_catalogue(self):
        """Le cœur de la garde : deux zéros réussis ne suffisent pas à trancher."""
        service = _service_ok()
        calls: list[str] = []

        async def recorded(cql, *_args, **_kwargs):
            calls.append(cql)
            return _ok([])

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        report = await service.resolve_for_alert_report("Dailycieux", "Whoogy's")

        assert report.resolution is None
        # Conjonction, puis repli titre seul, puis l'élargissement `anywhere` que
        # le chemin existant applique déjà quand l'auteur est fourni et que rien
        # n'est revenu. Trois requêtes réelles, trois tentatives.
        assert [cql.split(" all")[0] for cql in calls] == [
            "bib.title",
            "bib.title",
            "bib.anywhere",
        ]
        # Aucune panne : la BnF a répondu aux deux requêtes, l'ouvrage est absent.
        assert all(a.status == ProviderQueryStatus.SUCCESS for a in report.attempts)
        # La recherche rend un rapport, jamais un verdict d'épuisement : celui-ci
        # n'est écrit que par le chemin série, sur un `CatalogueVerdict`.
        assert not hasattr(report, "is_catalogue_exhausted")

    @pytest.mark.asyncio
    async def test_a_failed_query_alone_never_exhausts(self):
        """Une panne interdit l'épuisement, même sans la moindre notice."""
        service = _service_ok()

        async def recorded(_cql, *_args, **_kwargs):
            return _failure()

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        report = await service.resolve_for_alert_report("Dune", "Frank Herbert")

        attempts = report.attempts
        assert [a for a in attempts if a.status != ProviderQueryStatus.SUCCESS]
        assert not is_catalogue_exhausted(
            CatalogueVerdict(
                consulted=consulted_authorities(attempts),
                unavailable=failed_authorities(attempts),
                notices=(),
            )
        )


class TestAttemptsAreFaithful:
    """Le rapport ne doit jamais annoncer une tentative quand deux sont parties."""

    @pytest.mark.asyncio
    async def test_the_fallback_records_both_queries(self):
        service = _service_ok()
        calls: list[str] = []

        async def recorded(cql, *_args, **_kwargs):
            calls.append(cql)
            if "bib.author" in cql:
                return _ok([])
            return _ok([_notice()])

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        report = await service.resolve_for_alert_report("Le Voyage", "Edmond Baudoin")

        title_attempts = [a for a in report.attempts if a.search_type == "title"]
        assert len(calls) == 2
        assert len(title_attempts) == 2, "deux requêtes émises, deux tentatives consignées"

    @pytest.mark.asyncio
    async def test_one_query_records_one_attempt(self):
        service = _service_ok()

        async def recorded(_cql, *_args, **_kwargs):
            return _ok([_notice()])

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        report = await service.resolve_for_alert_report("Le Voyage", "Edmond Baudoin")

        assert len([a for a in report.attempts if a.search_type == "title"]) == 1

    @pytest.mark.asyncio
    async def test_the_failed_attempt_keeps_its_own_status(self):
        """La tentative en échec ne doit pas être maquillée en succès vide."""
        service = _service_ok()

        async def recorded(_cql, *_args, **_kwargs):
            return _failure()

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        report = await service.resolve_for_alert_report("Le Voyage", "Edmond Baudoin")

        statuses = {a.status for a in report.attempts}
        assert ProviderQueryStatus.TIMEOUT in statuses


class TestNoRegressionOnExistingVerdicts:
    """Les seuils et gardes existants s'appliquent toujours après la recherche."""

    @pytest.mark.asyncio
    async def test_a_conjunction_that_names_another_author_still_falls_back(self):
        """La notice rendue ne nomme pas l'auteur : le repli OpenLibrary reste actif."""
        service = _service_ok()
        other = BookMetadata(
            title="Le Voyage", author="Quentin Breton", isbn13="9782000000001",
            source=AUTORITE_BNF,
        )
        service._query_openlibrary_result = AsyncMock(  # type: ignore[method-assign]
            return_value=ProviderQueryResult(status=ProviderQueryStatus.SUCCESS, items=())
        )

        async def recorded(_cql, *_args, **_kwargs):
            return _ok([other])

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        report = await service.resolve_for_alert_report("Le Voyage", "Edmond Baudoin")

        # Un homonyme ne se ratifie jamais en silence : aucun repli ne le nomme.
        assert any(a.provider == AUTORITE_BNF for a in report.attempts)

    @pytest.mark.asyncio
    async def test_the_volume_guard_still_rejects_a_wrong_tome(self):
        """`matches_requested_volume` s'applique aux notices de la conjonction."""
        service = _service_ok()
        wrong = BookMetadata(
            title="Blacksad", author="Juan Díaz Canales", isbn13="9782000000002",
            source=AUTORITE_BNF, volume_number=4,
        )

        async def recorded(_cql, *_args, **_kwargs):
            return _ok([wrong])

        service._bnf_records_result = recorded  # type: ignore[method-assign]
        report = await service.resolve_for_alert_report("Blacksad tome 1", "Juan Díaz Canales")

        assert report.resolution is None or report.resolution.best.isbn13 != "9782000000002"

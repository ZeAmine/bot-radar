"""Contrat de recherche bibliographique partagé (Lots 0 à 2).

Ce module est l'adaptateur minimal entre les méthodes existantes de
`BookMetadataService` et un contrat de réponse exploitable par de futurs
endpoints (`GET /api/v1/books/search`, `GET /api/v1/books/isbn/{isbn}`) comme
par le parcours Telegram. Aucune route HTTP, aucun changement Telegram.

Mapping explicite (les verdicts métier ne sont pas remplacés) :

- recherche par ISBN (`consult_authorities_report`) :
  - `confirme` (+ titre concordant si attendu) -> `found` ;
  - `contradiction` -> `not_found` pour le couple demandé, l'ouvrage réel
    étant exposé en `conflict`, jamais en `reference` ;
  - `inconnu`, toutes recherches réussies -> `not_found` ;
  - `inconnu` avec une tentative en échec -> `unavailable` ;
  - `silence` sans notice -> `unavailable` ;
  - `silence` avec notice de repli -> `partial` : un candidat existe mais,
    la BNF n'ayant pas répondu, il ne confirme rien (règle existante
    préservée : un repli ne confirme jamais pendant une panne BNF).
- recherche par titre (`resolve_for_alert_report`) :
  - résolution non ambiguë avec ISBN, BNF répondante -> `found` ;
  - résolution non ambiguë issue d'un repli pendant une panne BNF ->
    `partial` (correction T9 : le retour historique, lui, la présente comme
    identifiée — comportement Telegram inchangé dans ce lot, ADR intacts) ;
  - `is_ambiguous` -> `ambiguous`, œuvres en `works`, aucune édition ;
  - `None`, toutes recherches réussies -> `not_found` ;
  - `None` avec une tentative en échec -> `unavailable` ;
  - notice sans ISBN (cas Google réel) -> `partial`.

Invariants du contrat : `reference` n'est renseignée que sur `found`
(une identité confirmée se distingue toujours des candidats) ; `conflict`
n'apparaît que sur `not_found` ; les éditions d'ISBN différents restent
séparées (jamais fusionnées) ; toute métadonnée inconnue vaut `None`.
Un ISBN mal formé est rejeté (`ValueError`) avant tout appel réseau, jamais
représenté par `not_found` ; le chemin historique garde `inconnu`.

Observabilité (Lot 2) : chaque rapport porte ses `attempts` (provider, type
de recherche, statut, brut vide ou non, code contrôlé). `consulted` en
dérive (ordre réel, sans doublons) ; `unavailable` nomme les échecs établis.
Un provider désactivé n'est ni consulté ni en panne. `has_results` vient des
éléments bruts reçus, avant parsing, scoring et filtrage.

Garanties : lecture seule (aucune écriture `Book` / `AlertBook`), aucun appel LLM,
aucun cache bibliographique ajouté ni migration. L'ingestion peut consulter ce
contrat pour confirmer un ISBN vendeur après tous les envois ; il reste absent
du matching et du chemin synchrone avant Telegram.
"""

from dataclasses import dataclass, replace
from typing import Any

from app.domain.enums import BookSearchStatus, ProviderQueryStatus
from app.services.book_metadata_service import (
    AUTORITE_BNF,
    TITLE_SIMILARITY_MIN,
    BibliographicReport,
    BookMetadata,
    BookMetadataService,
    BookResolution,
    IsbnVerdict,
    ProviderAttempt,
    SeriesVolume,
    ambiguity_note,
    consulted_authorities,
    failed_authorities,
    matches_requested_volume,
    provenance_note,
    title_similarity,
)
from app.utils.isbn import is_valid_isbn, normalize_isbn


class InvalidIsbnError(ValueError):
    """Un ISBN mal formé, rejeté avant tout appel réseau.

    Sous-classe dédiée (et non `ValueError` nu) pour que la route HTTP ne
    convertisse en 422 que cette erreur de validation précise : une autre
    `ValueError` reste une erreur interne inattendue (500 générique).
    """


@dataclass(frozen=True)
class BookWorkChoice:
    """Une œuvre candidate, telle qu'une autorité la nomme.

    `provenance` est la source de la notice (niveau notice uniquement) : elle
    vaut `None` quand elle n'est pas réellement connue, jamais une provenance
    par champ déduite d'une provenance globale. Les rôles des contributeurs ne
    sont pas portés : le service ne les préserve pas (un seul nom d'auteur,
    mentions de rôle retirées). La langue provient uniquement de la notice de l’édition exacte ;
    aucun préfixe ISBN ne prouve une langue.
    Le caractère numérique non plus : `printType` n'est même pas lu, et
    `printType=BOOK` ne serait pas une preuve d'édition papier.
    """

    title: str
    isbn13: str | None = None
    author: str | None = None
    publisher: str | None = None
    publication_year: int | None = None
    provenance: str | None = None
    volume_number: int | None = None
    language: str | None = None


@dataclass(frozen=True)
class BookEditionChoice:
    """Une édition d'ISBN donné, jamais fusionnée avec une autre. Champs
    inconnus à `None`, langue d’édition attestée uniquement."""

    isbn13: str
    publisher: str | None = None
    publication_year: int | None = None
    format_detail: str | None = None
    provenance: str | None = None
    volume_number: int | None = None
    language: str | None = None


@dataclass(frozen=True)
class BookSearchResult:
    """Réponse normalisée d'une recherche bibliographique.

    `consulted` nomme les autorités réellement interrogées, dans l'ordre réel
    et sans doublons ; `()` signifie « personne » (ISBN mal formé, titre vide),
    jamais « inconnu ». `unavailable` nomme les autorités dont l'échec est
    établi. `attempts` porte les tentatives (provider, type de recherche,
    statut, brut vide ou non, code contrôlé) ; `consulted=None` ne survit que
    dans les adaptateurs purs historiques sans rapport.

    `confirmed` est la vue œuvre de la notice qui **confirme** l'identité :
    renseignée uniquement sur `found` (une autorité a nommé l'ouvrage), `None`
    dès qu'il y a ambiguïté, repli non confirmé ou absence. C'est la seule
    source de `title`/`author` faisant autorité pour le brouillon. `is_series`
    est calculé par le cas d'usage via `series_signal` (il possède le titre
    demandé) ; `False` sur le chemin ISBN (tome explicitement demandé).
    """

    status: BookSearchStatus
    works: tuple[BookWorkChoice, ...] = ()
    editions: tuple[BookEditionChoice, ...] = ()
    reference: BookEditionChoice | None = None
    consulted: tuple[str, ...] | None = None
    unavailable: tuple[str, ...] = ()
    provenance: str | None = None
    conflict: BookWorkChoice | None = None
    note: str | None = None
    attempts: tuple[ProviderAttempt, ...] = ()
    confirmed: BookWorkChoice | None = None
    is_series: bool = False
    series_volumes: tuple[SeriesVolume, ...] = ()
    series_truncated: bool = False
    series_total: int | None = None
    series_inconsistent: bool = False

    def __post_init__(self) -> None:
        if (self.reference is None) == (self.status == BookSearchStatus.FOUND):
            raise ValueError("reference n'est renseignée que sur found")
        if self.conflict is not None and self.status != BookSearchStatus.NOT_FOUND:
            raise ValueError("conflict n'apparaît que sur not_found")


def _work_choice(meta: BookMetadata) -> BookWorkChoice:
    """Un choix d'œuvre depuis une notice, provenance réelle uniquement."""
    return BookWorkChoice(
        title=meta.title,
        isbn13=meta.isbn13,
        author=meta.author,
        publisher=meta.publisher,
        publication_year=meta.publication_year,
        provenance=meta.source,
        volume_number=meta.volume_number,
        language=meta.language,
    )


def _edition_choice(meta: BookMetadata, provenance: str | None) -> BookEditionChoice:
    """Un choix d'édition depuis une notice, sans inventer de provenance."""
    isbn13 = meta.isbn13
    assert isbn13 is not None
    return BookEditionChoice(
        isbn13=isbn13,
        publisher=meta.publisher,
        publication_year=meta.publication_year,
        format_detail=meta.format_detail,
        provenance=provenance,
        volume_number=meta.volume_number,
        language=meta.language,
    )


def _sources_by_isbn(resolution: BookResolution) -> dict[str, str]:
    """Provenance réelle par ISBN, lue sur les notices (pas de déduction)."""
    sources: dict[str, str] = {}
    for meta in (resolution.best, *resolution.alternatives, *resolution.other_works):
        if meta.isbn13 and meta.isbn13 not in sources:
            sources[meta.isbn13] = meta.source
    return sources


def result_from_resolution(
    resolution: BookResolution | None, query_title: str | None = None
) -> BookSearchResult:
    """Adapte une résolution par titre vers le contrat partagé.

    `None` vaut `not_found` avec `consulted=None` : le service ne dit pas
    aujourd'hui si c'est une absence ou une panne (limitation documentée en
    tête de module), et l'adaptateur ne tranche pas à sa place.
    `query_title` alimente `is_series` via `series_signal` (le cas d'usage
    le passe ; sans lui, `False`).
    """
    if resolution is None:
        return BookSearchResult(status=BookSearchStatus.NOT_FOUND, consulted=None)
    if resolution.is_ambiguous:
        works = tuple(
            _work_choice(meta) for meta in _choice_metas(resolution) if meta.isbn13
        )
        if not works:
            works = (_work_choice(resolution.best),)
        return BookSearchResult(
            status=BookSearchStatus.AMBIGUOUS,
            works=works,
            consulted=None,
            note=ambiguity_note(resolution),
        )
    best = resolution.best
    series = resolution.series_signal(query_title) if query_title else None
    if not best.isbn13:
        return BookSearchResult(
            status=BookSearchStatus.PARTIAL,
            works=(_work_choice(best),),
            consulted=None,
            provenance=best.source,
            note="Ouvrage nommé sans édition confirmée : la notice ne porte aucun ISBN.",
            is_series=bool(series and series.is_series),
        )
    sources = _sources_by_isbn(resolution)
    by_isbn = _metas_by_isbn(resolution)
    editions = tuple(
        _edition_choice(by_isbn[edition.isbn13], sources.get(edition.isbn13))
        for edition in resolution.editions()
    )
    return BookSearchResult(
        status=BookSearchStatus.FOUND,
        editions=editions,
        reference=editions[0] if editions else None,
        consulted=None,
        provenance=best.source,
        note=provenance_note(best),
        confirmed=_work_choice(best),
        is_series=bool(series and series.is_series),
    )


def _choice_metas(resolution: BookResolution) -> list[BookMetadata]:
    """Notices homonymes uniques par ISBN, notice retenue en tête."""
    seen: set[str] = set()
    metas: list[BookMetadata] = []
    for meta in (resolution.best, *resolution.other_works):
        if not meta.isbn13 or meta.isbn13 in seen:
            continue
        seen.add(meta.isbn13)
        metas.append(meta)
    return metas


def _metas_by_isbn(resolution: BookResolution) -> dict[str, BookMetadata]:
    """Notices d'une même œuvre indexées par ISBN, référence en tête."""
    by_isbn: dict[str, BookMetadata] = {}
    for meta in (resolution.best, *resolution.alternatives):
        if meta.isbn13 and meta.isbn13 not in by_isbn:
            by_isbn[meta.isbn13] = meta
    return by_isbn


def result_from_isbn_verdict(
    verdict: IsbnVerdict, expected_title: str | None = None
) -> BookSearchResult:
    """Adapte un verdict ISBN vers le contrat partagé.

    `expected_title` rejoue le contrôle de titre au même seuil que
    `confirm_isbn_for_title` : un ISBN rattaché à un autre ouvrage vaut
    `not_found` pour le couple demandé, avec l'ouvrage réel en `conflict`.
    Aucun titre de référence n'est exigé pour une recherche par ISBN seul.
    """
    notice = verdict.notice
    contradicts = verdict.outcome == "contradiction" or (
        verdict.outcome == "confirme"
        and notice is not None
        and bool(expected_title)
        and (
            title_similarity(notice.title, expected_title) < TITLE_SIMILARITY_MIN
            or not matches_requested_volume(notice, expected_title or "")
        )
    )
    if verdict.outcome == "confirme" and not contradicts and notice is not None:
        reference = _edition_choice(notice, notice.source)
        return BookSearchResult(
            status=BookSearchStatus.FOUND,
            editions=(reference,),
            reference=reference,
            consulted=verdict.consulted,
            provenance=notice.source,
            note=provenance_note(notice),
            confirmed=_work_choice(notice),
        )
    if contradicts and notice is not None:
        author = notice.author or "un auteur non renseigné"
        return BookSearchResult(
            status=BookSearchStatus.NOT_FOUND,
            consulted=verdict.consulted,
            conflict=_work_choice(notice),
            note=(
                f"Cet ISBN désigne « {notice.title} » de {author}, "
                "pas l'ouvrage demandé."
            ),
        )
    if verdict.outcome == "silence":
        if notice is None:
            return BookSearchResult(
                status=BookSearchStatus.UNAVAILABLE,
                consulted=verdict.consulted,
                unavailable=(AUTORITE_BNF,),
                note=f"Autorité indisponible : {verdict.panne}",
            )
        return BookSearchResult(
            status=BookSearchStatus.PARTIAL,
            works=(_work_choice(notice),),
            consulted=verdict.consulted,
            unavailable=(AUTORITE_BNF,),
            provenance=notice.source,
            note=(
                "Candidat de repli sans valeur de confirmation : "
                "la BNF n'a pas répondu."
            ),
        )
    return BookSearchResult(
        status=BookSearchStatus.NOT_FOUND, consulted=verdict.consulted
    )


def _bnf_failed(attempts: tuple[ProviderAttempt, ...]) -> bool:
    """La BNF a-t-elle manqué au moins une requête tentée ?"""
    return any(
        attempt.provider == AUTORITE_BNF
        and attempt.status != ProviderQueryStatus.SUCCESS
        for attempt in attempts
    )


def result_from_report(
    report: BibliographicReport, query_title: str | None = None
) -> BookSearchResult:
    """Chemin titre depuis un rapport : décisions existantes + règle T9.

    Le retour historique (`resolve_for_alert`, Telegram dans ce lot) accepte
    un repli pendant une panne BNF comme une identification. Le nouveau
    contrat, lui, ne confirme jamais dans ce cas : candidat non ambigu vers
    `partial` (`reference=None`), homonymes vers `ambiguous`, rien vers
    `unavailable`. Les ADR d'autorité sont inchangés : c'est la lecture du
    rapport qui diffère, pas le service.
    """
    attempts = report.attempts
    consulted = consulted_authorities(attempts)
    failed = failed_authorities(attempts)
    resolution = report.resolution
    if resolution is None:
        if failed:
            return BookSearchResult(
                status=BookSearchStatus.UNAVAILABLE,
                consulted=consulted,
                unavailable=failed,
                attempts=attempts,
                note="Aucun candidat exploitable et une autorité n'a pas répondu.",
            )
        return BookSearchResult(
            status=BookSearchStatus.NOT_FOUND,
            consulted=consulted,
            attempts=attempts,
        )
    base = result_from_resolution(resolution, query_title)
    if (
        base.status == BookSearchStatus.FOUND
        and _bnf_failed(attempts)
        and resolution.best.source != AUTORITE_BNF
    ):
        best = resolution.best
        return BookSearchResult(
            status=BookSearchStatus.PARTIAL,
            works=(_work_choice(best),),
            consulted=consulted,
            unavailable=failed,
            provenance=best.source,
            attempts=attempts,
            note=(
                "Candidat de repli sans valeur de confirmation : "
                "la BNF n'a pas répondu. Le retour historique de "
                "resolve_for_alert le présente, lui, comme identifié "
                "(comportement Telegram inchangé dans ce lot)."
            ),
        )
    return replace(base, consulted=consulted, unavailable=failed, attempts=attempts)


def result_from_verdict_report(
    report: BibliographicReport, expected_title: str | None = None
) -> BookSearchResult:
    """Chemin ISBN depuis un rapport : verdict intact, contexte réel attaché.

    Un `not_found` sans aucun candidat (ni `reference`, ni `works`, ni
    `conflict`) ne prend jamais le pas sur des tentatives en échec : il vaut
    alors `unavailable`. Une contradiction explicite reste distincte d'une
    panne, et une identité confirmée reste `found`, échecs secondaires ou pas.
    """
    verdict = report.verdict
    assert verdict is not None
    base = result_from_isbn_verdict(verdict, expected_title)
    # Sans tentative dans le rapport (adaptateur sans observabilité), la liste du
    # verdict fait foi : un vide inventerait « personne consulté ».
    consulted = consulted_authorities(report.attempts) or verdict.consulted
    failed = failed_authorities(report.attempts)
    if (
        base.status == BookSearchStatus.NOT_FOUND
        and not base.works
        and base.conflict is None
        and failed
    ):
        return replace(
            base,
            status=BookSearchStatus.UNAVAILABLE,
            consulted=consulted,
            unavailable=failed,
            attempts=report.attempts,
            note="Aucun candidat exploitable et une autorité n'a pas répondu.",
        )
    return replace(
        base,
        consulted=consulted,
        unavailable=failed,
        attempts=report.attempts,
    )


def metadata_from_result(result: BookSearchResult) -> BookMetadata | None:
    """Notice d'autorité d'un résultat `found`, pour les appelants qui en veulent une.

    Projection unique partagée (Telegram, natural_add) : elle refuse tout statut
    qui n'est pas `found`, donc elle ne peut pas faire passer un candidat de repli
    pour une identité confirmée.
    """
    if result.status != BookSearchStatus.FOUND or result.confirmed is None:
        return None
    return BookMetadata(
        title=result.confirmed.title,
        author=result.confirmed.author,
        publisher=result.confirmed.publisher,
        isbn13=result.reference.isbn13 if result.reference else result.confirmed.isbn13,
        publication_year=result.confirmed.publication_year,
        # La mise en forme appartient à l'édition, pas à l'œuvre : elle vit
        # sur la référence.
        format_detail=result.reference.format_detail if result.reference else None,
        source=result.confirmed.provenance or AUTORITE_BNF,
        volume_number=result.confirmed.volume_number,
        language=result.reference.language if result.reference else None,
    )




def series_bibliographic_identity(volumes: tuple[SeriesVolume, ...]) -> dict[str, Any]:
    """Champs communs des éditions confirmées sélectionnées, sans supposer ceux qui manquent."""
    def common(field: str) -> Any:
        values = {getattr(v.notice, field) for v in volumes if v.isbn13}
        return next(iter(values)) if len(values) == 1 else None
    sources = list(dict.fromkeys(v.notice.source for v in volumes if v.isbn13 and v.notice.source))
    return {
        "author": common("author"), "publisher": common("publisher"),
        "language": common("language"), "format_detail": common("format_detail"),
        "publication_year": None,
        "source": sources[0] if len(sources) == 1 else None,
        "identification_sources": sources,
    }


def creation_bibliographic_identity(result: BookSearchResult) -> dict[str, Any]:
    """Identité confirmée du brouillon, commune aux voies manuelle et naturelle."""
    def common(field: str) -> Any:
        values = {getattr(v.notice, field) for v in result.series_volumes if v.isbn13}
        return next(iter(values)) if len(values) == 1 else None
    confirmed = result.confirmed
    reference = result.reference
    return {
        "author": confirmed.author if confirmed else None,
        "publisher": common("publisher") if result.is_series else (
            confirmed.publisher if confirmed else None),
        "language": common("language") if result.is_series else (
            reference.language if reference else None),
        "publication_year": None if result.is_series else (
            confirmed.publication_year if confirmed else None),
        "format_detail": common("format_detail") if result.is_series else (
            reference.format_detail if reference else None),
        "source": result.provenance,
        **(series_bibliographic_identity(result.series_volumes) if result.series_volumes else {}),
        "series_volumes": [v.stored() for v in result.series_volumes],
        "series_reference_publisher": result.confirmed.publisher if result.confirmed else None,
        "series_truncated": result.series_truncated,
        "series_total": result.series_total,
        "series_inconsistent": result.series_inconsistent,
    }


class BookSearchUseCase:
    """Orchestration de recherche partagée (futurs endpoints et Telegram).

    Lecture seule : le cas d'usage interroge le service bibliographique et
    mappe son résultat. Il n'écrit ni `Book` ni `AlertBook`, n'appelle aucun
    LLM, ne pose aucun cache, et n'est branché sur aucun chemin d'alerte.
    """

    def __init__(self, service: BookMetadataService | None = None) -> None:
        self._service = service or BookMetadataService()

    async def search_by_title(
        self, title: str, author_hint: str | None = None, *,
        include_series_catalogue: bool = False,
    ) -> BookSearchResult:
        """Recherche par titre (+ indice d'auteur), adaptée au contrat."""
        report = await self._service.resolve_for_alert_report(title, author_hint)
        result = result_from_report(report, query_title=title)
        if result.is_series and include_series_catalogue:
            catalogue = await self._service.lookup_series_notices(
                title,
                author_hint=author_hint or (result.confirmed.author if result.confirmed else None),
                for_creation=True,
                reference_publisher=result.confirmed.publisher if result.confirmed else None,
            )
            result = replace(
                result, series_volumes=catalogue.volumes, series_truncated=catalogue.truncated,
                series_total=catalogue.declared_volume_count,
                series_inconsistent=catalogue.inconsistent_counts,
            )
        return result

    async def search_by_isbn(
        self, isbn: str, expected_title: str | None = None
    ) -> BookSearchResult:
        """Recherche d'une édition par ISBN, adaptée au contrat.

        Un code mal formé est rejeté explicitement avant tout appel réseau :
        il ne vaut jamais `not_found`. Le comportement historique
        (`consult_authorities` → `inconnu`) est préservé pour ses appelants.
        """
        clean_isbn = normalize_isbn(isbn)
        if not clean_isbn or not is_valid_isbn(clean_isbn):
            raise InvalidIsbnError(
                f"ISBN invalide : {isbn.strip()} — aucun provider interrogé."
            )
        report = await self._service.consult_authorities_report(isbn)
        return result_from_verdict_report(report, expected_title)

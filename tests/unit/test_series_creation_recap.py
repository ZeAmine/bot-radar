"""Régressions Bird King : une ligne par tome, édition française et identité explicite."""

import re
from unittest.mock import AsyncMock, patch

import pytest

from app.bot.handlers.alerts_helpers import RECAP_TITLE, creation_card, draft_updates_from_search
from app.domain.enums import ProviderQueryStatus
from app.providers.google_books import ProviderQueryResult
from app.services.book_metadata_service import BookMetadata, BookMetadataService, BookResolution
from app.use_cases.book_search import result_from_resolution
from tests.fakes_catalogue import MARCXCHANGE_SCHEMAS, patch_bnf_admission
from tests.fakes_market_source import source_history_values

CODES = ('9782369904151', '9782369903086', '9782205051407', '9781444977387')


def bird(n, *, isbn=None, language='fr', label='Livre'):
    return BookMetadata(
        title='Birdking', volume_title=f'Birdking, {label} {n}', volume_number=n,
        author='Daniel Freedman', publisher='Éditeur test', language=language,
        isbn13=isbn, publication_year=2024, format_detail='Album',
    )


@pytest.mark.parametrize('series', [False, True])
def test_bd_identity_has_all_fields_even_when_unknown(series):
    text = creation_card({'name': 'Birdking', 'is_series': series, 'category_slug': 'bd_comics'})
    assert text.startswith(RECAP_TITLE)
    for field in ('Langue', 'Auteur', 'Éditeur', 'Année', 'Format', 'Source'):
        assert f'<b>{field} :</b> Non renseigné' in text


def test_series_keeps_confirmed_author_but_no_global_isbn():
    result = result_from_resolution(
        BookResolution(best=bird(1, isbn=CODES[0]), notice_titles=('Birdking Livre 1',)),
        'Birdking',
    )
    draft = draft_updates_from_search(result, query_title='Birdking')
    assert draft['author'] == 'Daniel Freedman'
    assert draft['isbn'] is None
    assert '<b>Auteur :</b> Daniel Freedman' in creation_card(draft)


async def test_catalogue_keeps_four_volumes_without_isbn_and_sorts():
    service = BookMetadataService()
    notices = [bird(3, isbn=CODES[2]), bird(1, isbn=CODES[0]), bird(4), bird(2, isbn=CODES[1])]
    with patch.object(service, '_bnf_records_result', AsyncMock(return_value=(
        ProviderQueryResult(items=tuple(notices), status=ProviderQueryStatus.SUCCESS), None
    ))), patch.object(service, '_openlibrary_works', AsyncMock(return_value=(
        [], ProviderQueryResult()
    ))):
        verdict = await service.lookup_series_notices(
            'Birdking', author_hint='Daniel Freedman', for_creation=True,
        )
    assert [v.number for v in verdict.volumes] == [1, 2, 3, 4]
    assert verdict.volumes[-1].isbn13 is None


def test_recap_four_rows_one_vocabulary_and_named_rejection():
    """Quatre tomes détectés : trois retenus avec leur cote, un refusé et nommé."""
    rows = [
        {'number': n, 'title': f'Birdking Livre {n}', 'isbn13': CODES[n-1],
         'language': 'fr', 'source': 'bnf', 'confirmed': True, 'publication_year': 2024,
         'price_lines': [
             '<b>Cote :</b> 18,00 € (Momox)',
             '<b>Prix bas :</b> 16,50 €',
         ]}
        for n in (1, 2, 3)
    ] + [{'number': 4, 'title': 'Birdking Livre 4', 'isbn13': None, 'confirmed': False,
          'admission': 'no_bnf_notice'}]
    text = creation_card({'name': 'Birdking', 'is_series': True, 'series_volumes': rows})
    assert re.findall(r'📚 <b>Livre (\d+)</b>', text) == ['1', '2', '3']
    assert 'Volume 1' not in text
    assert 'Tome 1' not in text
    for code in CODES[:3]:
        assert text.count(code) == 1
    assert text.count('18,00 €') == 3
    assert text.count('16,50 €') == 3
    assert '<b>Parution :</b> 2024 (BNF, année prouvée)' in text
    assert 'ISBN-13' not in text
    assert 'Cote indisponible' not in text
    # Le tome écarté n'a pas de fiche : il est nommé une fois, avec son motif.
    assert 'Livre 4' in text.split('sur demande explicite')[1]
    assert 'aucune notice BNF' in text
    assert 'Complétude non confirmée' in text


async def test_bnf_pagination_and_verified_french_edition_reach_volume_four():
    """Une page de notices ne vaut pas une série ; les cotes restent par édition."""
    import httpx

    from app.utils.isbn import isbn10_to_isbn13
    from tests import fakes_catalogue
    from tests.fakes_catalogue import BNF_RECORD_TEMPLATE, SRU_HEADER

    codes = [isbn10_to_isbn13(f"{i:09d}0") for i in range(100, 107)]
    entries = [(n, "Volume", "eng", codes[n-1]) for n in (1, 2, 3)]
    entries += [(n, "Livre", "fre", codes[n+2]) for n in (1, 2, 3, 4)]
    requests = []

    def record(entry):
        number, label, language, code = entry
        return BNF_RECORD_TEMPLATE.format(
            ark="123", title=f"Birdking. {label} {number} / Daniel Freedman",
            creator="Daniel Freedman", publisher="Éditeur test",
            date="<dc:date>2024</dc:date>", fmt="Album", isbn=code,
        ).replace("<dc:format>", f"<dc:language>{language}</dc:language><dc:format>")

    def transport(request):
        requests.append(request)
        if request.url.host == "openlibrary.org":
            return httpx.Response(200, json={"docs": []})
        query = request.url.params["query"]
        if "bib.isbn" in query:
            selected = [entry for entry in entries if entry[3] in query]
        else:
            start = int(request.url.params.get("startRecord", "1"))
            selected = entries[start-1:start+2]
        rendered = [record(entry) for entry in selected]
        if request.url.params.get("recordSchema") in MARCXCHANGE_SCHEMAS:
            # La preuve de parution ne se lit que dans la notice MarcXchange.
            rendered = [fakes_catalogue.marcxchange_from_dc(notice) for notice in rendered]
        xml = SRU_HEADER.format(count=len(entries) if "bib.isbn" not in query else len(selected),
                                records="".join(rendered))
        return httpx.Response(200, text=xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        verdict = await BookMetadataService(http_client=client).lookup_series_notices(
            "Birdking", "Daniel Freedman", for_creation=True,
        )
    assert [v.number for v in verdict.volumes] == [1, 2, 3, 4]
    assert [v.isbn13 for v in verdict.volumes] == codes[3:]
    assert all(v.notice.language == "fr" for v in verdict.volumes)
    assert any(request.url.params.get("startRecord") == "7" for request in requests)


async def test_declared_total_no_longer_fabricates_missing_volumes():
    """« 4 tomes annoncés » n'autorise pas à inventer les tomes 2, 3 et 4.

    Le total déclaré reste affiché comme une complétude non vérifiée ; il ne crée
    plus de lignes sans ISBN, qui laissaient croire à des tomes catalogués.
    """
    service = BookMetadataService()
    notice = bird(1, isbn=CODES[0])
    notice.declared_volume_count = 4
    with patch.object(service, '_bnf_records_result', AsyncMock(return_value=(
        ProviderQueryResult(items=(notice,)), None
    ))), patch.object(service, '_openlibrary_works', AsyncMock(return_value=(
        [], ProviderQueryResult()
    ))), patch.object(service, 'verify_isbn', AsyncMock(return_value=notice)), \
            patch_bnf_admission(service, [notice], series='Birdking'):
        verdict = await service.lookup_series_notices(
            'Birdking', author_hint='Daniel Freedman', for_creation=True,
        )
    assert verdict.declared_volume_count == 4
    assert [v.number for v in verdict.volumes] == [1]
    assert [v.isbn13 for v in verdict.volumes] == [CODES[0]]


@pytest.mark.parametrize('verified', [None, BookMetadata(
    title='Autre œuvre', isbn13=CODES[3], volume_number=4, source='openlibrary',
)])
async def test_unconfirmed_replacement_never_becomes_confirmed_isbn(verified):
    from types import SimpleNamespace

    from app.services.book_metadata_service import AUTORITE_OPENLIBRARY

    service = BookMetadataService()
    fourth = bird(4, isbn=CODES[3])
    fourth.source = AUTORITE_OPENLIBRARY
    with patch.object(service, '_bnf_records_result', AsyncMock(return_value=(
        ProviderQueryResult(), None
    ))), patch.object(service, '_openlibrary_works', AsyncMock(return_value=(
        [SimpleNamespace(meta=fourth, edition_isbns=[CODES[3]])], ProviderQueryResult()
    ))), patch.object(service, 'verify_isbn', AsyncMock(return_value=verified)):
        verdict = await service.lookup_series_notices(
            'Birdking', author_hint='Daniel Freedman', for_creation=True,
        )
    assert [v.number for v in verdict.volumes] == [4]
    assert verdict.volumes[0].isbn13 is None
    assert verdict.notices == ()


def test_isbn_normalization_and_french_priority_are_independent_of_input_order():
    from app.services.book_metadata_service import SeriesVolume, select_series_volumes

    french = bird(1, isbn=CODES[0])
    english = bird(1, isbn=CODES[3], language='en', label='Volume')
    candidates = [SeriesVolume(1, english, CODES[3]),
                  SeriesVolume(2, bird(2, isbn=CODES[1]), CODES[1]),
                  SeriesVolume(1, french, '978-2-36990-415-1'),
                  SeriesVolume(1, french, CODES[0]),
                  SeriesVolume(1, french, '2369904151')]
    for ordered in (candidates, list(reversed(candidates))):
        volumes = select_series_volumes(ordered)
        assert [v.number for v in volumes] == [1, 2]
        assert [v.isbn13 for v in volumes] == [CODES[0], CODES[1]]


def test_summary_names_the_failure_instead_of_showing_an_empty_field():
    """Un livre absent n'a pas « zéro offre » : sa cote n'a pas pu être lue."""
    from app.services.market_price_service import MarketPriceService

    summary = MarketPriceService.get_comparison_summary(None, None, for_creation=True)
    assert '<b>Cote :</b> récupération impossible, la source n\'a pas répondu' in summary
    assert '<b>Prix bas constaté (1 an) :</b> historique indisponible' in summary
    for empty in ('Non renseigné', 'Cote indisponible'):
        assert empty not in summary


@pytest.mark.parametrize('discovery', [
    'same', 'confirms_missing', 'unavailable', 'french_replaces_english',
    'initial_unknown', 'old_book_title', 'contradiction',
])
async def test_birdking_creation_confirms_four_rows_from_prepared_local_caches(
    monkeypatch, discovery,
):
    """La vraie création relit quatre cotes en session, une seule édition par numéro."""
    import uuid
    from datetime import UTC, datetime, timedelta

    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from sqlalchemy import select

    from app.bot.handlers.alerts_create import save_alert
    from app.db.models.alert import Alert
    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.db.models.category import Category
    from app.db.models.user import User
    from app.domain.enums import AlertStatus, ComparisonStatus
    from app.services.book_metadata_service import CatalogueVerdict, SeriesVolume
    from app.use_cases import link_bd_catalog, prepare_bd_alert
    from tests.add_fakes import DraftState
    from tests.database import sqlite_only_database
    from tests.screens import confirm_press

    class State(DraftState):
        def __init__(self):
            super().__init__()
            self.storage = MemoryStorage()
            self.key = StorageKey(bot_id=1, chat_id=42, user_id=42)

    notices = [bird(n, isbn=CODES[n-1]) for n in (1, 2, 3, 4)]
    volumes = tuple(SeriesVolume(n, notices[n-1], CODES[n-1]) for n in (1, 2, 3, 4))
    catalogue = CatalogueVerdict(notices=tuple(notices), volumes=volumes)
    result = result_from_resolution(
        BookResolution(best=notices[0], notice_titles=tuple(m.volume_title for m in notices)),
        'Birdking',
    )
    from dataclasses import replace
    initial = volumes
    if discovery == 'initial_unknown':
        initial = tuple(SeriesVolume(n, bird(n)) for n in (1, 2, 3, 4))
    if discovery == 'french_replaces_english':
        english = bird(1, isbn='9780306406157', language='en', label='Volume')
        initial = (SeriesVolume(1, english, english.isbn13), *volumes[1:])
    if discovery == 'confirms_missing':
        initial = (*volumes[:3], SeriesVolume(4, bird(4)))
    result = replace(result, series_volumes=initial)
    if discovery == 'contradiction':
        catalogue = CatalogueVerdict(
            notices=tuple(notices[1:]), volumes=(SeriesVolume(1, bird(1)), *volumes[1:]),
            rejected_isbns=(CODES[0],),
        )
    if discovery in ('unavailable', 'old_book_title'):
        catalogue = CatalogueVerdict(consulted=('bnf',), unavailable=('bnf',))
    state = State()
    state.data = draft_updates_from_search(result, query_title='Birdking')
    callback, message = confirm_press()

    async with sqlite_only_database() as db:
        db.maker.configure(autoflush=False)
        user = User(telegram_user_id=601, is_allowed=True)
        db.session.add_all([
            user, Category(slug='bd_comics', label='BD', emoji='B', is_active=True),
        ])
        if discovery == 'old_book_title':
            db.session.add(Book(title='Birdking', normalized_title='birdking', isbn13=CODES[0]))
        await db.session.commit()
        monkeypatch.setattr('app.db.session.AsyncSessionLocal', db.maker)
        monkeypatch.setattr(prepare_bd_alert, 'AsyncSessionLocal', db.maker)
        monkeypatch.setattr(prepare_bd_alert, 'preparation_runner',
                            prepare_bd_alert.PreparationRunner())
        monkeypatch.setattr(BookMetadataService, 'lookup_series_notices',
                            AsyncMock(return_value=catalogue))

        async def collect(book_id, *, reason):
            assert reason == 'bnf_link'
            async with db.maker() as session:
                alert = (await session.execute(select(Alert))).scalar_one()
                assert alert.status == AlertStatus.PAUSED
                book = await session.get(Book, book_id)
                n = CODES.index(book.isbn13) + 1
                now = datetime.now(UTC)
                old = now - timedelta(days=30)
                cache = BookMarketCache(
                    book_id=book_id, lowest_delivered_price_cents=n*1000+800,
                    checked_at=now, expires_at=now+timedelta(days=1),
                    comparison_status=ComparisonStatus.AVAILABLE,
                    annual_low_cents=n*1000+650, annual_low_observed_at=old,
                    **source_history_values(n*1000+650, now),
                    annual_low_snapshot_id=uuid.uuid4(), annual_low_provider='synthetic',
                    annual_first_at=old, annual_last_at=now, annual_count=2,
                    annual_computed_at=now, annual_expires_at=old+timedelta(days=365),
                )
                session.add(cache)
                await session.commit()
                return cache

        monkeypatch.setattr(link_bd_catalog, 'price_book_now', collect)
        await save_alert(callback, state, user, db.session)
        text = message.edit_text.call_args.args[0]
        expected_codes = CODES[1:] if discovery == 'contradiction' else CODES
        retained = [str(n) for n, code in enumerate(CODES, 1) if code in expected_codes]
        assert re.findall(r'📚 <b>Livre (\d+)</b>', text) == retained
        for n, code in enumerate(CODES, 1):
            if code not in expected_codes:
                # Le tome sans notice prouvée n'est plus une fiche à prix absent :
                # il est nommé une fois, parmi les tomes détectés et non ajoutés.
                assert code not in text
                assert f'Livre {n}' in text.split('sur demande explicite')[1]
                assert 'Cote indisponible' not in text
                continue
            assert text.count(code) == 1
            assert f'{n}8,00 €' in text
            assert f'{n}6,50 €' in text
        assert '<b>ISBN :</b>' not in text
        assert '<b>Langue :</b> Français' in text
        assert '<b>Auteur :</b> Daniel Freedman' in text
        async with db.maker() as observer:
            alert = (await observer.execute(select(Alert))).scalar_one()
            assert alert.status == AlertStatus.ACTIVE
            assert alert.isbn is None
            from app.db.repositories.book_repo import BookRepository
            linked = await BookRepository(observer).get_all_books_for_alert(alert.id)
            assert {b.isbn13 for b in linked} == set(expected_codes)
            assert len(linked) == len(expected_codes)
            assert all('Livre' in b.title for b in linked)


async def test_openlibrary_multiple_editions_do_not_hide_other_volumes():
    from types import SimpleNamespace

    from app.utils.isbn import isbn10_to_isbn13

    service = BookMetadataService()
    codes = [isbn10_to_isbn13(f"{i:09d}0") for i in range(400, 416)]
    works = [SimpleNamespace(meta=bird(1, isbn=codes[0]), edition_isbns=codes[:13])]
    works += [SimpleNamespace(meta=bird(n, isbn=codes[n+11]), edition_isbns=[codes[n+11]])
              for n in (2, 3, 4)]
    with patch.object(service, '_bnf_records_result', AsyncMock(return_value=(
        ProviderQueryResult(), None
    ))), patch.object(service, '_openlibrary_works', AsyncMock(return_value=(
        works, ProviderQueryResult()
    ))), patch.object(service, 'verify_isbn', AsyncMock(return_value=None)):
        verdict = await service.lookup_series_notices(
            'Birdking', author_hint='Daniel Freedman', for_creation=True,
        )
    assert [v.number for v in verdict.volumes] == [1, 2, 3, 4]


async def test_edition_switch_replaces_year_format_and_source(monkeypatch):
    from app.bot.handlers.alerts_create_confirm import select_alert_creation_edition
    from app.services.book_metadata_service import IsbnVerdict
    from app.use_cases.book_search import result_from_isbn_verdict
    from tests.add_fakes import DraftState
    from tests.screens import confirm_press

    notice = BookMetadata(title='Birdking', isbn13=CODES[0], publication_year=2024,
                          format_detail='Album', source='bnf')
    confirmed = result_from_isbn_verdict(IsbnVerdict(outcome='confirme', notice=notice))
    case = type('Case', (), {'search_by_isbn': AsyncMock(return_value=confirmed)})()
    monkeypatch.setattr(
        'app.bot.handlers.alerts_create_confirm.BookSearchUseCase', lambda **_: case
    )
    state = DraftState()
    state.data = {'name': 'Birdking', 'isbn': CODES[1], 'publication_year': 1995,
                  'format_detail': 'Poche', 'source': 'openlibrary',
                  'edition_alternatives': [{'isbn13': CODES[0]}]}
    callback, message = confirm_press()
    callback.data = 'al_create_edition:0'
    await select_alert_creation_edition(callback, state)
    assert state.data['publication_year'] == 2024
    assert state.data['format_detail'] == 'Album'
    assert state.data['source'] == 'bnf'
    text = message.edit_text.call_args.args[0]
    assert '<b>Année :</b> 2024' in text
    assert '<b>Format :</b> Album' in text
    assert '<b>Source :</b> BNF' in text


async def test_google_completes_the_series_after_bnf_and_openlibrary():
    from types import SimpleNamespace

    from app.providers.google_books import GoogleBooksClient

    service = BookMetadataService()
    calls = []
    parsed = GoogleBooksClient._parse_item({'volumeInfo': {
        'title': 'Birdking', 'subtitle': 'Livre 4', 'authors': ['Daniel Freedman'],
        'language': 'fr', 'industryIdentifiers': [{'type': 'ISBN_13', 'identifier': CODES[3]}],
    }})
    async def bnf(*_args, **_kwargs):
        calls.append('bnf')
        return ProviderQueryResult(), None
    async def ol(*_args, **_kwargs):
        calls.append('openlibrary')
        return [], ProviderQueryResult()
    async def google(*_args):
        calls.append('google_books')
        return ProviderQueryResult(items=(parsed,))
    client = SimpleNamespace(is_configured=True, search_by_title_result=google)
    with patch.object(service, '_bnf_records_result', bnf), \
            patch.object(service, '_openlibrary_works', ol), \
            patch.object(service, '_google_books_client', return_value=client), \
            patch.object(service, 'verify_isbn', AsyncMock(return_value=bird(4, isbn=CODES[3]))), \
            patch_bnf_admission(service, [bird(4, isbn=CODES[3])], series='Birdking'):
        verdict = await service.lookup_series_notices(
            'Birdking', author_hint='Daniel Freedman', for_creation=True,
        )
    assert calls == ['bnf', 'bnf', 'openlibrary', 'google_books']
    assert [v.number for v in verdict.volumes] == [4]
    assert verdict.volumes[0].isbn13 == CODES[3]
    assert verdict.volumes[0].notice.source == 'google_books'
    assert verdict.volumes[0].notice.confidence.value == 'medium'


def test_creation_summary_dates_no_offer_without_promising_a_pending_price():
    from datetime import UTC, datetime

    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.domain.enums import ComparisonStatus
    from app.services.market_price_service import MarketPriceService

    book = Book(title='Birdking Livre 4', normalized_title='birdking livre 4')
    book.market_cache = BookMarketCache(
        comparison_status=ComparisonStatus.UNAVAILABLE,
        collection_status="empty",
        checked_at=datetime(2026, 10, 13, 10, 30, tzinfo=UTC),
        # L'historique a été lu à cette date et n'a rien rendu : c'est un fait daté,
        # pas une lecture ratée.
        historical_checked_at=datetime(2026, 10, 13, 10, 30, tzinfo=UTC),
    )
    summary = MarketPriceService.get_comparison_summary(None, book, for_creation=True)
    assert '<b>Cote :</b> aucune offre trouvée' in summary
    assert '13/10' not in summary
    assert 'en cours' not in summary
    assert '<b>Prix bas constaté (1 an) :</b> aucun relevé sur 12 mois' in summary
    for empty in ('Non renseigné', 'Cote indisponible'):
        assert empty not in summary


async def test_resume_keeps_series_proofs_but_identity_change_discards_them():
    from app.db.models.alert import Alert
    from app.db.models.category import Category
    from app.db.models.user import User
    from app.db.repositories.alert_repo import AlertRepository
    from app.domain.entities.alert_preparation import PreparationRequest, preparation_request
    from app.domain.enums import AlertStatus
    from tests.database import sqlite_only_database

    async with sqlite_only_database() as db:
        user = User(telegram_user_id=610)
        category = Category(slug='bd_comics', label='BD', emoji='B')
        db.session.add_all([user, category])
        await db.session.flush()
        row = {'number': 1, 'title': 'Birdking Livre 1', 'isbn13': CODES[0], 'confirmed': True}
        alert = Alert(user_id=user.id, category_id=category.id, name='Birdking',
                      status=AlertStatus.PAUSED, include_keywords=['birdking'])
        alert.preparation = PreparationRequest(
            state='blocked', series_volumes=[row], author_hint='Daniel Freedman',
        ).stored()
        db.session.add(alert)
        await db.session.commit()
        repo = AlertRepository(db.session)
        await repo.set_status(alert, AlertStatus.ACTIVE)
        assert preparation_request(alert).series_volumes == [row]
        alert.name = 'Autre série'
        await repo.invalidate_preparation(alert, author_hint='Autre auteur')
        assert preparation_request(alert).series_volumes == []


async def test_broad_bnf_search_preserves_its_limit_warning():
    service = BookMetadataService()
    notice = bird(1, isbn=CODES[0])
    with patch.object(service, '_bnf_records_result', AsyncMock(side_effect=[
        (ProviderQueryResult(), None),
        (ProviderQueryResult(items=(notice,), error_code='series_limit'), None),
    ])), patch.object(service, '_openlibrary_works', AsyncMock(return_value=(
        [], ProviderQueryResult()
    ))), patch.object(service, 'verify_isbn', AsyncMock(return_value=notice)):
        verdict = await service.lookup_series_notices(
            'Birdking', author_hint='Daniel Freedman', for_creation=True,
        )
    assert verdict.truncated is True


def test_numbered_work_without_isbn_still_resolves_as_a_series():
    result = result_from_resolution(
        BookResolution(best=bird(4), notice_titles=('Birdking Livre 4',)), 'Birdking',
    )
    assert result.is_series is True
    assert result.reference is None


async def test_typed_isbn_names_a_volume_even_if_the_model_suggests_a_series(monkeypatch):
    from app.services.ai.modules.natural_add import NaturalAddModule
    from tests.fakes_llm import FakeLLM

    notice = bird(2, isbn=CODES[1])
    found = result_from_resolution(
        BookResolution(best=notice, notice_titles=('Birdking Livre 2',)), 'Birdking',
    )
    case = type('Case', (), {'search_by_title': AsyncMock(return_value=found)})()
    monkeypatch.setattr('app.services.ai.modules.natural_add.BookSearchUseCase', lambda **_: case)
    module = NaturalAddModule(client=FakeLLM(text={
        'name': 'Birdking', 'category_slug': 'bd_comics', 'is_series': True,
    }))
    result = await module.parse_prompt(f'Birdking ISBN {CODES[1]}')
    assert result.is_series is False
    assert result.isbn13 == CODES[1]
    assert result.requested_volume == 2


async def test_series_selection_uses_the_reference_publisher_at_equal_priority():
    service = BookMetadataService()
    preferred = bird(1, isbn=CODES[0])
    other = bird(1, isbn=CODES[1])
    other.publisher = 'Autre maison'
    async def verified(code, _title, **_options):
        return preferred if code == CODES[0] else other
    with patch.object(service, '_bnf_records_result', AsyncMock(return_value=(
        ProviderQueryResult(items=(other, preferred)), None
    ))), patch.object(service, '_openlibrary_works', AsyncMock(return_value=(
        [], ProviderQueryResult()
    ))), patch.object(service, 'verify_isbn', verified), \
            patch_bnf_admission(service, [preferred, other], series='Birdking'):
        verdict = await service.lookup_series_notices(
            'Birdking', author_hint='Daniel Freedman', for_creation=True,
            reference_publisher='Éditeur test',
        )
    assert [v.isbn13 for v in verdict.volumes] == [CODES[0]]


async def test_explicit_bnf_contradiction_is_reported_for_initial_proof_invalidation():
    import httpx

    from tests import fakes_catalogue
    from tests.fakes_catalogue import BNF_RECORD_TEMPLATE, SRU_HEADER

    def transport(request):
        if request.url.host == 'openlibrary.org':
            return httpx.Response(200, json={'docs': []})
        query = request.url.params['query']
        title = 'Autre ouvrage' if 'bib.isbn' in query else 'Birdking Livre 1'
        notice = BNF_RECORD_TEMPLATE.format(
            ark='123', title=title, creator='Daniel Freedman', publisher='Éditeur test',
            date='<dc:date>2024</dc:date>', fmt='Album', isbn=CODES[0],
        )
        if request.url.params.get('recordSchema') in MARCXCHANGE_SCHEMAS:
            notice = fakes_catalogue.marcxchange_from_dc(notice)
        return httpx.Response(200, text=SRU_HEADER.format(count=1, records=notice))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        catalogue = await BookMetadataService(http_client=client).lookup_series_notices(
            'Birdking', author_hint='Daniel Freedman', for_creation=True,
        )
    assert catalogue.rejected_isbns == (CODES[0],)
    assert catalogue.volumes[0].isbn13 is None

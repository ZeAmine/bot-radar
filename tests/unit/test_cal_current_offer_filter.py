import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.domain.rules.market_offer import (
    condition_tier,
    is_acceptable_secondhand_condition,
    is_very_good_or_better,
)
from app.providers.base import requested_components
from app.providers.chasse_aux_livres import LOOKUP_POLL_MAX, ChasseAuxLivresProvider
from app.providers.registry import ProviderRegistry
from app.services.book_market_pricing import BookPricingResult
from app.services.market_price_service import _select_secondhand_partners

PILOT = json.loads((
    Path(__file__).parents[1] / 'fixtures/chasse/human_pilot_selection_2026_10_24.json'
).read_text())


def _synthetic_offer(condition, *, merchant='Boutique', total=1500, sale_kind='USED'):
    return {
        'logo': {'name': merchant},
        'condition': {'_name': sale_kind},
        'subCondition': {'name': condition},
        'price': {'amount': total - 300},
        'shippingCost': {'amount': 300},
        'totalPrice': {'amount': total},
        'affiliateUrl': 'https://example.invalid/offer',
    }


async def _collect_synthetic_payload(
    rows, *, isbn='9782344058138', title='1629 Tome 2', engines=1,
):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.startswith('/prix/'):
            return httpx.Response(200, text=(
                f'<div data-asin="{isbn}" data-lvs="synthetic" '
                f'data-nbeng="{engines}"></div>'
            ))
        assert request.url.path == '/rest/lookup/results'
        return httpx.Response(200, json={'offers': {'0': rows}})

    scope = requested_components.set(frozenset({'current'}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            offers = await ChasseAuxLivresProvider(
                http_client=client, max_retries=0, sleep=AsyncMock(),
            ).get_offers(
                isbn13=isbn, title=title,
                source_url=f'https://www.chasse-aux-livres.fr/prix/{isbn}/{isbn}',
            )
    finally:
        requested_components.reset(scope)
    return offers, calls


@pytest.mark.parametrize('case', PILOT['cases'], ids=lambda case: case['isbn'])
async def test_human_capture_selection_with_synthetic_http_envelope(case):
    rows = []
    for source in case['offers']:
        row = _synthetic_offer(
            source['condition'], merchant=(
                'AMAZON_DE' if source['merchant'] == 'amazon.de' else source['merchant']
            ), sale_kind=source['sale_kind'], total=source['total_cents'],
        )
        row['price']['amount'] = source['item_cents']
        row['shippingCost']['amount'] = source['shipping_cents']
        row['affiliateUrl'] = 'https://example.invalid/' + source['id']
        rows.append(row)
    offers, calls = await _collect_synthetic_payload(
        rows, isbn=case['isbn'], title=case['title'],
    )
    assert PILOT['origin'] == 'human_screenshot_transcription'
    assert len(offers) == 1
    selected = offers[0]
    expected = next(r for r in case['offers'] if r['id'] == case['expected_offer_id'])
    assert selected.delivered_price_cents == case['expected_minimum_cents']
    assert selected.item_price_cents == expected['item_cents']
    assert selected.shipping_price_cents == expected['shipping_cents']
    assert selected.source_url.endswith('/' + expected['id'])
    assert selected.raw_payload['best_merchant'].casefold() == expected['merchant'].casefold()
    assert selected.raw_payload['isbn13'] == case['isbn']
    assert selected.item_condition.startswith(expected['condition'])
    assert offers.attempts[0].status == 'success'
    assert len(calls) == 2


@pytest.mark.parametrize('condition', ['Très bon', 'Comme neuf', 'Excellent état', 'Occasion'])
async def test_synthetic_admissible_source_label_is_preserved(condition):
    offers, _ = await _collect_synthetic_payload([_synthetic_offer(condition)])
    assert len(offers) == 1
    assert offers[0].item_condition == f'{condition} (Boutique)'
    assert offers[0].raw_payload['partners']['Boutique']['condition'] == condition


@pytest.mark.parametrize('condition', [None, '', '   '])
async def test_synthetic_empty_source_label_is_not_replaced_with_occasion(condition):
    offers, _ = await _collect_synthetic_payload([_synthetic_offer(condition)])
    assert len(offers) == 1
    assert offers[0].raw_payload['partners']['Boutique']['condition'] == condition
    assert offers[0].item_condition == condition


@pytest.mark.parametrize('condition', ['Bon', 'Neuf', 'État mystère'])
async def test_synthetic_inferior_new_or_unrecognized_label_is_not_reclassified(condition):
    offers, _ = await _collect_synthetic_payload([_synthetic_offer(condition)])
    assert list(offers) == []


@pytest.mark.parametrize('sale_kind', [None, '', 'UNKNOWN', 'NEW'])
@pytest.mark.parametrize('condition', ['Occasion', 'Très bon'])
async def test_synthetic_missing_unknown_or_new_sale_kind_is_not_assumed_used(
    sale_kind, condition,
):
    offers, _ = await _collect_synthetic_payload([
        _synthetic_offer(condition, sale_kind=sale_kind),
    ])
    assert list(offers) == []


@pytest.mark.parametrize('merchant', ['AMAZON_DE', 'Amazon (DE)', 'amazon-de'])
async def test_synthetic_cheaper_amazon_de_stays_excluded(merchant):
    offers, _ = await _collect_synthetic_payload([
        _synthetic_offer('Très bon', merchant=merchant, total=500),
        _synthetic_offer('Occasion', total=1500),
    ])
    assert offers[0].delivered_price_cents == 1500
    assert list(offers[0].raw_payload['partners']) == ['Boutique']


@pytest.mark.parametrize('qualified_first', [False, True])
@pytest.mark.parametrize('same_merchant', [False, True])
async def test_synthetic_equal_totals_prefer_qualified_source_in_both_orders(
    qualified_first, same_merchant,
):
    unknown = _synthetic_offer('Occasion', merchant='Boutique')
    qualified = _synthetic_offer(
        'Très bon', merchant='Boutique' if same_merchant else 'Qualifiée',
    )
    offers, _ = await _collect_synthetic_payload(
        [qualified, unknown] if qualified_first else [unknown, qualified],
    )
    assert offers[0].item_condition.startswith('Très bon')
    partners = offers[0].raw_payload['partners']
    if not same_merchant:
        assert partners['Boutique']['condition'] == 'Occasion'
        assert partners['Qualifiée']['condition'] == 'Très bon'


async def test_synthetic_cheaper_unknown_state_wins_over_costlier_qualified_state():
    offers, _ = await _collect_synthetic_payload([
        _synthetic_offer('Très bon', merchant='Qualifiée', total=1800),
        _synthetic_offer('Occasion', total=1500),
    ])
    assert offers[0].delivered_price_cents == 1500
    assert offers[0].item_condition == 'Occasion (Boutique)'
    assert len(offers[0].raw_payload['partners']) == 2


@pytest.mark.parametrize('shipping', [None, 0])
async def test_synthetic_unknown_shipping_is_not_changed_to_free(shipping):
    row = _synthetic_offer('Occasion')
    row['shippingCost'] = {'amount': shipping}
    row['price']['amount'] = 1500 if shipping == 0 else 1200
    offers, _ = await _collect_synthetic_payload([row])
    assert offers[0].shipping_price_cents == shipping
    assert offers[0].delivered_price_cents == 1500


async def test_synthetic_partial_current_collection_remains_uncertified():
    offers, calls = await _collect_synthetic_payload(
        [_synthetic_offer('Occasion')], engines=2,
    )
    assert offers[0].item_condition == 'Occasion (Boutique)'
    assert offers.attempts[0].status == 'partial'
    assert offers.attempts[0].reason == 'incomplete_offers'
    assert len(calls) == 2 + LOOKUP_POLL_MAX
    result = BookPricingResult.from_offers(
        offers, ProviderRegistry.select_best_offer(offers), None,
    )
    assert result.lowest_delivered_price_cents is None
    assert result.price_confirmed is False
    assert result.current_collection_usable is False


@pytest.mark.parametrize('label', ['Excellent état', 'excellent etat'])
def test_synthetic_shared_rule_accepts_explicit_excellent_state(label):
    assert condition_tier(label) == 'very_good'
    assert is_acceptable_secondhand_condition(label)
    assert is_very_good_or_better(label)


def test_synthetic_current_partner_comparison_keeps_unknown_state_admissible():
    partners = {
        'Boutique': {'is_used': True, 'condition': 'Occasion', 'total_cents': 1500},
        'Qualifiée': {'is_used': True, 'condition': 'Très bon', 'total_cents': 1500},
        'Neuf': {'is_used': False, 'condition': 'Très bon', 'total_cents': 500},
    }
    assert _select_secondhand_partners(partners) == {
        'Boutique': partners['Boutique'], 'Qualifiée': partners['Qualifiée'],
    }

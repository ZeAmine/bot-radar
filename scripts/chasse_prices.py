import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def inspect_book(args: argparse.Namespace) -> dict[str, Any]:
    from app.core.http_client import close_shared_http_client, get_shared_http_client
    from app.core.redis import close_redis
    from app.db.repositories.book_repo import BookRepository
    from app.db.session import AsyncSessionLocal, engine
    from app.providers.cal_governor import (
        CalCollectionBudget,
        CalSuspendedError,
        GovernedClient,
        cal_governor,
    )
    from app.providers.chasse_aux_livres import (
        CHASSE_BROWSER_HEADERS,
        _build_ajax_headers,
        parse_historical_observations,
    )
    from app.providers.chasse_session import chasse_session, valid_history
    from app.services.market_price_service import MarketPriceService

    try:
        async with AsyncSessionLocal() as session:
            book = await BookRepository(session).get_by_isbn13(args.isbn)
            if book is None:
                raise ValueError('Livre absent du cache')
            if args.mode == 'render':
                return {'text': MarketPriceService.format_market_details_card(None, book),
                        'telegram_sent': False, 'collection_requested': False}
            source = book.market_cache.source_history if book.market_cache else None
            url = source.get('source_url') if isinstance(source, dict) else None
            await session.rollback()
        if not isinstance(url, str) or not url.startswith('https://www.chasse-aux-livres.fr/prix/'):
            raise ValueError('URL source certifiée requise après collecte')
        try:
            probe_token = await cal_governor.admit_book(
                "script-chasse-prices", counts_as_book=False,
            )
        except CalSuspendedError as exc:
            raise ValueError(f'CAL reporté par le régulateur : {exc.reason}') from exc
        budget = CalCollectionBudget(cal_governor)
        budget.probe_token = probe_token
        client = GovernedClient(get_shared_http_client(), budget)
        page = await client.get(url, headers=CHASSE_BROWSER_HEADERS,
                                follow_redirects=False, timeout=4)
        if page.status_code != 200:
            raise ValueError('Fiche source inaccessible')
        attrs = {name: re.search(r'data-' + name + r'=["\x27]([^"\x27]+)', page.text)
                 for name in ('asin', 'didh')}
        if not all(attrs.values()):
            raise ValueError('Métadonnées source absentes')
        asin, didh = attrs['asin'], attrs['didh']
        assert asin is not None
        assert didh is not None
        response = await chasse_session.get_history(
            client, url, {'id': asin[1], 'didh': didh[1], 'all': 'true', 'days': '365'},
            _build_ajax_headers(url),
        )
        verified = valid_history(response)
        await cal_governor.conclude_probe(probe_token, succeeded=verified)
        from app.core.redis import get_redis_client
        from app.providers.chasse_session import SESSION_KEY

        persisted = await asyncio.wait_for(get_redis_client().get(SESSION_KEY), timeout=2)
        stored = json.loads(persisted) if persisted else {}
        redis_verified = (stored.get("token") == await chasse_session.token()
                          and stored.get("seed") == chasse_session.state.get("seed"))
        points = parse_historical_observations(response.json(), url) if verified else []
        prices = [point.delivered_price_cents for point in points
                  if point.item_condition == 'Très bon état +']
        return {'authenticated_history_verified': verified,
                'minimum_cents': min(prices, default=None),
                'login_attempted': chasse_session.login_attempted,
                'redis_persistence_verified': redis_verified,
                'prices_modified': False, 'session_store': 'configured_redis'}
    finally:
        await close_shared_http_client()
        await close_redis()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['render', 'probe'])
    parser.add_argument('--isbn', required=True)
    parser.add_argument('--authorize-source-access', action='store_true')
    args = parser.parse_args()
    if args.mode == 'probe' and not args.authorize_source_access:
        parser.error('La sonde nécessite --authorize-source-access')
    try:
        result = asyncio.run(inspect_book(args))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if (result.get('authenticated_history_verified', True)
                     and result.get('redis_persistence_verified', True)) else 1
    except Exception as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

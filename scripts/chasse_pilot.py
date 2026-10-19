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
    from app.core import pilot_mode
    from app.core.http_client import close_shared_http_client, get_shared_http_client
    from app.core.redis import close_redis
    from app.db.repositories.book_repo import BookRepository
    from app.db.session import AsyncSessionLocal, engine
    from app.providers.chasse_aux_livres import (
        CHASSE_BROWSER_HEADERS,
        _build_ajax_headers,
        parse_historical_observations,
    )
    from app.providers.chasse_session import chasse_session, valid_history
    from app.services.market_price_service import MarketPriceService

    try:
        if args.mode == "render":
            if args.isbn not in pilot_mode.read_state()["manifest"]["collection_isbn13"]:
                raise pilot_mode.PilotBlockedError("Édition hors périmètre")
        else:
            pilot_mode.require_isbn(args.isbn)
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
        with pilot_mode.session_probe_scope(args.isbn):
            client = get_shared_http_client()
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
            from app.core.redis import get_redis_client
            from app.providers.chasse_session import SESSION_KEY

            persisted = await asyncio.wait_for(get_redis_client().get(SESSION_KEY), timeout=2)
            stored = json.loads(persisted) if persisted else {}
            redis_verified = (stored.get("token") == await chasse_session.token()
                              and stored.get("seed") == chasse_session.state.get("seed"))
            if not verified or not redis_verified:
                pilot_mode.halt("session_access_or_persistence_unverified")
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


async def resume_pilot(args: argparse.Namespace) -> None:
    from sqlalchemy import text

    from app.core import pilot_mode
    from app.db.repositories.book_repo import BookRepository
    from app.db.session import AsyncSessionLocal, engine
    from app.services.market_refresh_policy import aware

    try:
        state = pilot_mode.read_state()
        if (not args.authorize_pilot_retry or args.approved_digest != state["digest"]
                or args.isbn not in state["manifest"]["collection_isbn13"]):
            raise pilot_mode.PilotBlockedError("Reprise pilote non autorisée")
        async with AsyncSessionLocal() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                await session.execute(text("SET TRANSACTION READ ONLY"))
            book = await BookRepository(session).get_by_isbn13(args.isbn)
            deadline = book.market_cache.next_refresh_at if book and book.market_cache else None
            await session.rollback()
        if deadline is None:
            raise pilot_mode.PilotBlockedError("Échéance réelle de reprise absente")
        pilot_mode.resume(args.state, args.approved_digest, isbn=args.isbn,
                          authorize=args.authorize_pilot_retry, not_before=aware(deadline))
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['enable', 'status', 'release', 'resume',
                                         'probe', 'render'])
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--approved-digest')
    parser.add_argument('--authorize-normal-operation', action='store_true')
    parser.add_argument('--authorize-pilot-retry', action='store_true')
    parser.add_argument('--isbn')
    args = parser.parse_args()
    from app.core import pilot_mode
    from app.core.config import settings

    settings.CHASSE_PILOT_STATE_PATH = str(args.state.resolve())
    try:
        if args.mode == 'enable':
            if not args.manifest or not args.approved_digest:
                parser.error('Manifeste et empreinte explicites requis')
            pilot_mode.enable(args.state, json.loads(args.manifest.read_text()),
                              args.approved_digest)
        elif args.mode == 'resume':
            if not args.isbn or not args.approved_digest or not args.authorize_pilot_retry:
                parser.error('ISBN, empreinte et autorisation de reprise explicites requis')
            asyncio.run(resume_pilot(args))
        elif args.mode == 'release':
            pilot_mode.release(args.state, args.approved_digest,
                               authorize=args.authorize_normal_operation)
        elif args.mode in {'probe', 'render'}:
            if not args.isbn:
                parser.error('ISBN requis')
            result = asyncio.run(inspect_book(args))
            print(json.dumps(result, ensure_ascii=False))
            return 0 if (result.get('authenticated_history_verified', True)
                         and result.get('redis_persistence_verified', True)) else 1
        state = pilot_mode.read_state(args.state)
        print(json.dumps({'active': state['mode'] == 'pilot', 'halted': state['halted'],
                          'attempts': state['attempts'], 'digest': state['digest'],
                          'automatic_jobs': 'suspended' if state['mode'] == 'pilot' else 'enabled',
                          'telegram': 'blocked' if state['mode'] == 'pilot' else 'enabled'}))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

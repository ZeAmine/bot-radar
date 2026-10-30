"""Relance la cote d'un livre nommé, par le chemin de collecte partagé.

Ce script existe pour un livre nommé dont l'état de rafraîchissement est faux —
daté par une passe qui ne l'a jamais interrogé — et qu'aucun geste de
l'application ne sait plus relancer avant l'échéance. Il n'écrit aucune valeur à
la main : il appelle `price_book`, la collecte partagée qui décide, persiste et
replanifie (mêmes décisions, mêmes traces que le bouton « Prix du marché »).

La garde locale de fraîcheur, elle, est écartée : le cache du livre n'est pas
chargé, donc la collecte demande les deux composantes (« cote » et
« historique »), comme à la création d'une alerte — un livre déjà coté n'est
donc pas réinterrogé par ce script. C'est la seule entorse, et elle est nommée :
elle ne touche pas au régulateur, qui reste seul juge du droit d'interroger la
source.

Trois limites sont celles de la source, pas de ce script : deux livres par
fenêtre de quinze minutes, douze requêtes par collecte, une collecte à la fois et
deux secondes entre deux requêtes. C'est le régulateur CAL qui les applique ; un
refus est rapporté tel quel (`deferred`), jamais contourné. Un défi anti-robot
arrête la série : le livre est rapporté, pas réessayé.

    python scripts/refresh_book_prices.py --isbn 9782857085737 --isbn 9782080270290

Sortie : une ligne JSON par livre (`RELANCE {...}`), puis un récapitulatif. Code
de sortie 1 si un livre n'a pas rendu d'état de cote exploitable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Lancé depuis le dépôt, ou lu sur l'entrée standard dans le conteneur : dans les
# deux cas, la racine importable est celle qui porte `app/`.
_ICI = Path(__file__).resolve().parent
ROOT = _ICI.parent if (_ICI.parent / "app").is_dir() else _ICI
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# États de cote qui répondent à « que vaut ce livre aujourd'hui » : mesuré, vide
# ou écarté. Un refus de la source n'en est pas un — il se dit refus.
ETATS_RENDUS = frozenset({"cote_trouvee", "aucune_offre", "offres_ecartees_par_les_filtres"})


async def _relance(isbn13: str) -> dict[str, Any]:
    from app.db.repositories.book_repo import BookRepository
    from app.db.session import AsyncSessionLocal
    from app.domain.rules.cote_outcome import (
        ETATS_COTE_ACCEPTES,
        select_cote_offer,
        select_historical_low,
    )
    from app.providers.base import MarketOffers
    from app.services.book_market_pricing import (
        BookPricingResult,
        cote_offres_from_market,
        history_points_from_market,
        price_book,
    )
    from app.utils.isbn import is_valid_isbn13, normalize_isbn

    result: dict[str, Any] = {"isbn": isbn13}
    code = normalize_isbn(isbn13)
    if code is None or code != isbn13 or not is_valid_isbn13(code):
        return {**result, "relance": "refus", "raison": "isbn_invalide"}
    results: list[BookPricingResult] = []
    async with AsyncSessionLocal() as session:
        book = await BookRepository(session).get_by_isbn13(code)
        if book is None:
            return {**result, "relance": "refus", "raison": "livre_inconnu"}
        # La garde de fraîcheur se lit sur le cache chargé : `fetch_offers_for_book`
        # ne demande que les composantes échues. Sans cache, la collecte demande
        # « cote » et « historique », exactement comme à la création d'une alerte.
        # C'est le seul contournement de ce script, et il est nommé ici : la garde
        # locale cède, le régulateur CAL garde ses plafonds (deux livres par
        # fenêtre, douze requêtes, une collecte à la fois).
        book.__dict__.pop("market_cache", None)
        result.update(book_id=str(book.id), titre=book.title)
        cache = await price_book(session, book, result_sink=results)
        await session.commit()
    result["statut_collecte"] = cache.collection_status
    result.update(
        cache_cote_cents=cache.lowest_delivered_price_cents,
        cache_cote_marchand=cache.best_provider_name,
        cache_cote_url=cache.best_source_url,
        cache_bas_1an_cents=cache.annual_low_cents,
        cache_dernier_essai=cache.last_attempt_at.isoformat() if cache.last_attempt_at else None,
        cache_derniere_reussite=(
            cache.last_successful_collection_at.isoformat()
            if cache.last_successful_collection_at
            else None
        ),
        cache_prochaine_echeance=(
            cache.next_refresh_at.isoformat() if cache.next_refresh_at else None
        ),
        cache_historique_source=bool(cache.source_history),
    )
    # La collecte rendue, recomposée avec ses tentatives : c'est ce que la décision
    # de cote lit pour distinguer « rien à vendre » de « source muette ».
    resultat = results[-1] if results else None
    offers = (
        MarketOffers(resultat.offers, attempts=resultat.attempts,
                     failed_providers=resultat.failed_providers,
                     historical_checked=resultat.historical_checked,
                     emitted_requests=resultat.emitted_requests)
        if resultat is not None
        else None
    )
    offres = None if offers is None else cote_offres_from_market(offers)
    cote = select_cote_offer(offres, ETATS_COTE_ACCEPTES)
    releves = None if offers is None else history_points_from_market(offers)
    bas = select_historical_low(releves)
    result.update(
        etat_cote=cote.state,
        cote_cents=cote.offre.total_cents if cote.offre else None,
        cote_marchand=cote.offre.marchand if cote.offre else None,
        cote_etat_article=cote.offre.etat if cote.offre else None,
        cote_url=cote.offre.url if cote.offre else None,
        offres_examinees=cote.offres_examinees,
        moins_chere_ecartee_cents=cote.moins_chere_ecartee_cents,
        etat_bas_1an=bas.state,
        bas_1an_cents=bas.cents,
        bas_1an_etat=bas.etat,
        tentatives=[
            {"provider": attempt.provider, "operation": attempt.operation,
             "statut": attempt.status, "raison": attempt.reason,
             "http": attempt.http_status}
            for attempt in (getattr(offers, "attempts", ()) or ())
        ],
        requetes_emises=getattr(offers, "emitted_requests", None),
        historique_lu=getattr(offers, "historical_checked", None),
    )
    result["relance"] = "collecte" if cote.state in ETATS_RENDUS else "refus_source"
    return result


async def _run(isbns: list[str], max_books: int) -> int:
    from app.core.http_client import close_shared_http_client
    from app.core.redis import close_redis
    from app.db.session import engine

    lignes: list[dict[str, Any]] = []
    try:
        for isbn in isbns[:max_books]:
            try:
                ligne = await _relance(isbn)
            except Exception as exc:  # une relance ratée se dit, elle ne masque pas les autres
                ligne = {"isbn": isbn, "relance": "refus", "erreur": type(exc).__name__}
            print("RELANCE " + json.dumps(ligne, ensure_ascii=False), flush=True)
            lignes.append(ligne)
    finally:
        await close_shared_http_client()
        await close_redis()
        await engine.dispose()
    rendus = [ligne for ligne in lignes if ligne.get("relance") == "collecte"]
    print("RECAP " + json.dumps(
        {"demandes": len(isbns[:max_books]), "rendus": len(rendus),
         "sans_etat": [ligne["isbn"] for ligne in lignes
                       if ligne.get("relance") != "collecte"]},
        ensure_ascii=False,
    ), flush=True)
    return 0 if len(rendus) == len(lignes) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--isbn", action="append", required=True,
                        help="ISBN-13 du livre à relancer (répétable, traité dans l'ordre)")
    parser.add_argument("--max-books", type=int, default=2,
                        help="livres collectés au plus dans cette invocation (défaut : 2, "
                             "plafond de la source par fenêtre de quinze minutes)")
    args = parser.parse_args()
    return asyncio.run(_run(args.isbn, args.max_books))


if __name__ == "__main__":
    raise SystemExit(main())

"""Fournisseur de prix d'occasion agrégés Chasse-aux-livres."""

import asyncio
import html
import logging
import re
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.config import settings
from app.core.constants import MatchConfidence
from app.core.http_client import get_shared_http_client
from app.core.redis import get_redis_client
from app.domain.rules.market_offer import (
    is_brand_new_partner,
    is_merchant_excluded,
    is_very_good_or_better,
)
from app.providers.base import MarketOffer, MarketOffers, normalize_search_isbn
from app.services.book_metadata_service import title_similarity
from app.services.market_price_service import extract_tome_number_from_title
from app.utils.text import normalize_text

logger = logging.getLogger("radar.providers.chasse_aux_livres")

# Cle Redis du jeton anti-bot injecte par `scripts/sync_chasse_session.py`.
REDIS_SESSION_KEY = "radar:chasse:duih"

# Attente entre deux tentatives. Injectable pour que les tests qui exercent le
# backoff ne paient pas les délais en temps réel : en production c'est
# `asyncio.sleep`, jamais autre chose.
Sleeper = Callable[[float], Awaitable[None]]

# Similarité minimale entre le titre attendu et celui renvoyé par l'agrégateur.
TITLE_COHERENCE_MIN = 0.72

# Vocabulaire générique d'édition, sans valeur discriminante pour identifier une œuvre.
_TITLE_STOP_WORDS = frozenset(
    {
        "tome",
        "tomes",
        "volume",
        "integrale",
        "edition",
        "livre",
        "bd",
        "des",
        "les",
        "une",
        "par",
        "pour",
        "dans",
        "avec",
        "sur",
    }
)

_HISTORICAL_LOW_LABELS = (
    "Très bon état +",
    "Comme neuf +",
)


async def resolve_session_token() -> str:
    """Jeton anti-bot de session, sans jamais le loguer.

    L'environnement passe devant : c'est le réglage d'une installation qui veut
    figer sa valeur. Sinon le jeton vient de Redis, injecté par
    `scripts/sync_chasse_session.py` depuis un navigateur — le site le délivre à une
    session qui a passé son défi JavaScript, et une requête HTTP seule ne l'obtient
    pas. Un Redis muet ramène à une série 1 an absente, jamais à une exception.
    """
    configured = (settings.CHASSE_AUX_LIVRES_DUIH or "").strip()
    if configured:
        return configured
    try:
        stored = await get_redis_client().get(REDIS_SESSION_KEY)
    except Exception as exc:  # pragma: no cover - Redis indisponible
        logger.debug("Jeton de session Chasse-aux-livres illisible : %s", exc)
        return ""
    if isinstance(stored, bytes):  # pragma: no cover - client sans decode_responses
        stored = stored.decode("utf-8", errors="ignore")
    return (stored or "").strip()


def select_historical_low_cents(series_by_label: dict[str, float]) -> int | None:
    """Choisit le plus bas 1 an au même palier que les offres live.

    Seuls « Très bon état + » et « Comme neuf + » comptent. Un plus bas
    « Bon état + » ou « D'occasion » est ignoré.

    Tous les paliers retenus sont des occasions comparables — « Comme neuf »
    vaut mieux que « Très bon » — : le plus bas constaté est donc le minimum de
    ces séries, jamais la première d'entre elles : s'arrêter à la première
    affichait une « cote » au-dessus d'un prix qu'un meilleur état avait déjà
    atteint.
    """
    cents = [
        round(series_by_label[label] * 100)
        for label in _HISTORICAL_LOW_LABELS
        if label in series_by_label
    ]
    return min(cents) if cents else None


CHASSE_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Google Chrome";v="128", "Chromium";v="128", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _build_ajax_headers(referer: str) -> dict[str, str]:
    return {
        **CHASSE_BROWSER_HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


class ChasseAuxLivresProvider:
    """Connecteur vers le comparateur multi-boutiques Chasse-aux-livres.

    Agrège en temps réel les offres de Momox, RecycLivre, Rakuten, eBay,
    Fnac 2nde main, AbeBooks, Gibert, etc., et extrait l'offre au meilleur prix livré.
    """

    name: str = "chasse_aux_livres"
    BASE_URL: str = "https://www.chasse-aux-livres.fr"

    def __init__(
        self,
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client
        self._sleep = sleep

    @staticmethod
    def _significant_words(title: str) -> set[str]:
        """Mots porteurs du titre, hors vocabulaire générique d'édition."""
        return {
            word
            for word in normalize_text(title).split()
            if len(word) >= 3 and word not in _TITLE_STOP_WORDS
        }

    @staticmethod
    def is_title_coherent(expected: str | None, found: str | None) -> bool:
        """Vérifie que l'ouvrage renvoyé par Chasse-aux-livres est bien l'œuvre attendue.

        Trois garde-fous : refus immédiat si les deux titres désignent deux tomes
        différents, puis similarité du titre, puis identité du noyau d'œuvre (un
        sous-titre peut manquer d'un côté). Un titre sans mot significatif
        exploitable est rejeté : mieux vaut une cote absente qu'une cote attribuée
        à un autre ouvrage.
        """
        if not expected or not found:
            return True

        # Un tome différent est un refus immédiat : la similarité des titres de la
        # même série est trop élevée pour distinguer deux volumes.
        expected_tome = extract_tome_number_from_title(expected)
        found_tome = extract_tome_number_from_title(found)
        if expected_tome is not None and found_tome is not None and expected_tome != found_tome:
            return False

        if title_similarity(expected, found) >= TITLE_COHERENCE_MIN:
            return True

        exp_words = ChasseAuxLivresProvider._significant_words(expected)
        fnd_words = ChasseAuxLivresProvider._significant_words(found)
        if not exp_words or not fnd_words:
            return False

        return exp_words <= fnd_words or fnd_words <= exp_words

    async def get_offers(
        self,
        *,
        isbn13: str | None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
    ) -> list[MarketOffer]:
        """Interroge Chasse-aux-livres par ISBN-13 ou par Titre canonique avec repli automatique."""
        _ = edition
        # 1. Tentative par ISBN : uniquement si le code passe sa clé de contrôle,
        # sinon la recherche renverrait l'ouvrage d'un autre livre.
        clean_isbn = normalize_search_isbn(isbn13)
        failures: set[str] = set()
        if clean_isbn:
            offers = await self._search_query(
                clean_isbn, expected_title=title, expected_isbn=clean_isbn
            )
            failures.update(getattr(offers, "failed_providers", ()))
            if offers:
                return offers

        # 2. Repli par Titre (+ auteur éventuel) : même si la requête part du
        # titre, l'ISBN attendu accompagne la recherche — un résultat qui
        # affiche ce code prouve l'identité quoi que dise la graphie (ADR-035).
        if title and len(title.strip()) > 0:
            t_query = title.strip()
            if author:
                t_query = f"{t_query} {author.strip()}"
            offers = await self._search_query(
                t_query, expected_title=title, expected_isbn=clean_isbn
            )
            if offers:
                return offers
            failures.update(getattr(offers, "failed_providers", ()))
            return MarketOffers(offers, failed_providers=tuple(sorted(failures)))

        return MarketOffers(failed_providers=tuple(sorted(failures)))

    async def _search_query(
        self,
        query: str,
        expected_title: str | None = None,
        expected_isbn: str | None = None,
    ) -> list[MarketOffer]:
        """Exécute une recherche sur Chasse-aux-livres pour une requête donnée."""
        search_encoded = urllib.parse.quote_plus(query)
        search_url = f"{self.BASE_URL}/search?query={search_encoded}&catalog=fr"

        client = self.http_client or get_shared_http_client()
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                # Étape 1 : Récupérer la page de recherche avec ses cookies et son token hash
                resp1 = await client.get(
                    search_url,
                    headers=CHASSE_BROWSER_HEADERS,
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                )
                if resp1.status_code == 429 or "Vérification robot" in resp1.text:
                    if attempt < max_retries:
                        wait_time = 2.0 * (attempt + 1)
                        logger.warning(
                            "Chasse-aux-livres anti-bot/rate-limit pour '%s' "
                            "(tentative %d/%d). Pause %.1fs...",
                            query,
                            attempt + 1,
                            max_retries + 1,
                            wait_time,
                        )
                        await self._sleep(wait_time)
                        continue
                    logger.warning("Chasse-aux-livres rate-limit persistant pour '%s'.", query)
                    return MarketOffers(failed_providers=(self.name,))

                if resp1.status_code != 200:
                    logger.debug(
                        "Chasse-aux-livres returned HTTP %d on search page", resp1.status_code
                    )
                    return MarketOffers(failed_providers=(self.name,))

                html_page = resp1.text
                hash_match = re.search(r'data-hash=["\']([a-zA-Z0-9_-]{16,128})["\']', html_page)
                if not hash_match:
                    logger.debug("No search data-hash found in Chasse-aux-livres page")
                    return []

                data_hash = hash_match.group(1)

                # Étape 2 : Récupérer les résultats agrégés via l'endpoint REST
                ajax_headers = _build_ajax_headers(search_url)
                rest_url = f"{self.BASE_URL}/rest/search-results?h={data_hash}&p=1&l=1&b=d"
                resp2 = await client.get(
                    rest_url, headers=ajax_headers, timeout=self.timeout_seconds
                )
                if resp2.status_code != 200:
                    logger.debug(
                        "Chasse-aux-livres returned HTTP %d on search-results", resp2.status_code
                    )
                    return MarketOffers(failed_providers=(self.name,))

                data = resp2.json()
                d_html = data.get("d", "")
                if not d_html:
                    return []

                # Récupération fine et vérifiée des offres partenaires d'occasion
                deep_offers = await self._try_deep_lookup(
                    client,
                    d_html,
                    query,
                    expected_title=expected_title,
                    expected_isbn=expected_isbn,
                )
                if deep_offers is not None:
                    return deep_offers

                return []

            except Exception as exc:
                if attempt < max_retries:
                    await self._sleep(1.5)
                    continue
                logger.debug("Erreur silencieuse connecteur Chasse-aux-livres: %s", exc)
                return MarketOffers(failed_providers=(self.name,))

        return []

    async def _try_deep_lookup(
        self,
        client: httpx.AsyncClient,
        d_html: str,
        query: str,
        expected_title: str | None = None,
        expected_isbn: str | None = None,
    ) -> list[MarketOffer] | None:
        """Tente une extraction détaillée des offres par boutique
        (eBay, Momox, Rakuten, Fnac, etc.)."""
        try:
            url_match = re.search(r'href=["\'](/prix/[^"\']+)["\']', d_html)
            if not url_match:
                return None
            product_path = url_match.group(1)
            product_url = f"{self.BASE_URL}{product_path}"

            # Extraction ISBN et Titre depuis d_html
            isbn_match = re.search(r"ISBN&nbsp;:\s*(\d{10,13})", d_html)
            found_isbn = normalize_search_isbn(isbn_match.group(1)) if isbn_match else None
            title_match = re.search(
                r'class=["\']title["\'][^>]*>\s*<a[^>]*>([^<]+)</a>', d_html, re.DOTALL
            )
            if not title_match:
                title_match = re.search(
                    r'<a[^>]+href=["\']/prix/[^"\']+["\'][^>]+title=["\']([^"\']+)["\']', d_html
                )

            found_title = html.unescape(title_match.group(1).strip()) if title_match else None
            slug_m = re.search(r"/prix/[^/]+/([^/]+)", product_path)
            slug_title = slug_m.group(1).replace("-", " ") if slug_m else ""

            if not found_title or found_title.isdigit():
                found_title = slug_title or query

            # Contrôle strict de cohérence du titre (rejet si hors sujet, ex: Ogrest pour Loba Loca)
            # On vérifie contre le titre affiché et contre le slug URL qui
            # contient les sous-titres et collections complètes
            #
            # L'ISBN exact prime sur le titre (ADR-035) : quand le résultat
            # affiche le code même que celui cherché, l'identité est prouvée et
            # la graphie ne vétote plus — le site orthographie les volumes de
            # façons très diverses (« 1629, ou l'effrayante histoire… » pour
            # « Les naufragés du Jakarta T1 »), et un refus ici prive la série
            # de toute cote alors que le bon livre est sous nos yeux.
            isbn_proven = bool(expected_isbn) and found_isbn == expected_isbn

            title_coherent = self.is_title_coherent(expected_title, found_title) or (
                bool(slug_title) and self.is_title_coherent(expected_title, slug_title)
            )
            is_coherent = isbn_proven or title_coherent
            if expected_title and not is_coherent:
                logger.warning(
                    "Rejet Chasse-aux-livres pour '%s' : le titre retourné '%s' "
                    "(slug: '%s', ISBN: %s) est incohérent.",
                    expected_title,
                    found_title,
                    slug_title,
                    found_isbn,
                )
                return None
            if isbn_proven and expected_title and not title_coherent:
                logger.debug(
                    "Chasse-aux-livres : ISBN %s confirmé malgré des titres divergents "
                    "('%s' vs '%s') — l'ISBN fait foi.",
                    found_isbn,
                    expected_title,
                    found_title,
                )

            resp_prod = await client.get(product_url, headers=CHASSE_BROWSER_HEADERS)
            if resp_prod.status_code != 200:
                return MarketOffers(failed_providers=(self.name,))

            p_html = resp_prod.text
            asin_m = re.search(r'data-asin="([^"]+)"', p_html)
            lvs_m = re.search(r'data-lvs="([^"]+)"', p_html)
            nbeng_m = re.search(r'data-nbeng="(\d+)"', p_html)
            if not (asin_m and lvs_m and nbeng_m):
                return None

            asin = asin_m.group(1)
            lvs = lvs_m.group(1)
            nbeng = int(nbeng_m.group(1))
            engines = "_".join(str(i) for i in range(min(nbeng, 32)))

            ajax_headers = _build_ajax_headers(product_url)
            lookup_url = (
                f"{self.BASE_URL}/rest/lookup/results?calls=offers&itemId={asin}"
                f"&retry=0&duih=&lvs={lvs}&ui=&engines={engines}&f=false"
            )
            resp_lookup = await client.get(lookup_url, headers=ajax_headers)
            if resp_lookup.status_code != 200:
                return MarketOffers(failed_providers=(self.name,))

            data = resp_lookup.json()
            raw_offers = data.get("offers", {})
            if not isinstance(raw_offers, dict):
                return None

            # Si le moteur eBay (22) n'a pas encore répondu au premier appel, interroger retry=1
            if "22" not in raw_offers:
                retry_url = (
                    f"{self.BASE_URL}/rest/lookup/results?calls=offers&itemId={asin}"
                    f"&retry=1&duih=&lvs={lvs}&ui=&engines={engines}&f=false"
                )
                try:
                    resp_retry = await client.get(retry_url, headers=ajax_headers, timeout=3.5)
                    if resp_retry.status_code == 200:
                        data_retry = resp_retry.json()
                        offers_retry = data_retry.get("offers", {})
                        if isinstance(offers_retry, dict):
                            for k, v in offers_retry.items():
                                if k not in raw_offers:
                                    raw_offers[k] = v
                                elif isinstance(v, list) and isinstance(raw_offers[k], list):
                                    raw_offers[k].extend(v)
                except Exception as exc:
                    logger.debug("Second passage lookup retry=1 ignoré : %s", exc)

            partners: dict[str, dict[str, Any]] = {}
            best_used: dict[str, Any] | None = None

            for _, off_list in raw_offers.items():
                if not isinstance(off_list, list):
                    continue
                for off in off_list:
                    merchant_raw = (
                        off.get("logo", {}).get("name")
                        or off.get("origin", {}).get("name")
                        or "Partenaire"
                    )
                    merchant_clean = (
                        merchant_raw.replace("_SHOP", "")
                        .replace("_MARKET", "")
                        .replace("_DE", " (DE)")
                        .replace("_", " ")
                        .title()
                    )

                    # Exclusion stricte des marchands étrangers (ex: Amazon DE)
                    if is_merchant_excluded(merchant_raw) or is_merchant_excluded(merchant_clean):
                        continue

                    is_used = off.get("condition", {}).get("_name") != "NEW"
                    sub_cond = off.get("subCondition", {}).get("name") or (
                        "Occasion" if is_used else "Neuf"
                    )
                    tot = off.get("totalPrice", {}).get("amount", 0)
                    aff_url = off.get("affiliateUrl") or product_url

                    if tot <= 0:
                        continue

                    # Filtrage STRICT occasion : exclusion absolue du Neuf
                    if is_brand_new_partner({"is_used": is_used, "condition": sub_cond}):
                        continue

                    # Filtrage STRICT état : uniquement Très bon et Comme neuf
                    # (rejet Bon, Correct, etc.)
                    if not is_very_good_or_better(sub_cond):
                        continue

                    entry = {
                        "merchant": merchant_clean,
                        "is_used": True,
                        "condition": sub_cond,
                        "total_cents": tot,
                        "url": aff_url,
                    }

                    if (
                        merchant_clean not in partners
                        or partners[merchant_clean]["total_cents"] > tot
                    ):
                        partners[merchant_clean] = entry

                    if best_used is None or tot < best_used["total_cents"]:
                        best_used = entry

            # Extraction du minimum historique sur 1 an via l'API /rest/jcd de Chasse-aux-livres
            hist_low_1y_cents: int | None = None
            historical_failed = False
            didh_m = re.search(r'data-didh=["\']([a-f0-9]+)["\']', p_html)
            if didh_m:
                didh = didh_m.group(1)
                duih_val = await resolve_session_token()
                ui_val = duih_val.split(".")[0] if "." in duih_val else duih_val
                jcd_url = (
                    f"{self.BASE_URL}/rest/jcd?id={asin}&all=true&days=365"
                    f"&ui={ui_val}&duih={duih_val}&didh={didh}"
                )
                try:
                    resp_jcd = await client.get(jcd_url, headers=ajax_headers, timeout=4.0)
                    if resp_jcd.status_code != 200:
                        historical_failed = True
                    if resp_jcd.status_code == 200 and len(resp_jcd.text) > 0:
                        jcd_data = resp_jcd.json()
                        series_by_label: dict[str, float] = {}
                        for s in jcd_data.get("stats", []):
                            label = s.get("label")
                            if label:
                                pts: list[float] = []
                                for v in s.get("statValues", []):
                                    try:
                                        t_str = str(v.get("t", ""))
                                        t_m = re.search(r"#\[([0-9]+)[,\.]([0-9]{2})\s*€", t_str)
                                        if t_m:
                                            eur = float(f"{t_m.group(1)}.{t_m.group(2)}")
                                        else:
                                            eur = float(v.get("y", 0))
                                        if eur > 0:
                                            pts.append(eur)
                                    except Exception:
                                        pass
                                if pts:
                                    series_by_label[label] = min(pts)

                        hist_low_1y_cents = select_historical_low_cents(series_by_label)
                except Exception as jcd_exc:
                    historical_failed = True
                    logger.debug("Historique des prix Chasse-aux-livres indisponible: %s", jcd_exc)

            # Zéro Neuf déguisé : Si aucune occasion n'est en stock,
            # renvoyer l'historique seul ou None
            if not best_used:
                if hist_low_1y_cents:
                    return [
                        MarketOffer(
                            provider_name=self.name,
                            item_price_cents=0,
                            shipping_price_cents=None,
                            delivered_price_cents=0,
                            source_url=product_url,
                            currency="EUR",
                            item_condition="Occasion indisponible",
                            confidence=MatchConfidence.HIGH,
                            is_available=False,
                            is_comparable=True,
                            raw_payload={
                                "isbn13": found_isbn,
                                "title": found_title,
                                "query": query,
                                "best_merchant": None,
                                "partners": {},
                                "historical_low_1y_cents": hist_low_1y_cents,
                            },
                        )
                    ]
                return MarketOffers(failed_providers=(self.name,)) if historical_failed else None

            primary = best_used
            cond_str = f"{primary['condition']} ({primary['merchant']})"

            return MarketOffers(
                [
                    MarketOffer(
                        provider_name=self.name,
                        item_price_cents=primary["total_cents"],
                        shipping_price_cents=None,
                        delivered_price_cents=primary["total_cents"],
                        price_includes_shipping=True,
                        source_url=primary["url"],
                        currency="EUR",
                        item_condition=cond_str,
                        confidence=MatchConfidence.HIGH,
                        is_available=True,
                        is_comparable=True,
                        raw_payload={
                            "isbn13": found_isbn,
                            "title": found_title,
                            "query": query,
                            "best_merchant": primary["merchant"],
                            "partners": partners,
                            "historical_low_1y_cents": hist_low_1y_cents,
                        },
                    )
                ],
                failed_providers=(self.name,) if historical_failed else (),
            )
        except Exception as exc:
            logger.debug("Échec de l'extraction détaillée Chasse-aux-livres: %s", exc)
            return MarketOffers(failed_providers=(self.name,))

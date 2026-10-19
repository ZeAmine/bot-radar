"""Fournisseur de prix d'occasion agrégés Chasse-aux-livres."""

import asyncio
import html
import logging
import re
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core import pilot_mode
from app.core.constants import MatchConfidence
from app.core.http_client import get_shared_http_client
from app.domain.rules.market_offer import (
    is_brand_new_partner,
    is_merchant_excluded,
    is_very_good_or_better,
)
from app.providers.base import (
    CollectionAttempt,
    MarketOffer,
    MarketOffers,
    failed_offers,
    normalize_search_isbn,
    requested_components,
)
from app.providers.chasse_session import chasse_session, source_restriction
from app.services.book_metadata_service import title_similarity
from app.services.market_price_service import extract_tome_number_from_title
from app.utils.isbn import isbn_from_identifier
from app.utils.text import normalize_text

logger = logging.getLogger("radar.providers.chasse_aux_livres")

REDIS_SESSION_KEY = "radar:chasse:duih"

# Attente entre deux tentatives, injectable pour les tests.
Sleeper = Callable[[float], Awaitable[None]]

TITLE_COHERENCE_MIN = 0.72

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
    """Retourne le jeton effectif du gestionnaire de session partagé."""
    return await chasse_session.token()


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


def parse_historical_observations(
    data: dict[str, Any], source_url: str,
) -> list[MarketOffer]:
    """Conserve les points datés des paliers d'occasion livrée admissibles.

    Une valeur sans date reste un agrégat, jamais une observation annuelle.
    Le champ x peut porter un instant Unix en millisecondes ou une date ISO
    avec fuseau. Le total livré doit être explicite : delivered_price_cents ou
    price_includes_shipping. Aucun port manquant n'est assimilé à zéro.
    """
    points: list[MarketOffer] = []
    now = datetime.now(UTC)
    for series in data.get("stats", []):
        if not isinstance(series, dict) or series.get("label") not in _HISTORICAL_LOW_LABELS:
            continue
        label = series["label"]
        for value in series.get("statValues", []):
            if not isinstance(value, dict):
                continue
            delivered = value.get("delivered_price_cents")
            includes_delivery = any(
                owner.get("price_includes_shipping") is True for owner in (data, series, value)
            )
            if delivered is None and not includes_delivery:
                continue
            raw_date = value.get("x")
            try:
                if (isinstance(raw_date, (int, float)) and not isinstance(raw_date, bool)
                        or isinstance(raw_date, str) and raw_date.isdigit()):
                    at = datetime.fromtimestamp(float(raw_date) / 1000, UTC)
                elif isinstance(raw_date, str):
                    at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    if at.tzinfo is None:
                        continue
                    at = at.astimezone(UTC)
                else:
                    continue
                if delivered is not None:
                    if not isinstance(delivered, int) or isinstance(delivered, bool):
                        continue
                    cents = delivered
                else:
                    if isinstance(value.get("y"), bool):
                        continue
                    amount = Decimal(str(value["y"]))
                    cents = round(amount * 100)
            except (KeyError, ValueError, TypeError, OverflowError, OSError, InvalidOperation):
                continue
            if cents <= 0 or not now - timedelta(days=365) < at <= now:
                continue
            identity = f"{source_url}|{label}|{at.isoformat()}|{cents}"
            points.append(MarketOffer(
                provider_name="chasse_aux_livres", item_price_cents=cents,
                shipping_price_cents=None, delivered_price_cents=cents,
                source_url=source_url, item_condition=label, price_includes_shipping=True,
                is_available=False, is_historical=True, observed_at=at,
                observation_id=uuid.uuid5(uuid.NAMESPACE_URL, identity),
                raw_payload={"best_merchant": value.get("merchant")},
            ))
    return points


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


def _response_failure(
    response: httpx.Response, *, stage: str, operation: str = "current",
) -> MarketOffers | None:
    reason = source_restriction(response)
    if reason or response.status_code != 200:
        return failed_offers("chasse_aux_livres", response=response, reason=reason,
                             operation=operation, stage=stage)
    return None


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
        max_retries: int = 2,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client
        self._sleep = sleep
        self.max_retries = max(0, max_retries)

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
        pilot_mode.require_source(isbn13)
        _ = edition
        # ISBN utilisé seulement s'il passe sa clé de contrôle.
        clean_isbn = normalize_search_isbn(isbn13)
        failures: set[str] = set()
        historical_checked = False
        attempts: list[CollectionAttempt] = []
        if clean_isbn:
            offers = await self._search_query(
                clean_isbn, expected_title=title, expected_isbn=clean_isbn
            )
            attempts.extend(getattr(offers, "attempts", ()))
            failures.update(getattr(offers, "failed_providers", ()))
            historical_checked |= getattr(offers, "historical_checked", False)
            if offers or getattr(offers, "historical_checked", False):
                return offers
            if any(attempt.status == "failed" and (
                attempt.operation == "current"
                and attempt.reason not in {"identity_mismatch", "volume_mismatch"}
                or attempt.reason in {"rate_limited", "bot_challenge", "session_unavailable"}
            ) for attempt in getattr(offers, "attempts", ())):
                return offers

        # Repli par titre (+ auteur) ; l'ISBN attendu prouve l'identité (ADR-035).
        if title and len(title.strip()) > 0:
            t_query = title.strip()
            if author:
                t_query = f"{t_query} {author.strip()}"
            offers = await self._search_query(
                t_query, expected_title=title, expected_isbn=clean_isbn
            )
            if offers or getattr(offers, "historical_checked", False):
                return offers
            attempts.extend(getattr(offers, "attempts", ()))
            failures.update(getattr(offers, "failed_providers", ()))
            historical_checked |= getattr(offers, "historical_checked", False)
            return MarketOffers(offers, failed_providers=tuple(sorted(failures)),
                                historical_checked=historical_checked, attempts=tuple(attempts))

        return MarketOffers(failed_providers=tuple(sorted(failures)), attempts=tuple(attempts))

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
        max_retries = self.max_retries
        for attempt in range(max_retries + 1):
            stage = "search_page"
            try:
                resp1 = await client.get(
                    search_url,
                    headers=CHASSE_BROWSER_HEADERS,
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                )
                failure = _response_failure(resp1, stage=stage)
                if failure is not None:
                    return failure

                html_page = resp1.text
                hash_match = re.search(r'data-hash=["\']([a-zA-Z0-9_-]{16,128})["\']', html_page)
                if not hash_match:
                    logger.debug("No search data-hash found in Chasse-aux-livres page")
                    return failed_offers(self.name, response=resp1, reason="invalid_response",
                                         stage="search_metadata")

                data_hash = hash_match.group(1)

                ajax_headers = _build_ajax_headers(search_url)
                rest_url = f"{self.BASE_URL}/rest/search-results?h={data_hash}&p=1&l=1&b=d"
                stage = "search_results"
                resp2 = await client.get(
                    rest_url, headers=ajax_headers, timeout=self.timeout_seconds
                )
                failure = _response_failure(resp2, stage=stage)
                if failure is not None:
                    return failure

                data = resp2.json()
                if not isinstance(data, dict) or not isinstance(data.get("d"), str):
                    raise ValueError("Résultats de recherche invalides")
                d_html = data["d"]
                if not d_html:
                    return []

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
                if attempt < max_retries and isinstance(exc, (httpx.RequestError, TimeoutError)):
                    await self._sleep(1.5)
                    continue
                logger.debug("Erreur connecteur Chasse-aux-livres: %s", type(exc).__name__)
                return failed_offers(self.name, error=exc, stage=stage)

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
        stage = "candidate_identity"
        try:
            candidates = re.findall(r'href=["\'](/prix/[^"\']+)["\']', d_html)
            candidates = list(dict.fromkeys(html.unescape(path) for path in candidates))
            selected_path = None
            selected_isbn = None
            for path in candidates:
                path_codes = re.findall(r"(?<!\d)(?:97[89]\d{10}|\d{9}[\dX])(?!\d)", path)
                codes = [isbn_from_identifier(code) for code in path_codes]
                exact = isbn_from_identifier(expected_isbn) if expected_isbn else None
                if exact and exact in codes:
                    selected_path, selected_isbn = path, exact
                    break
                if selected_path is None and not expected_isbn:
                    slug = path.split("/")[-1].replace("-", " ")
                    if self.is_title_coherent(expected_title, slug):
                        selected_path = path
                        selected_isbn = next((code for code in codes if code), None)
            if selected_path is None:
                return failed_offers(self.name, reason="identity_mismatch") if candidates else None
            product_path = selected_path
            product_url = f"{self.BASE_URL}{product_path}"
            found_isbn = selected_isbn
            slug_m = re.search(r"/prix/[^/]+/([^/?]+)", product_path)
            slug_title = slug_m.group(1).replace("-", " ") if slug_m else ""
            title_match = re.search(
                r'<a[^>]+href=["\']' + re.escape(product_path)
                + r'["\'][^>]*>(.*?)</a>', d_html, re.DOTALL,
            )
            found_title = (html.unescape(re.sub(r"<[^>]*>", "", title_match[1]).strip())
                           if title_match else "")
            if not found_title or found_title.isdigit():
                found_title = slug_title or query

            # Cohérence sur titre affiché et slug ; ISBN identique prime (ADR-035).
            isbn_proven = bool(expected_isbn) and found_isbn == expected_isbn

            title_coherent = self.is_title_coherent(expected_title, found_title) or (
                bool(slug_title) and self.is_title_coherent(expected_title, slug_title)
            )
            if expected_isbn and found_isbn and not isbn_proven:
                return failed_offers(self.name, reason="identity_mismatch")
            expected_volume = re.search(r"\b(?:vol(?:ume)?|tome|t)\.?\s*(\d+)\b",
                                        expected_title or "", re.I)
            found_volume = re.search(r"\b(?:vol(?:ume)?|tome|t)\.?\s*(\d+)\b",
                                     found_title or slug_title, re.I)
            if expected_volume and found_volume and expected_volume[1] != found_volume[1]:
                return failed_offers(self.name, reason="volume_mismatch")
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

            stage = "product_page"
            resp_prod = await client.get(product_url, headers=CHASSE_BROWSER_HEADERS, timeout=4)
            failure = _response_failure(resp_prod, stage=stage)
            if failure is not None:
                return failure

            p_html = resp_prod.text
            asin_m = re.search(r'data-asin="([^"]+)"', p_html)
            lvs_m = re.search(r'data-lvs="([^"]+)"', p_html)
            nbeng_m = re.search(r'data-nbeng="(\d+)"', p_html)
            if not (asin_m and lvs_m and nbeng_m):
                return failed_offers(self.name, response=resp_prod, reason="invalid_response",
                                     stage="product_metadata")

            asin = asin_m.group(1)
            product_isbn = isbn_from_identifier(asin)
            if (expected_isbn and product_isbn
                    and product_isbn != isbn_from_identifier(expected_isbn)):
                return failed_offers(self.name, reason="identity_mismatch")
            lvs = lvs_m.group(1)
            nbeng = int(nbeng_m.group(1))
            engines = "_".join(str(i) for i in range(min(nbeng, 32)))

            ajax_headers = _build_ajax_headers(product_url)
            lookup_url = (
                f"{self.BASE_URL}/rest/lookup/results?calls=offers&itemId={asin}"
                f"&retry=0&duih=&lvs={lvs}&ui=&engines={engines}&f=false"
            )
            source_blocked = False
            current_attempt = CollectionAttempt(
                self.name, "current", "success" if "current" in requested_components.get()
                else "skipped",
            )
            try:
                if "current" in requested_components.get():
                    stage = "current_offers"
                    resp_lookup = await client.get(lookup_url, headers=ajax_headers, timeout=4)
                    failure = _response_failure(resp_lookup, stage=stage)
                    if failure is not None:
                        if any(a.reason in {"rate_limited", "bot_challenge"}
                               for a in failure.attempts):
                            return failure
                        resp_lookup.raise_for_status()
                    data = resp_lookup.json()
                else:
                    data = {"offers": {"22": []}}
                raw_offers = data.get("offers")
                if not isinstance(raw_offers, dict):
                    raise ValueError("Offres courantes invalides")

                if "22" not in raw_offers:
                    retry_url = (
                        f"{self.BASE_URL}/rest/lookup/results?calls=offers&itemId={asin}"
                        f"&retry=1&duih=&lvs={lvs}&ui=&engines={engines}&f=false"
                    )
                    try:
                        resp_retry = await client.get(retry_url, headers=ajax_headers, timeout=3.5)
                        failure = _response_failure(resp_retry, stage="current_offers_retry")
                        if failure is not None and any(
                            a.reason in {"rate_limited", "bot_challenge"} for a in failure.attempts
                        ):
                            current_attempt = failure.attempts[0]
                            source_blocked = True
                        if resp_retry.status_code == 200 and not source_blocked:
                            data_retry = resp_retry.json()
                            offers_retry = data_retry.get("offers", {})
                            if isinstance(offers_retry, dict):
                                for k, v in offers_retry.items():
                                    if k not in raw_offers:
                                        raw_offers[k] = v
                                    elif isinstance(v, list) and isinstance(raw_offers[k], list):
                                        raw_offers[k].extend(v)
                    except Exception as exc:
                        logger.debug("Second passage lookup retry=1 ignoré : %s",
                                     type(exc).__name__)

            except (httpx.HTTPError, ValueError, TypeError) as exc:
                current_attempt = failed_offers(self.name, error=exc,
                                                stage="current_offers").attempts[0]
                raw_offers = {"22": []}

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

                    # Marchands étrangers exclus (ex. Amazon DE).
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

                    if is_brand_new_partner({"is_used": is_used, "condition": sub_cond}):
                        continue

                    # États retenus : Très bon et Comme neuf (Bon, Correct rejetés).
                    if not is_very_good_or_better(sub_cond):
                        continue

                    item_cents = off.get("price", {}).get("amount")
                    shipping_cents = off.get("shippingCost", {}).get("amount")
                    if not (isinstance(item_cents, int) and not isinstance(item_cents, bool)
                            and item_cents > 0 and isinstance(shipping_cents, int)
                            and not isinstance(shipping_cents, bool) and shipping_cents >= 0
                            and item_cents + shipping_cents == tot):
                        item_cents, shipping_cents = None, None
                    entry = {
                        "item_price_cents": item_cents,
                        "shipping_price_cents": shipping_cents,
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

            # Plus bas historique sur 1 an via l'API /rest/jcd.
            hist_low_1y_cents: int | None = None
            history_attempt = CollectionAttempt(self.name, "history", "failed", "missing_metadata")
            historical_failed = True
            historical_points: list[MarketOffer] = []
            historical_checked = False
            didh_m = re.search(r'data-didh=["\']([a-f0-9]+)["\']', p_html)
            if source_blocked:
                history_attempt = CollectionAttempt(self.name, "history", "failed",
                                                    "source_restricted", stage="history")
            if didh_m and "history" in requested_components.get() and not source_blocked:
                didh = didh_m.group(1)
                duih_val = await resolve_session_token()
                ui_val = duih_val.split(".")[0] if "." in duih_val else duih_val
                history_params = {
                    "id": asin, "all": "true", "days": "365",
                    "ui": ui_val, "duih": duih_val, "didh": didh,
                }
                try:
                    async with asyncio.timeout(10):
                        resp_jcd = await chasse_session.get_history(
                            client, product_url, history_params, ajax_headers,
                        )
                    failure = _response_failure(resp_jcd, stage="history", operation="history")
                    if resp_jcd.extensions.get("chasse_auth_failed"):
                        history_attempt = CollectionAttempt(
                            self.name, "history", "failed", "session_unavailable",
                            None if resp_jcd.extensions.get("request_skipped")
                            else resp_jcd.status_code, stage="history",
                        )
                    elif failure is not None:
                        history_attempt = failure.attempts[0]
                    if failure is None and resp_jcd.status_code == 200 and resp_jcd.content:
                        jcd_data = resp_jcd.json()
                        if (not isinstance(jcd_data, dict)
                                or not isinstance(jcd_data.get("stats"), list)):
                            raise ValueError("Série historique invalide")
                        historical_failed = False
                        historical_checked = True
                        history_attempt = CollectionAttempt(self.name, "history")
                        jcd_data["price_includes_shipping"] = True
                        historical_points = parse_historical_observations(jcd_data, product_url)
                        source_points = [point for point in historical_points
                                         if point.item_condition == "Très bon état +"]
                        hist_low_1y_cents = select_historical_low_cents({
                            "Très bon état +": min(
                                point.delivered_price_cents for point in source_points
                            ) / 100,
                        }) if source_points else None
                except Exception as jcd_exc:
                    historical_failed = True
                    history_attempt = failed_offers(
                        self.name, operation="history", error=jcd_exc, stage="history",
                    ).attempts[0]
                    logger.warning("Historique Chasse-aux-livres indisponible (%s)",
                                   type(jcd_exc).__name__)

            if "history" not in requested_components.get():
                historical_failed = False
                history_attempt = CollectionAttempt(self.name, "history", "skipped")
            current_failed = current_attempt.status == "failed"
            if historical_failed:
                logger.warning("Historique Chasse-aux-livres non reçu : réponse vide, "
                               "invalide ou refusée")
            # Aucune occasion en stock : retourner l'historique seul (cote indisponible).
            if not best_used:
                if hist_low_1y_cents:
                    return MarketOffers([*historical_points,
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
                    ], historical_checked=historical_checked,
                       failed_providers=(self.name,) if historical_failed or current_failed else (),
                       attempts=(current_attempt, history_attempt))
                return MarketOffers(historical_points,
                                    failed_providers=(
                                        (self.name,) if historical_failed or current_failed else ()
                                    ),
                                    historical_checked=historical_checked,
                                    attempts=(current_attempt, history_attempt))

            primary = best_used
            cond_str = f"{primary['condition']} ({primary['merchant']})"

            return MarketOffers(
                [*historical_points,
                    MarketOffer(
                        provider_name=self.name,
                        item_price_cents=primary["item_price_cents"] or primary["total_cents"],
                        shipping_price_cents=primary["shipping_price_cents"],
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
                failed_providers=((self.name,) if historical_failed or current_failed else ()),
                historical_checked=historical_checked,
                attempts=(current_attempt, history_attempt),
            )
        except Exception as exc:
            logger.debug("Échec de l'extraction détaillée Chasse-aux-livres: %s",
                         type(exc).__name__)
            return failed_offers(self.name, error=exc, stage=stage)

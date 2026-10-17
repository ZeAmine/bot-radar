"""Service métier pour la gestion et validation des alertes."""

from math import isfinite
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.core.url_validator import is_safe_external_url


class AlertServiceError(Exception):
    """Exception levée en cas d'erreur de validation métier des alertes."""

    pass


def parse_price_to_cents(price_str: str) -> int:
    """Convertit une chaîne représentant un montant en euros en centimes.

    Accepte les formats comme "15", "15.50", "15,50", " 15 € ".
    """
    cleaned = price_str.strip().replace("€", "").replace(" ", "").replace(",", ".")
    try:
        amount_float = float(cleaned)
        if not isfinite(amount_float) or not isfinite(amount_float * 100):
            raise AlertServiceError("Le prix doit être un montant fini.")
        if amount_float < 0:
            raise AlertServiceError("Le prix doit être positif.")
        return round(amount_float * 100)
    except ValueError as err:
        raise AlertServiceError(f"Format de prix invalide : '{price_str}'.") from err


def parse_comma_separated_list(text: str) -> list[str]:
    """Découpe et nettoie une chaîne de mots-clés séparés par des virgules."""
    items: list[str] = []
    for part in text.split(","):
        cleaned = part.strip()
        if cleaned and cleaned not in items:
            items.append(cleaned)
    return items


def validate_and_parse_vinted_url(url: str) -> dict[str, Any]:
    """Valide une URL de recherche Vinted et extrait ses paramètres de recherche.

    Règles strictes :
    - Doit être une URL HTTPS.
    - Le domaine doit appartenir à vinted.fr ou vinted.com (anti-SSRF validé).
    - Ne jamais exécuter de requête HTTP réseau vers l'URL.
    """
    url_cleaned = url.strip()
    is_safe, reason = is_safe_external_url(
        url_cleaned,
        allowed_schemes=("https",),
        allowed_domains=("vinted.fr", "vinted.com"),
    )
    if not is_safe:
        raise AlertServiceError(
            "Le lien doit impérativement provenir de vinted.fr ou vinted.com en HTTPS.")

    try:
        parsed = urlparse(url_cleaned)
        netloc = parsed.netloc.lower()
    except Exception as err:
        raise AlertServiceError("Format d'URL invalide.") from err

    query_params = parse_qs(parsed.query)

    search_text: str | None = None
    if "search_text" in query_params and query_params["search_text"]:
        search_text = query_params["search_text"][0].strip()

    price_to_cents: int | None = None
    if "price_to" in query_params and query_params["price_to"]:
        raw_price = query_params["price_to"][0].strip()
        try:
            price_to_cents = parse_price_to_cents(raw_price)
        except AlertServiceError:
            price_to_cents = None

    return {
        "url": url_cleaned,
        "domain": netloc,
        "search_text": search_text,
        "price_to_cents": price_to_cents,
        "all_params": {k: v[0] if len(v) == 1 else v for k, v in query_params.items()},
    }

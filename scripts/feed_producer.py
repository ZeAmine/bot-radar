"""Script client autonome pour pousser des annonces vers l'API d'ingestion de Radar Bot.

Ce script sert de connecteur de référence pour tout scraper, flux RSS ou service tiers
qui alimente Radar Bot en annonces de seconde main.
"""

import argparse
import json
import logging
import os
from typing import Any

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("radar.producer")


def push_listing(
    api_url: str,
    api_token: str,
    listing_data: dict[str, Any],
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Envoie une annonce à l'endpoint d'ingestion de Radar Bot.

    Args:
        api_url: URL complète de l'endpoint (ex: http://localhost:8000/api/v1/ingest/listings)
        api_token: Jeton secret INGEST_API_TOKEN
        listing_data: Dictionnaire respectant le schéma ListingIngestPayload
        timeout: Délai d'expiration de la requête HTTP en secondes

    Returns:
        Réponse JSON renvoyée par le serveur.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
    }

    endpoint = api_url.rstrip("/")
    if not endpoint.endswith("/listings"):
        endpoint = f"{endpoint}/api/v1/ingest/listings"

    with httpx.Client(timeout=timeout) as client:
        try:
            response = client.post(endpoint, headers=headers, json=listing_data)
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            logger.info(
                "Annonce envoyée avec succès : id=%s, duplicate=%s, matches=%s",
                result.get("listing_id"),
                result.get("is_duplicate"),
                len(result.get("matches", [])),
            )
            return result
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Erreur HTTP %s lors de l'ingestion : %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise
        except httpx.RequestError as exc:
            logger.error("Impossible de joindre le serveur Radar Bot à %s : %s", endpoint, exc)
            raise


def build_sample_listing(
    external_id: str,
    title: str,
    category_slug: str,
    item_price_cents: int,
    condition: str = "Très bon état",
    url: str | None = None,
    photos: list[str] | None = None,
    isbn13: str | None = None,
) -> dict[str, Any]:
    """Fabrique un dictionnaire d'annonce standardisé."""
    payload: dict[str, Any] = {
        "source_name": "vinted",
        "external_id": external_id,
        "url": url or f"https://www.vinted.fr/items/{external_id}",
        "title": title,
        "description": f"Exemplaire {title} en {condition}.",
        "category_slug": category_slug,
        "item_price_cents": item_price_cents,
        "estimated_protection_cents": 70 + int(item_price_cents * 0.05),
        "estimated_shipping_cents": 299,
        "condition": condition,
        "seller_name": "vendeur_pro",
        "seller_rating": 4.85,
        "photos": photos or [],
    }
    if isbn13:
        payload["isbn13"] = isbn13
    return payload


def main() -> None:
    """Point d'entrée CLI pour tester ou automatiser l'ingestion d'annonces."""
    parser = argparse.ArgumentParser(description="Radar Bot - Producteur d'annonces")
    parser.add_argument(
        "--url",
        default=os.getenv("RADAR_API_URL", "http://localhost:8000"),
        help="URL de base de Radar Bot (défaut: http://localhost:8000)",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("INGEST_API_TOKEN", "your_secure_ingest_token"),
        help="Jeton secret d'ingestion (défaut: lu depuis .env ou your_secure_ingest_token)",
    )
    parser.add_argument(
        "--file",
        help="Chemin vers un fichier JSON contenant une annonce ou une liste d'annonces",
    )
    parser.add_argument("--id", default="demo_001", help="Identifiant externe unique de l'annonce")
    parser.add_argument("--title", default="Tintin en Amérique", help="Titre de l'annonce")
    parser.add_argument(
        "--category",
        default="bd_comics",
        choices=["bd_comics", "clothes", "tech"],
        help="Catégorie de l'annonce",
    )
    parser.add_argument(
        "--price",
        type=int,
        default=10,
        help="Prix de l'article en euros (défaut: 10)",
    )
    parser.add_argument(
        "--condition",
        default="Très bon état",
        help="État de l'article",
    )
    parser.add_argument(
        "--isbn",
        default=None,
        help="ISBN-13 optionnel de la BD / du livre (ex: 9782205049657)",
    )
    parser.add_argument(
        "--photo",
        default=None,
        help="URL optionnelle de la photo principale",
    )

    args = parser.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                logger.info("Envoi d'un lot de %d annonces...", len(data))
                for item in data:
                    push_listing(args.url, args.token, item)
            else:
                push_listing(args.url, args.token, data)
    else:
        sample = build_sample_listing(
            external_id=args.id,
            title=args.title,
            category_slug=args.category,
            item_price_cents=args.price * 100,
            condition=args.condition,
            isbn13=args.isbn,
            photos=[args.photo] if args.photo else [],
        )
        push_listing(args.url, args.token, sample)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Envoie une notification Telegram lors d'un déploiement réussi."""

from __future__ import annotations

import argparse
import base64
import os
import sys

import httpx

# Permet d'importer app quand le script est exécuté depuis la racine ou dans Docker
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402


def send_telegram_notification(text: str, dry_run: bool = False) -> bool:
    """Envoie un message HTML aux utilisateurs autorisés via l'API Telegram."""
    if not text.strip():
        print("Message vide, aucun envoi.")
        return True

    allowed_ids = settings.TELEGRAM_ALLOWED_USER_IDS
    token = settings.TELEGRAM_BOT_TOKEN

    if not token or not allowed_ids:
        print("Telegram non configuré (TELEGRAM_BOT_TOKEN ou TELEGRAM_ALLOWED_USER_IDS manquant).")
        return False

    if dry_run:
        print(f"[DRY-RUN] Notification vers {len(allowed_ids)} utilisateur(s) :\n{text}")
        return True

    success = True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with httpx.Client(timeout=10.0) as client:
        for chat_id in allowed_ids:
            try:
                resp = client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code != 200:
                    print(
                        f"Erreur Telegram ({resp.status_code}) pour chat_id {chat_id}: {resp.text}"
                    )
                    success = False
            except Exception as exc:
                print(f"Échec d'envoi Telegram pour chat_id {chat_id}: {exc}")
                success = False

    return success


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", default=None, help="Texte du message à envoyer")
    parser.add_argument(
        "--base64", dest="b64_text", default=None, help="Message encodé en base64"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Affiche le message sans l'envoyer"
    )
    args = parser.parse_args()

    message = ""
    if args.b64_text:
        try:
            message = base64.b64decode(args.b64_text).decode("utf-8")
        except Exception as exc:
            print(f"Erreur de décodage base64: {exc}", file=sys.stderr)
            return 1
    elif args.text:
        message = args.text
    elif not sys.stdin.isatty():
        message = sys.stdin.read()

    if not message.strip():
        print("Aucun message fourni à envoyer.", file=sys.stderr)
        return 1

    ok = send_telegram_notification(message.strip(), dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

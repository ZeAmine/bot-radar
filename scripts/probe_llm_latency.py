#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel  # noqa: E402

from app.core.http_client import close_shared_http_client  # noqa: E402
from app.providers.llm.base import LLMClient  # noqa: E402
from app.providers.llm.registry import build_llm_client  # noqa: E402
from app.services.ai.modules.keyword_variants import (  # noqa: E402
    OPTIMIZE_SYSTEM_PROMPT,
    KeywordVariantsModule,
)
from app.services.ai.modules.natural_add import SYSTEM_PROMPT  # noqa: E402
from app.services.ai.schemas import KeywordVariantsPayload, NaturalAddPayload  # noqa: E402


async def measure(
    client: LLMClient, name: str, system_prompt: str, user_prompt: str,
    response_model: type[BaseModel],
) -> tuple[bool, float]:
    started = time.monotonic()
    data = await client.generate_json(
        system_prompt, user_prompt, temperature=0.1 if name == "natural_add" else 0.3,
        response_model=response_model, ai_module=name,
    )
    elapsed = time.monotonic() - started
    ok = data is not None
    print(f"{name}: {elapsed:.2f}s ; {'réponse validée' if ok else 'échec ou aucune réponse'}")
    return ok, elapsed


async def _run(args: argparse.Namespace) -> int:
    client = build_llm_client()
    if not client.is_configured:
        print("Moteur IA désactivé ou non configuré.", file=sys.stderr)
        return 2
    print(f"Fournisseur : {client.vendor} ; modèle : {client.model}")
    parsing, _ = await measure(
        client, "natural_add", SYSTEM_PROMPT,
        f"Requête utilisateur : {args.title}, max 10€", NaturalAddPayload,
    )
    variants, _ = await measure(
        client, "keyword_variants", OPTIMIZE_SYSTEM_PROMPT,
        KeywordVariantsModule._build_user_prompt(args.title, args.category, None),
        KeywordVariantsPayload,
    )
    return 0 if parsing and variants else 1


async def run(args: argparse.Namespace) -> int:
    try:
        return await _run(args)
    finally:
        await close_shared_http_client()


def main() -> int:
    parser = argparse.ArgumentParser(description="Mesure deux appels IA du fournisseur configuré.")
    parser.add_argument("--title", default="Regards")
    parser.add_argument("--category", default="bd_comics")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

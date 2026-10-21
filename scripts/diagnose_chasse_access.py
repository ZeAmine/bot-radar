import argparse
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from dotenv.parser import parse_stream

ROOT = Path(__file__).resolve().parents[1]


class PrivateFileStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def get(self, key: str) -> str | None:
        try:
            values = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        value = values.get(key)
        return value if isinstance(value, str) else None

    async def set(self, key: str, value: str) -> bool:
        try:
            values = json.loads(self.path.read_text())
        except FileNotFoundError:
            values = {}
        values[key] = value
        descriptor, temporary = tempfile.mkstemp(prefix=".chasse-session-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(values, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True


async def diagnose(store: PrivateFileStore, *, full_collection: bool = False) -> dict[str, Any]:
    import httpx

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
    from app.providers.chasse_session import BASE_URL, ChasseSessionManager, valid_history

    async def no_notification(_message: str) -> bool:
        return False

    async def guard(request: httpx.Request) -> None:
        if request.url.scheme != "https" or request.url.host != "www.chasse-aux-livres.fr":
            raise ValueError("unexpected_destination")

    offer_fields: dict[str, Any] = {}
    source_responses: list[dict[str, Any]] = []

    async def inspect_offer_fields(response: httpx.Response) -> None:
        path = response.request.url.path
        if path in {"/rest/search-results", "/rest/lookup/results"}:
            await response.aread()
            try:
                payload = response.json()
                shape = ({key: type(payload.get(key)).__name__ for key in ("d", "offers")
                          if key in payload} if isinstance(payload, dict)
                         else {"payload": type(payload).__name__})
            except ValueError:
                shape = {"payload": "non_json"}
            source_responses.append({"stage": path, "status": response.status_code,
                                     "shape": shape})
        if path != "/rest/lookup/results":
            return
        await response.aread()
        if response.status_code != 200:
            return
        try:
            groups = response.json().get("offers", {})
            for group in groups.values():
                if isinstance(group, list) and group:
                    offer_fields.update({key: sorted(value) if isinstance(value, dict)
                                         else type(value).__name__
                                         for key, value in group[0].items()
                                         if "price" in key.lower() or "shipping" in key.lower()})
                    return
        except (ValueError, TypeError, AttributeError):
            return

    session = ChasseSessionManager(store=store, notify=no_notification)
    product_url = BASE_URL + (
        "/prix/2756079332/jean-doux-et-le-mystere-de-la-disquette-molle-"
        "philippe-valette-9782756079332"
    )
    async with httpx.AsyncClient(
        headers=CHASSE_BROWSER_HEADERS,
        event_hooks={"request": [guard], "response": [inspect_offer_fields]},
        timeout=8,
        trust_env=False,
    ) as raw_client:
        try:
            probe_token = await cal_governor.admit_book("script-diagnostic", counts_as_book=False)
        except CalSuspendedError as exc:
            return {"history_access_verified": False,
                    "reason": f"cal_deferred:{exc.reason}"}
        budget = CalCollectionBudget(cal_governor)
        budget.probe_token = probe_token
        client = GovernedClient(raw_client, budget)
        page = await client.get(product_url, follow_redirects=False)
        if page.status_code != 200 or "9782756079332" not in page.text:
            await cal_governor.conclude_probe(probe_token, succeeded=False,
                                              reason="diagnostic_page")
            return {"history_access_verified": False, "reason": "product_unavailable"}
        attrs = {
            key: re.search(r"data-" + key + r"=[\"\x27]([^\"\x27]+)", page.text)
            for key in ("asin", "didh")
        }
        if not all(attrs.values()):
            await cal_governor.conclude_probe(probe_token, succeeded=False,
                                              reason="diagnostic_metadata")
            return {"history_access_verified": False, "reason": "missing_product_metadata"}
        params = {
            "id": attrs["asin"][1],
            "didh": attrs["didh"][1],  # type: ignore[index]
            "all": "true",
            "days": "365",
        }
        response = await session.get_history(
            client,
            product_url,
            params,
            _build_ajax_headers(product_url),
        )
        if not valid_history(response):
            await cal_governor.conclude_probe(probe_token, succeeded=False,
                                              reason="diagnostic_history")
            return {"history_access_verified": False, "reason": "history_unavailable"}
        await cal_governor.conclude_probe(probe_token, succeeded=True)
        data = response.json()
        data["price_includes_shipping"] = True
        points = [
            point
            for point in parse_historical_observations(data, product_url)
            if point.item_condition == "Très bon état +"
        ]
        minimum = min(points, key=lambda point: point.delivered_price_cents) if points else None
        report = {
            "history_access_verified": True,
            "account_login_attempted": session.login_attempted,
            "isbn": "9782756079332",
            "condition": "Très bon état +",
            "days": 365,
            "shipping_included": True,
            "minimum_cents": minimum.delivered_price_cents if minimum else None,
            "minimum_observed_at": minimum.observed_at.isoformat()
            if minimum and minimum.observed_at
            else None,
            "database_modified": False,
            "redis_used": False,
        }
        if full_collection:
            from app.providers import chasse_aux_livres

            chasse_aux_livres.chasse_session = session
            provider = chasse_aux_livres.ChasseAuxLivresProvider(http_client=client, max_retries=0)
            async with asyncio.timeout(30):
                collected = await provider.get_offers(
                    isbn13="9782756079332",
                    title="Jean Doux et le mystère de la disquette molle",
                )
            live = [offer for offer in collected if offer.is_available and not offer.is_historical]
            historical = [offer for offer in collected if offer.is_historical
                          and offer.item_condition == "Très bon état +"]
            report.update({
                "collector_history_checked": getattr(collected, "historical_checked", False),
                "collector_history_minimum_cents": min(
                    (offer.delivered_price_cents for offer in historical), default=None,
                ),
                "collector_last_offer_cents": min(
                    (offer.delivered_price_cents for offer in live), default=None,
                ),
                "collector_partner_count": sum(len((offer.raw_payload or {}).get("partners", {}))
                                               for offer in live),
                "collector_failures": list(getattr(collected, "failed_providers", ())),
                "collector_price_fields": offer_fields,
                "source_responses": source_responses,
                "collector_attempts": [{"operation": attempt.operation,
                                        "status": attempt.status, "reason": attempt.reason,
                                        "http_status": attempt.http_status}
                                       for attempt in getattr(collected, "attempts", ())],
            })
        return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--full-collection", action="store_true")
    args = parser.parse_args()
    args.session_file = args.session_file.resolve()
    logging.disable(logging.CRITICAL)
    allowed = {"CHASSE_AUX_LIVRES_DUIH", "CHASSE_ACCOUNT_EMAIL", "CHASSE_ACCOUNT_PASSWORD"}
    try:
        with args.env_file.open() as stream:
            selected = {
                binding.key: binding.value
                for binding in parse_stream(stream)
                if binding.key in allowed and binding.value
            }
        os.environ.clear()
        os.environ.update(selected)
        sys.path.insert(0, str(ROOT))
        with tempfile.TemporaryDirectory(prefix="radar-chasse-probe-") as directory:
            os.chdir(directory)
            report = asyncio.run(diagnose(
                PrivateFileStore(args.session_file.resolve()), full_collection=args.full_collection,
            ))
        print(json.dumps(report, ensure_ascii=False))
        verified = report["history_access_verified"]
        if args.full_collection:
            verified = verified and report.get("collector_history_checked", False)
            verified = verified and not report.get("collector_failures")
        return 0 if verified else 1
    except Exception as exc:
        print(json.dumps({"history_access_verified": False, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

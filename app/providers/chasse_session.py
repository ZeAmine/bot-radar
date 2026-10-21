import asyncio
import hashlib
import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx

from app.core.config import settings
from app.core.redis import get_redis_client

if TYPE_CHECKING:
    from app.providers.cal_governor import CalHttpClient

logger = logging.getLogger(__name__)
BASE_URL = "https://www.chasse-aux-livres.fr"
SESSION_KEY = "radar:chasse:session:v2"
LEGACY_KEY = "radar:chasse:duih"
RETRY_HOURS = 6


def source_restriction(response: httpx.Response) -> str | None:
    if response.status_code == 429:
        return "rate_limited"
    if ("vérification robot" in response.text.casefold()
            or response.headers.get("cf-mitigated") == "challenge"):
        return "bot_challenge"
    return None


def valid_history(response: httpx.Response) -> bool:
    if response.status_code != 200 or not response.content:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    return isinstance(data, dict) and isinstance(data.get("stats"), list)


async def _notify(message: str) -> bool:
    from app.services.telegram_service import notify_admins

    return await notify_admins(message)


class ChasseSessionManager:
    def __init__(
        self,
        *,
        store: Any = None,
        notify: Callable[[str], Awaitable[bool]] = _notify,
    ) -> None:
        self.store = store
        self.notify = notify
        self.lock = asyncio.Lock()
        self.load_lock = asyncio.Lock()
        self.state: dict[str, Any] = {}
        self.loaded = False
        self.login_attempted = False

    @staticmethod
    def _seed() -> str:
        values = (
            settings.CHASSE_AUX_LIVRES_DUIH or "",
            settings.CHASSE_ACCOUNT_EMAIL.get_secret_value(),
            settings.CHASSE_ACCOUNT_PASSWORD.get_secret_value(),
        )
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    async def _load(self) -> None:
        async with self.load_lock:
            seed = self._seed()
            if self.loaded and self.state.get("seed") == seed:
                return
            self.state = {"seed": seed}
            try:
                store = self.store if self.store is not None else get_redis_client()
                raw = await asyncio.wait_for(store.get(SESSION_KEY), timeout=2)
                stored = json.loads(raw) if raw else {}
                if isinstance(stored, dict) and stored.get("seed") == seed:
                    self.state = stored
                elif not settings.CHASSE_AUX_LIVRES_DUIH:
                    legacy = await asyncio.wait_for(store.get(LEGACY_KEY), timeout=2)
                    if isinstance(legacy, bytes):
                        legacy = legacy.decode()
                    if isinstance(legacy, str):
                        self.state["token"] = legacy.strip()
                        self.state["origin"] = "redis"
            except Exception as exc:
                logger.warning("État de session Chasse inaccessible : %s", type(exc).__name__)
            self.loaded = True

    async def _save(self) -> None:
        try:
            store = self.store if self.store is not None else get_redis_client()
            await asyncio.wait_for(store.set(SESSION_KEY, json.dumps(self.state)), timeout=2)
        except Exception as exc:
            logger.warning("Persistance de session Chasse impossible : %s", type(exc).__name__)

    async def token(self) -> str:
        await self._load()
        value = self.state.get("token")
        return (
            value
            if isinstance(value, str) and value
            else (settings.CHASSE_AUX_LIVRES_DUIH or "").strip()
        )

    async def install(self, token: str) -> None:
        async with self.lock:
            self.loaded = True
            self.state = {"seed": self._seed(), "token": token, "origin": "redis"}
            await self._save()

    async def source(self) -> str | None:
        value = await self.token()
        if not value:
            return None
        origin = self.state.get("origin")
        if isinstance(origin, str):
            return origin
        return "env" if settings.CHASSE_AUX_LIVRES_DUIH else "redis"

    def _in_rest(self) -> bool:
        value = self.state.get("retry_at")
        if not isinstance(value, str):
            return False
        try:
            return datetime.fromisoformat(value) > datetime.now(UTC)
        except (TypeError, ValueError):
            return False

    async def _failed(self) -> None:
        self.state["retry_at"] = (datetime.now(UTC) + timedelta(hours=RETRY_HOURS)).isoformat()
        await self._save()
        if not self.state.get("notified"):
            try:
                delivered = await self.notify(
                    "🔑 Le compte Chasse aux livres doit être reconnecté. "
                    "Les derniers prix connus sont conservés."
                )
                if delivered:
                    self.state["notified"] = True
            except Exception as exc:
                logger.warning("Notification de session Chasse impossible : %s", type(exc).__name__)
        await self._save()

    @staticmethod
    async def _login(client: httpx.AsyncClient, headers: dict[str, str]) -> str | None:
        email = settings.CHASSE_ACCOUNT_EMAIL.get_secret_value()
        password = settings.CHASSE_ACCOUNT_PASSWORD.get_secret_value()
        if not email or not password:
            return None
        async with asyncio.timeout(8):
            page = await client.get(
                BASE_URL + "/login?protect=true", headers=headers, follow_redirects=False, timeout=4
            )
            if page.status_code != 200 or any(
                marker in page.text.lower()
                for marker in ("g-recaptcha", "h-captcha", "cf-chl-", "vérification robot")
            ):
                return None
            fields = {}
            for tag in re.findall(r"<input\b[^>]*>", page.text, re.I):
                attrs = dict(re.findall(r"([\w-]+)=[\"\x27]([^\"\x27]*)", tag))
                if attrs.get("name") in {"_sourcePage", "__fp"}:
                    fields[attrs["name"]] = html.unescape(attrs.get("value", ""))
            if not fields.get("_sourcePage") or not fields.get("__fp"):
                return None
            response = await client.post(
                BASE_URL + "/login",
                headers=headers,
                data={**fields, "email": email, "password": password, "protect": "true"},
                follow_redirects=False,
                timeout=4,
            )
            if response.status_code not in {200, 302, 303} or re.search(
                r"<form\b[^>]*(?:id|name)=[\"\x27]login[\"\x27]",
                response.text,
                re.I,
            ):
                return None
            home = await client.get(
                BASE_URL + "/", headers=headers, follow_redirects=False, timeout=4
            )
            if home.status_code != 200:
                return None
            match = re.search(r"data-duih=[\"\x27]([^\"\x27]+)", home.text)
            value = html.unescape(match[1]) if match else ""
            return value if value not in {"", "0", "false", "null"} else None

    async def get_history(
        self,
        client: "CalHttpClient",
        product_url: str,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> httpx.Response:
        await self._load()
        token = params.get("duih") or await self.token()

        async def request(value: str) -> httpx.Response:
            query = {**params, "duih": value, "ui": value.split(".")[0]}
            return await client.get(
                BASE_URL + "/rest/jcd",
                params=query,
                headers=headers,
                timeout=4,
                follow_redirects=False,
            )

        if self._in_rest():
            return httpx.Response(401, extensions={"chasse_auth_failed": True,
                                                  "request_skipped": True})
        response = await request(token)
        if source_restriction(response):
            return response
        if valid_history(response):
            await self._verified(token, product_url)
            return response
        if response.status_code == 200 and response.content:
            try:
                response.json()
            except ValueError:
                pass
            else:
                return response
        if response.status_code not in {200, 301, 302, 303, 401, 403}:
            return response
        async with self.lock:
            if self._in_rest():
                return response
            current = await self.token()
            if current and current != token:
                response = await request(current)
                if source_restriction(response):
                    return response
                if valid_history(response):
                    await self._verified(current, product_url)
                    return response
            try:
                self.login_attempted = True
                renewed = await self._login(
                    cast(httpx.AsyncClient, client), headers,
                )
                if renewed:
                    response = await request(renewed)
                    if source_restriction(response):
                        return response
                    if valid_history(response):
                        self.state["origin"] = "renewed"
                        await self._verified(renewed, product_url)
                        return response
            except asyncio.CancelledError:
                self.state["retry_at"] = (
                    datetime.now(UTC) + timedelta(hours=RETRY_HOURS)
                ).isoformat()
                await asyncio.shield(self._save())
                raise
            except (httpx.HTTPError, TimeoutError) as exc:
                logger.warning("Reconnexion Chasse interrompue : %s", type(exc).__name__)
            await self._failed()
            response.extensions["chasse_auth_failed"] = True
            return response

    async def maintain(self) -> None:
        from app.core.http_client import get_shared_http_client
        from app.providers.chasse_aux_livres import CHASSE_BROWSER_HEADERS, _build_ajax_headers

        await self._load()
        url = self.state.get("probe_url")
        if not isinstance(url, str) or not url.startswith(BASE_URL + "/prix/"):
            return
        if self._in_rest():
            return
        now = datetime.now(UTC)
        checked = self.state.get("maintenance_at") or self.state.get("verified_at")
        if isinstance(checked, str):
            try:
                if now - datetime.fromisoformat(checked) < timedelta(hours=RETRY_HOURS):
                    return
            except (TypeError, ValueError):
                pass
        self.state["maintenance_at"] = now.isoformat()
        await self._save()
        client = get_shared_http_client()
        try:
            page = await client.get(url, headers=CHASSE_BROWSER_HEADERS,
                                    follow_redirects=False, timeout=4)
            if page.status_code != 200:
                return
            attrs = {key: re.search(r'data-' + key + r'=["\']([^"\']+)', page.text)
                     for key in ("asin", "didh")}
            asin, didh = attrs["asin"], attrs["didh"]
            if asin is None or didh is None:
                return
            await self.get_history(
                client, url, {"id": asin[1], "didh": didh[1], "all": "true", "days": "365"},
                _build_ajax_headers(url),
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            logger.warning("Vérification de session Chasse interrompue : %s", type(exc).__name__)

    async def _verified(self, token: str, product_url: str) -> None:
        last = self.state.get("verified_at")
        now = datetime.now(UTC)
        changed = self.state.get("token") != token or self.state.get("retry_at")
        self.state.update(token=token, probe_url=product_url)
        self.state.pop("retry_at", None)
        self.state.pop("notified", None)
        try:
            recent = (isinstance(last, str)
                      and now - datetime.fromisoformat(last) < timedelta(minutes=15))
        except (TypeError, ValueError):
            recent = False
        if changed or not recent:
            self.state["verified_at"] = now.isoformat()
            await self._save()


chasse_session = ChasseSessionManager()

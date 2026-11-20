"""HTTP-laag voor de interne API: headers, querystrings, fouten en tokenvernieuwing bij 401."""

from __future__ import annotations

from datetime import date
import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from .auth import TokenManager
from .const import BASE_URL, ORIGIN, USER_AGENT
from .exceptions import (
    NOT_APPLICABLE_CODES,
    ApiError,
    AuthenticationError,
    CloudflareError,
    NotApplicableError,
    parse_error,
)

_LOGGER = logging.getLogger(__name__)

QueryValue = str | int | float | bool | date | None
Query = list[tuple[str, QueryValue]]


def build_query(items: Query) -> str:
    """Querystring zoals de webapp hem bouwt: herhaalbare namen, ``yyyy-mm-dd``, ``true``/``false``.

    ``None``-waarden worden overgeslagen.
    """
    parts = []
    for name, value in items:
        if value is None:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, date):
            text = value.isoformat()
        else:
            text = str(value)
        parts.append(f"{quote(name, safe='')}={quote(text, safe='')}")
    return "&".join(parts)


class Transport:
    """Verstuurt requests naar ``https://monitoring.solaredge.com/services/``.

    Gebruik een eigen ``aiohttp.ClientSession`` met cookie jar (Cloudflare-cookies), geen gedeelde sessie.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: TokenManager | None,
        bearer_token: str | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        """``tokens`` levert het access token; met ``bearer_token`` wordt een vast token gebruikt."""
        self._session = session
        self._tokens = tokens
        self._bearer = bearer_token
        self._base = base_url

    async def get(self, path: str, query: Query | None = None) -> Any:
        """GET; geeft de JSON (of ``None`` bij een lege body)."""
        return await self.request("GET", path, query)

    async def post(self, path: str, body: Any = None, query: Query | None = None) -> Any:
        """POST met optionele JSON-body; geeft de JSON (of ``None``)."""
        return await self.request("POST", path, query, body)

    async def request(self, method: str, path: str, query: Query | None = None, body: Any = None) -> Any:
        """Verstuur een request en geef de geparste JSON terug."""
        text, status = await self.request_raw(method, path, query, body)
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            raise ApiError(f"{method} {path}: response is geen JSON", status, raw=text) from err

    async def request_raw(self, method: str, path: str, query: Query | None = None, body: Any = None) -> tuple[str, int]:
        """Verstuur een request en geef (ruwe tekst, status); foutstatussen worden exceptions."""
        url = self._base + path.lstrip("/")
        if query:
            qs = build_query(query)
            if qs:
                url = f"{url}?{qs}"

        resp_text, status, headers = await self._send(method, url, body, force_renew=False)
        if status == 401 and self._tokens and not self._bearer:
            _LOGGER.debug("401 op %s: token vernieuwen en opnieuw proberen", path)
            resp_text, status, headers = await self._send(method, url, body, force_renew=True)

        if 300 <= status < 400:
            raise AuthenticationError(f"Redirect ({status}) naar {headers.get('Location')}: sessie ongeldig")
        if status >= 400:
            code, detail = parse_error(resp_text)
            message = f"{method} {path} mislukt: {status} {code or ''} {detail or resp_text[:300]}".strip()
            if status == 401:
                raise AuthenticationError(message)
            not_applicable = code in NOT_APPLICABLE_CODES or (status == 403 and path.startswith("alerts/"))
            error_type = NotApplicableError if not_applicable else ApiError
            raise error_type(message, status, code, detail, resp_text)
        if "text/html" in headers.get("Content-Type", ""):
            raise CloudflareError(f"{method} {path} gaf HTML in plaats van JSON (Cloudflare?)", status, raw=resp_text)
        return resp_text, status

    async def _send(self, method: str, url: str, body: Any, force_renew: bool) -> tuple[str, int, Any]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "origin": ORIGIN,
            "referer": ORIGIN + "/one",
            "user-agent": USER_AGENT,
        }
        token = self._bearer or (await self._tokens.async_get_access_token(force_renew) if self._tokens else None)
        if token:
            headers["authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            headers["content-type"] = "application/json"
            data = json.dumps(body)
        async with self._session.request(method, url, data=data, headers=headers, allow_redirects=False) as resp:
            return await resp.text(), resp.status, resp.headers

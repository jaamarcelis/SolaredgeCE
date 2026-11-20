"""Minimale nep-``aiohttp.ClientSession`` voor de API-tests (onafhankelijk van aiohttp-internals)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import re
from typing import Any

from multidict import CIMultiDict

BASE = "https://monitoring.solaredge.com/services/"


@dataclass
class Call:
    """Eén verstuurde request."""

    method: str
    url: str
    headers: dict[str, str]
    data: str | None
    allow_redirects: bool | None

    @property
    def path(self) -> str:
        """URL zonder het basisadres van de API."""
        return self.url.removeprefix(BASE)

    @property
    def json(self) -> Any:
        """De body als JSON."""
        return json.loads(self.data) if self.data else None


@dataclass
class FakeResponse:
    """Response met de velden die de client gebruikt."""

    status: int = 200
    body: str = ""
    headers: CIMultiDict = field(default_factory=CIMultiDict)

    async def text(self) -> str:
        return self.body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


Responder = Callable[[Call], FakeResponse]


class FakeSession:
    """Geeft per request het eerste passende antwoord (op methode + URL-regex) en onthoudt alle calls."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._routes: list[tuple[str, re.Pattern[str], list[Responder], bool]] = []

    def add(
        self,
        method: str,
        pattern: str,
        *,
        json_body: Any = None,
        status: int = 200,
        text: str | None = None,
        headers: dict[str, str] | None = None,
        repeat: bool = True,
    ) -> FakeSession:
        """Registreer een antwoord. ``pattern`` is een regex op de URL zonder basisadres."""
        body = text if text is not None else ("" if json_body is None else json.dumps(json_body))
        hdrs = CIMultiDict(headers or {"Content-Type": "application/json"})
        self._routes.append((method, re.compile(pattern), [lambda _c: FakeResponse(status, body, hdrs)], repeat))
        return self

    def add_callback(self, method: str, pattern: str, responder: Responder, repeat: bool = True) -> FakeSession:
        """Registreer een antwoord dat per call berekend wordt."""
        self._routes.append((method, re.compile(pattern), [responder], repeat))
        return self

    def request(
        self,
        method: str,
        url: str,
        *,
        data: str | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool | None = None,
        **_: Any,
    ) -> FakeResponse:
        call = Call(method, url, dict(headers or {}), data, allow_redirects)
        self.calls.append(call)
        path = url.removeprefix(BASE)
        for index, (m, pattern, responders, repeat) in enumerate(self._routes):
            if m == method and (pattern.search(path) or pattern.search(url)):
                if not repeat:
                    self._routes.pop(index)
                return responders[0](call)
        raise AssertionError(f"Geen nep-antwoord voor {method} {url}")

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)

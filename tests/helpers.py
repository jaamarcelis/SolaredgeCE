"""Gedeelde testhulpmiddelen."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import time
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"
SITE_ID = 1000001


def load_fixture(name: str) -> Any:
    """JSON-fixture uit ``tests/fixtures``."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_jwt(expires_in: int = 3600) -> str:
    """Nep-JWT met alleen een ``exp``-claim."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + expires_in}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"

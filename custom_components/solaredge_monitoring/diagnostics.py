"""Diagnostics: het ontdekte site-model en de laatste coordinatordata, met persoonsgegevens verwijderd."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import SolarEdgeConfigEntry
from .const import CONF_TOKENS

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_TOKENS,
    "access_token",
    "refresh_token",
    "id_token",
    "email",
    "address",
    "latitude",
    "longitude",
    "first_name",
    "last_name",
    "zip",
    "link_big_image",
    "link_small_image",
    "big_image_download_link",
    "url",
}


def _plain(value: Any) -> Any:
    """Dataclasses, datums en sets naar JSON-vriendelijke waarden."""
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> dict[str, Any]:
    """Model, opties en coordinatordata."""
    runtime = entry.runtime_data
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "model": _plain(runtime.model),
            "not_applicable": {c.name: sorted(c.not_applicable) for c in runtime.coordinators},
            "data": {c.name: _plain(c.data) for c in runtime.coordinators},
            "history_limits_learned": runtime.history_limits.as_dict() if runtime.history_limits else None,
        },
        TO_REDACT,
    )

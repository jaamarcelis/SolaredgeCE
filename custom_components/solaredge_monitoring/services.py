"""Actie ``solaredge_monitoring.import_energy_history`` en de gedeelde import-routine (ook voor de knop)."""

from __future__ import annotations

from datetime import date, timedelta
import logging
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import SolarEdgeError
from .const import (
    CONF_HISTORY_DAYS,
    CONF_HISTORY_INTERVAL,
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
    HISTORY_INTERVAL_HOUR,
    HISTORY_INTERVALS,
)
from .history import HistoryImportError, ImportResult, Interval, async_import_history

if TYPE_CHECKING:
    from . import SolarEdgeConfigEntry

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_ENERGY_HISTORY = "import_energy_history"
ATTR_START = "start"
ATTR_END = "end"
ATTR_INTERVAL = "interval"

SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_START): cv.date,
        vol.Optional(ATTR_END): cv.date,  # standaard vandaag
        vol.Optional(ATTR_INTERVAL, default=HISTORY_INTERVAL_HOUR): vol.In(HISTORY_INTERVALS),
    }
)

_NOTIFY = {
    "nl": {
        "title": "SolarEdge: historie geïmporteerd",
        "done": "{hourly} uurrijen en {short_term} 5-minutenrijen van {start} tot {end} geïmporteerd in {entity}.",
        "nothing": "Er was niets te importeren voor {entity} in deze periode.",
        "failed_title": "SolarEdge: historie importeren mislukt",
    },
    "en": {
        "title": "SolarEdge: history imported",
        "done": "Imported {hourly} hourly and {short_term} 5-minute rows from {start} to {end} into {entity}.",
        "nothing": "Nothing to import for {entity} in this period.",
        "failed_title": "SolarEdge: history import failed",
    },
}


def _entry_for_device(hass: HomeAssistant, device_id: str) -> SolarEdgeConfigEntry:
    """De geladen config entry waarvan ``device_id`` het site-device is."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None or not any(d == DOMAIN and i.startswith("site_") for d, i in device.identifiers):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_a_site_device")
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == DOMAIN:
            if entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_loaded")
            return entry
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_a_site_device")


async def async_run_import(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry, start: date, end: date, interval: Interval
) -> ImportResult:
    """Import met een lock per config entry; API-fouten worden vertaalbare fouten."""
    runtime = entry.runtime_data
    if runtime.history_lock.locked():
        raise HistoryImportError("import_in_progress")
    async with runtime.history_lock:
        try:
            return await async_import_history(hass, runtime.client, runtime.model, start, end, interval)
        except HistoryImportError:
            raise
        except (SolarEdgeError, aiohttp.ClientError, TimeoutError) as err:
            raise HistoryImportError("fetch_failed", error=str(err) or type(err).__name__) from err


def button_period(entry: SolarEdgeConfigEntry) -> tuple[date, date, Interval]:
    """Periode en interval van de knop uit de opties: ``history_days`` dagen tot en met vandaag."""
    days = int(entry.options.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS))
    today = dt_util.now().date()
    return today - timedelta(days=days), today, entry.options.get(CONF_HISTORY_INTERVAL, HISTORY_INTERVAL_HOUR)


def notify_result(hass: HomeAssistant, entry: SolarEdgeConfigEntry, result: ImportResult | None, error: str | None) -> None:
    """Persistente melding na een import via de knop."""
    texts = _NOTIFY.get(hass.config.language.split("-", 1)[0].lower(), _NOTIFY["en"])
    notification_id = f"{DOMAIN}_history_{entry.entry_id}"
    if error is not None:
        persistent_notification.async_create(hass, error, texts["failed_title"], notification_id)
        return
    assert result is not None
    if not result.hourly_rows and not result.short_term_rows:
        message = texts["nothing"].format(entity=result.statistic_id)
    else:
        message = texts["done"].format(
            hourly=result.hourly_rows,
            short_term=result.short_term_rows,
            start=dt_util.as_local(result.start).strftime("%Y-%m-%d %H:%M"),
            end=dt_util.as_local(result.end).strftime("%Y-%m-%d %H:%M"),
            entity=result.statistic_id,
        )
    persistent_notification.async_create(hass, message, texts["title"], notification_id)


def async_setup_services(hass: HomeAssistant) -> None:
    """Registreer de actie (één keer, in ``async_setup``)."""

    async def import_energy_history(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for_device(hass, call.data[ATTR_DEVICE_ID])
        start: date = call.data[ATTR_START]
        end: date = call.data.get(ATTR_END) or dt_util.now().date()
        if start > end:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_period")
        result = await async_run_import(hass, entry, start, end, call.data[ATTR_INTERVAL])
        return result.as_dict() if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_ENERGY_HISTORY,
        import_energy_history,
        schema=SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

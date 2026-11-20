"""Actie ``solaredge_monitoring.import_energy_history`` en de gedeelde import-routine (ook voor de knop)."""

from __future__ import annotations

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
import voluptuous as vol

from .api import SolarEdgeError
from .const import DOMAIN
from .history import DEFAULT_DAYS, MAX_DAYS, HistoryImportError, ImportResult, async_import_history

if TYPE_CHECKING:
    from . import SolarEdgeConfigEntry

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_ENERGY_HISTORY = "import_energy_history"
ATTR_DAYS = "days"

SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_DAYS, default=DEFAULT_DAYS): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_DAYS)),
    }
)

_NOTIFY = {
    "nl": {
        "title": "SolarEdge: historie geïmporteerd",
        "done": "{hours} uren van {start} tot {end} geïmporteerd in {entity}.",
        "nothing": "Er was niets te importeren voor {entity}: de statistieken gaan al zo ver terug.",
        "failed_title": "SolarEdge: historie importeren mislukt",
    },
    "en": {
        "title": "SolarEdge: history imported",
        "done": "Imported {hours} hours from {start} to {end} into {entity}.",
        "nothing": "Nothing to import for {entity}: the statistics already go back this far.",
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


async def async_run_import(hass: HomeAssistant, entry: SolarEdgeConfigEntry, days: int) -> ImportResult:
    """Import met een lock per config entry; API-fouten worden vertaalbare fouten."""
    runtime = entry.runtime_data
    if runtime.history_lock.locked():
        raise HistoryImportError("import_in_progress")
    async with runtime.history_lock:
        try:
            return await async_import_history(hass, runtime.client, runtime.model, days)
        except HistoryImportError:
            raise
        except (SolarEdgeError, aiohttp.ClientError, TimeoutError) as err:
            raise HistoryImportError("fetch_failed", error=str(err) or type(err).__name__) from err


def notify_result(hass: HomeAssistant, entry: SolarEdgeConfigEntry, result: ImportResult | None, error: str | None) -> None:
    """Persistente melding na een import via de knop."""
    texts = _NOTIFY.get(hass.config.language.split("-", 1)[0].lower(), _NOTIFY["en"])
    notification_id = f"{DOMAIN}_history_{entry.entry_id}"
    if error is not None:
        persistent_notification.async_create(hass, error, texts["failed_title"], notification_id)
        return
    assert result is not None
    if not result.imported_hours:
        message = texts["nothing"].format(entity=result.statistic_id)
    else:
        data = result.as_dict()
        message = texts["done"].format(
            hours=result.imported_hours, start=data["from"], end=data["to"], entity=result.statistic_id
        )
    persistent_notification.async_create(hass, message, texts["title"], notification_id)


def async_setup_services(hass: HomeAssistant) -> None:
    """Registreer de actie (één keer, in ``async_setup``)."""

    async def import_energy_history(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for_device(hass, call.data[ATTR_DEVICE_ID])
        result = await async_run_import(hass, entry, call.data[ATTR_DAYS])
        return result.as_dict() if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_ENERGY_HISTORY,
        import_energy_history,
        schema=SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

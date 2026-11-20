"""Actie ``solaredge_monitoring.import_statistics`` en de gedeelde import-routine (ook voor de knop en de opties)."""

from __future__ import annotations

from datetime import date, timedelta
import logging
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
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
from .statistics_import import (
    HistoryImportError,
    ImportableMetric,
    ImportResult,
    Interval,
    async_import_history,
    importable_metrics,
)

if TYPE_CHECKING:
    from . import SolarEdgeConfigEntry

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_STATISTICS = "import_statistics"
ATTR_START = "start"
ATTR_END = "end"
ATTR_INTERVAL = "interval"

SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,  # leeg = alle sensoren van het device met historie
        vol.Required(ATTR_START): cv.date,
        vol.Optional(ATTR_END): cv.date,  # standaard vandaag
        vol.Optional(ATTR_INTERVAL, default=HISTORY_INTERVAL_HOUR): vol.In(HISTORY_INTERVALS),
    }
)

_NOTIFY = {
    "nl": {
        "title": "SolarEdge: historie geïmporteerd",
        "line": "- {name}: {hourly} uurrijen, {short_term} 5-minutenrijen",
        "done": "Van {start} tot {end}:\n{lines}",
        "failed_title": "SolarEdge: historie importeren mislukt",
    },
    "en": {
        "title": "SolarEdge: history imported",
        "line": "- {name}: {hourly} hourly rows, {short_term} 5-minute rows",
        "done": "From {start} to {end}:\n{lines}",
        "failed_title": "SolarEdge: history import failed",
    },
}


def entry_for_device(hass: HomeAssistant, device_id: str) -> SolarEdgeConfigEntry:
    """De geladen config entry van ``device_id``."""
    device = dr.async_get(hass).async_get(device_id)
    for entry_id in device.config_entries if device else ():
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == DOMAIN:
            if entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_loaded")
            return entry
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_a_solaredge_device")


def select_metrics(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry, device_id: str, entity_ids: list[str] | None
) -> list[ImportableMetric]:
    """De gekozen sensoren van het device (of alle met historie); een sensor zonder historie is een fout."""
    available = importable_metrics(hass, entry.runtime_data.model, device_id)
    if not entity_ids:
        if not available:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_importable_metrics")
        return available
    by_entity = {m.entity_id: m for m in available}
    unknown = [e for e in entity_ids if e not in by_entity]
    if unknown:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="metric_not_importable",
            translation_placeholders={"entity_ids": ", ".join(unknown)},
        )
    return [by_entity[e] for e in entity_ids]


async def async_run_import(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    metrics: list[ImportableMetric],
    start: date,
    end: date,
    interval: Interval,
) -> ImportResult:
    """Import met een lock per config entry; API-fouten worden vertaalbare fouten."""
    runtime = entry.runtime_data
    if runtime.history_lock.locked():
        raise HistoryImportError("import_in_progress")
    async with runtime.history_lock:
        try:
            return await async_import_history(hass, runtime.client, runtime.model, metrics, start, end, interval)
        except HistoryImportError:
            raise
        except (SolarEdgeError, aiohttp.ClientError, TimeoutError) as err:
            raise HistoryImportError("fetch_failed", error=str(err) or type(err).__name__) from err


def button_period(entry: SolarEdgeConfigEntry) -> tuple[date, date, Interval]:
    """Periode en interval van de knop uit de opties: ``history_days`` dagen tot en met vandaag."""
    days = int(entry.options.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS))
    today = dt_util.now().date()
    return today - timedelta(days=days), today, entry.options.get(CONF_HISTORY_INTERVAL, HISTORY_INTERVAL_HOUR)


def summary(hass: HomeAssistant, result: ImportResult) -> str:
    """Leesbare samenvatting voor een melding of de opties-flow."""
    texts = _NOTIFY.get(hass.config.language.split("-", 1)[0].lower(), _NOTIFY["en"])
    lines = "\n".join(
        texts["line"].format(name=m.name, hourly=m.hourly_rows, short_term=m.short_term_rows) for m in result.metrics
    )
    return texts["done"].format(
        start=dt_util.as_local(result.start).strftime("%Y-%m-%d %H:%M"),
        end=dt_util.as_local(result.end).strftime("%Y-%m-%d %H:%M"),
        lines=lines,
    )


def notify_result(hass: HomeAssistant, entry: SolarEdgeConfigEntry, result: ImportResult | None, error: str | None) -> None:
    """Persistente melding na een import via de knop."""
    texts = _NOTIFY.get(hass.config.language.split("-", 1)[0].lower(), _NOTIFY["en"])
    notification_id = f"{DOMAIN}_history_{entry.entry_id}"
    if error is not None or result is None:
        persistent_notification.async_create(hass, error or "", texts["failed_title"], notification_id)
        return
    persistent_notification.async_create(hass, summary(hass, result), texts["title"], notification_id)


def async_setup_services(hass: HomeAssistant) -> None:
    """Registreer de actie (één keer, in ``async_setup``)."""

    async def import_statistics(call: ServiceCall) -> ServiceResponse:
        device_id = call.data[ATTR_DEVICE_ID]
        entry = entry_for_device(hass, device_id)
        start: date = call.data[ATTR_START]
        end: date = call.data.get(ATTR_END) or dt_util.now().date()
        if start > end:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_period")
        metrics = select_metrics(hass, entry, device_id, call.data.get(ATTR_ENTITY_ID))
        result = await async_run_import(hass, entry, metrics, start, end, call.data[ATTR_INTERVAL])
        return result.as_dict() if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_STATISTICS,
        import_statistics,
        schema=SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

"""Knop *Historie importeren* op het site-device."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SolarEdgeEntity, site_device
from .services import async_run_import, button_period, notify_result
from .statistics_import import HistoryImportError, importable_metrics

if TYPE_CHECKING:
    from . import SolarEdgeConfigEntry
    from .coordinator import SolarEdgeRuntimeData


async def async_setup_entry(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Eén knop per site."""
    runtime = entry.runtime_data
    async_add_entities([ImportHistoryButton(entry, runtime)])


class ImportHistoryButton(SolarEdgeEntity, ButtonEntity):
    """Importeert de historie van *Energie totaal* volgens de opties (dagen en interval), op de achtergrond."""

    _attr_translation_key = "import_energy_history"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: SolarEdgeConfigEntry, runtime: SolarEdgeRuntimeData) -> None:
        """Hangt aan de statische coordinator (alleen voor beschikbaarheid)."""
        super().__init__(runtime.static, site_device(runtime.model), f"{runtime.model.site_id}_site_import_energy_history")
        self._entry = entry

    @property
    def available(self) -> bool:
        """Altijd bruikbaar zolang de integratie geladen is."""
        return True

    async def async_press(self) -> None:
        """Start de import; het resultaat volgt als persistente melding."""
        if self._entry.runtime_data.history_lock.locked():
            raise HistoryImportError("import_in_progress")
        self._entry.async_create_background_task(self.hass, self._run(), f"{self.entity_id} import")

    async def _run(self) -> None:
        try:
            metrics = [
                m
                for m in importable_metrics(self.hass, self._entry.runtime_data.model, self.device_entry.id)
                if m.unique_id.endswith("_site_energy_lifetime")
            ]
            if not metrics:
                raise HistoryImportError("no_importable_metrics")
            result = await async_run_import(self.hass, self._entry, metrics, *button_period(self._entry))
        except HomeAssistantError as err:
            notify_result(self.hass, self._entry, None, str(err))
            return
        notify_result(self.hass, self._entry, result, None)

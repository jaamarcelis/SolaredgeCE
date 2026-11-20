"""Knop *Historie importeren* op het site-device."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SolarEdgeEntity, site_device
from .history import DEFAULT_DAYS, HistoryImportError
from .services import async_run_import, notify_result

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
    """Importeert de laatste 90 dagen van *Energie totaal* in de statistieken (op de achtergrond)."""

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
            result = await async_run_import(self.hass, self._entry, DEFAULT_DAYS)
        except HomeAssistantError as err:
            notify_result(self.hass, self._entry, None, str(err))
            return
        notify_result(self.hass, self._entry, result, None)

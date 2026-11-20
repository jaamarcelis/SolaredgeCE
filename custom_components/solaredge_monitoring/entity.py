"""Devices en de basis-entity.

Alle devices worden centraal en in volgorde geregistreerd door :func:`async_register_devices`:
site → omvormer(s) → optimizer(s), en batterijen onder de site. De koppeling aan het bovenliggende device gaat
via ``via_device_id`` (de registry-ID, sinds HA 2026.10; op oudere versies via ``via_device``).
Entities verwijzen daarna alleen nog via ``identifiers`` naar hun device.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.const import PORTAL_SITE_URL
from .api.models import StorageDevice
from .const import DOMAIN, MANUFACTURER
from .coordinator import SolarEdgeCoordinator
from .discovery import InverterModel, OptimizerModel, SiteModel

# HA 2026.10+: koppelen via de registry-ID van het bovenliggende device
_HAS_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__


def site_identifier(model: SiteModel) -> tuple[str, str]:
    """Identifier van het site-device."""
    return (DOMAIN, f"site_{model.site_id}")


def battery_identifier(model: SiteModel, battery: StorageDevice, index: int) -> tuple[str, str]:
    """Identifier van een batterij (serienummer, of een vaste vervanging als dat ontbreekt)."""
    return (DOMAIN, battery.serial_number or f"battery_{model.site_id}_{index}")


def site_device(model: SiteModel) -> DeviceInfo:
    """Verwijzing naar het site-device."""
    return DeviceInfo(identifiers={site_identifier(model)})


def inverter_device(model: SiteModel, inverter: InverterModel) -> DeviceInfo:
    """Verwijzing naar een omvormer-device."""
    return DeviceInfo(identifiers={(DOMAIN, inverter.serial)})


def optimizer_device(model: SiteModel, optimizer: OptimizerModel) -> DeviceInfo:
    """Verwijzing naar een optimizer-device."""
    return DeviceInfo(identifiers={(DOMAIN, optimizer.serial)})


def battery_device(model: SiteModel, battery: StorageDevice, index: int) -> DeviceInfo:
    """Verwijzing naar een batterij-device."""
    return DeviceInfo(identifiers={battery_identifier(model, battery, index)})


def async_register_devices(hass: HomeAssistant, entry: ConfigEntry, model: SiteModel, include_optimizers: bool) -> None:
    """Registreer alle devices met hun eigenschappen, ouders eerst."""
    registry = dr.async_get(hass)
    portal = PORTAL_SITE_URL.format(site_id=model.site_id)

    def register(parent: dr.DeviceEntry | None, parent_identifier: tuple[str, str] | None, **info: Any) -> dr.DeviceEntry:
        if parent is not None:
            if _HAS_VIA_DEVICE_ID:
                info["via_device_id"] = parent.id
            else:
                info["via_device"] = parent_identifier
        return registry.async_get_or_create(config_entry_id=entry.entry_id, **info)

    peak = f" ({model.peak_power_kw:g} kWp)" if model.peak_power_kw else ""
    site = register(
        None,
        None,
        identifiers={site_identifier(model)},
        name=model.name,
        manufacturer=MANUFACTURER,
        model=f"{(model.components.site_type or 'PV').capitalize()} site{peak}",
        serial_number=str(model.site_id),
        configuration_url=portal,
    )

    inverters: dict[str, dr.DeviceEntry] = {}
    for inverter in model.inverters:
        details = inverter.details
        inverters[inverter.serial] = register(
            site,
            site_identifier(model),
            identifiers={(DOMAIN, inverter.serial)},
            name=inverter.name,
            manufacturer=MANUFACTURER,
            model=inverter.model or (details.part_number if details else None),
            serial_number=inverter.serial,
            sw_version=inverter.sw_version,
            hw_version=details.part_number if details else None,
            configuration_url=portal,
        )

    if include_optimizers:
        for optimizer in model.optimizers:
            module = optimizer.info.modules[0] if optimizer.info and optimizer.info.modules else None
            parent = inverters.get(optimizer.inverter_serial or "")
            register(
                parent or site,
                (DOMAIN, optimizer.inverter_serial) if parent else site_identifier(model),
                identifiers={(DOMAIN, optimizer.serial)},
                name=optimizer.name,
                manufacturer=MANUFACTURER,
                model=optimizer.model,
                model_id=module.model if module else None,
                serial_number=optimizer.serial,
            )

    if model.has_storage:
        batteries = model.batteries or [StorageDevice()]
        for index, battery in enumerate(batteries):
            capacity = f" {battery.capacity:g} kWh" if battery.capacity else ""
            register(
                site,
                site_identifier(model),
                identifiers={battery_identifier(model, battery, index)},
                name=f"Batterij {index + 1}" if len(batteries) > 1 else "Batterij",
                manufacturer=battery.manufacturer or MANUFACTURER,
                model=f"{battery.part_number or 'Batterij'}{capacity}",
                serial_number=battery.serial_number,
                sw_version=battery.software_version,
            )


class SolarEdgeEntity(CoordinatorEntity[SolarEdgeCoordinator]):
    """Basis voor alle entities: naam uit device + entity, unieke ID per site en apparaat."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarEdgeCoordinator, device: DeviceInfo, unique_id: str) -> None:
        """Koppel aan ``device`` (al geregistreerd) met ``unique_id``."""
        super().__init__(coordinator)
        self._attr_device_info = device
        self._attr_unique_id = unique_id

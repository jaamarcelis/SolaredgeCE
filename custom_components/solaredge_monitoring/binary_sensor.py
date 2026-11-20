"""Binaire sensoren: communicatie, vermogensbeperking en open alerts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SolarEdgeConfigEntry
from .coordinator import SolarEdgeCoordinator
from .entity import SolarEdgeEntity, site_device


@dataclass(frozen=True, kw_only=True)
class SolarEdgeBinaryDescription(BinarySensorEntityDescription):
    """Beschrijving van een binaire sensor."""

    coordinator: str
    is_on_fn: Callable[[dict[str, Any]], bool | None]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None


DESCRIPTIONS = (
    SolarEdgeBinaryDescription(
        key="communicating",
        translation_key="communicating",
        coordinator="live",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d["power_flow"].is_communicating if d.get("power_flow") else None,
    ),
    SolarEdgeBinaryDescription(
        key="power_limited",
        translation_key="power_limited",
        coordinator="energy",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: d["curtailment"].power_limited if d.get("curtailment") else None,
        attrs_fn=lambda d: {"reasons": d["curtailment"].limit_reasons} if d.get("curtailment") else None,
    ),
    SolarEdgeBinaryDescription(
        key="alerts",
        translation_key="alerts",
        coordinator="info",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: (d["alerts_total"] or 0) > 0 if d.get("alerts_total") is not None else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Maak de binaire sensoren van de site."""
    runtime = entry.runtime_data
    device = site_device(runtime.model)
    coordinators = {"live": runtime.live, "energy": runtime.energy, "info": runtime.info}
    async_add_entities(
        SolarEdgeBinarySensor(coordinators[d.coordinator], d, device, f"{runtime.model.site_id}_site_{d.key}")
        for d in DESCRIPTIONS
    )


class SolarEdgeBinarySensor(SolarEdgeEntity, BinarySensorEntity):
    """Binaire sensor op basis van coordinatordata."""

    entity_description: SolarEdgeBinaryDescription

    def __init__(
        self, coordinator: SolarEdgeCoordinator, description: SolarEdgeBinaryDescription, device: DeviceInfo, unique_id: str
    ) -> None:
        """Koppel de beschrijving."""
        super().__init__(coordinator, device, unique_id)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Aan/uit uit de coordinatordata."""
        return self.entity_description.is_on_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra eigenschappen."""
        return self.entity_description.attrs_fn(self.coordinator.data or {}) if self.entity_description.attrs_fn else None

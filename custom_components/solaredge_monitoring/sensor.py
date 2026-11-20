"""Sensoren: per apparaat (site, omvormer, optimizer, batterij) een vaste set plus dynamische telemetrie.

Elke sensor wordt beschreven door een :class:`SolarEdgeSensorDescription` met de coordinator waar de data uit
komt en een ``value_fn`` die de waarde uit die data haalt. De omvormertelemetrie wordt volledig afgeleid uit de
metric-metadata van de API (zie ``metrics.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfMass,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import SolarEdgeConfigEntry
from .api.models import PowerFlowNode, StorageDevice
from .coordinator import SolarEdgeCoordinator, SolarEdgeRuntimeData
from .discovery import InverterModel, OptimizerModel, SiteModel
from .entity import SolarEdgeEntity, battery_device, inverter_device, optimizer_device, site_device
from .metrics import MetricSpec

ValueFn = Callable[[dict[str, Any]], StateType | datetime]
AttrsFn = Callable[[dict[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True, kw_only=True)
class SolarEdgeSensorDescription(SensorEntityDescription):
    """Beschrijving van een sensor: waar de data vandaan komt en hoe de waarde gelezen wordt."""

    coordinator: str  # live / energy / telemetry / panels / info / static
    value_fn: ValueFn
    attrs_fn: AttrsFn | None = None


def _get(obj: Any, *path: str) -> Any:
    """Veilige pad-lookup: ``_get(data, "power_flow", "grid", "current_power")``."""
    for part in path:
        if obj is None:
            return None
        obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part, None)
    return obj


def _pct(value: float | None) -> float | None:
    """Fractie (0–1) naar procent."""
    return None if value is None else round(value * 100, 1)


def _deg(value: float | None) -> float | None:
    """Radialen naar graden."""
    return None if value is None else round(math.degrees(value), 1)


# --- Site --------------------------------------------------------------------------------------


def _site_sensors(model: SiteModel) -> list[SolarEdgeSensorDescription]:
    power = SensorDeviceClass.POWER
    energy = SensorDeviceClass.ENERGY
    m = SensorStateClass.MEASUREMENT
    total = SensorStateClass.TOTAL_INCREASING
    kwh = UnitOfEnergy.KILO_WATT_HOUR

    sensors = [
        SolarEdgeSensorDescription(
            key="current_power",
            translation_key="current_power",
            coordinator="live",
            device_class=power,
            state_class=m,
            native_unit_of_measurement=UnitOfPower.KILO_WATT,
            suggested_unit_of_measurement=UnitOfPower.WATT,
            value_fn=lambda d: _get(d, "power_flow", "solar_production", "current_power"),
        ),
        SolarEdgeSensorDescription(
            key="ac_power",
            translation_key="ac_power",
            coordinator="live",
            entity_registry_enabled_default=False,
            device_class=power,
            state_class=m,
            native_unit_of_measurement=UnitOfPower.WATT,
            value_fn=lambda d: _get(d, "live_power", "current_ac_power"),
            attrs_fn=lambda d: {"max_ac_power": _get(d, "live_power", "max_ac_power")},
        ),
        SolarEdgeSensorDescription(
            key="last_update",
            translation_key="last_update",
            coordinator="live",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda d: _get(d, "power_flow", "last_update_time"),
        ),
        SolarEdgeSensorDescription(
            key="energy_today",
            translation_key="energy_today",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=kwh,
            value_fn=lambda d: _get(d, "measurements", "energy_today"),
        ),
        SolarEdgeSensorDescription(
            key="energy_this_month",
            translation_key="energy_this_month",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=kwh,
            value_fn=lambda d: _get(d, "measurements", "energy_monthly"),
        ),
        SolarEdgeSensorDescription(
            key="energy_this_year",
            translation_key="energy_this_year",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=kwh,
            value_fn=lambda d: _get(d, "measurements", "energy_yearly"),
        ),
        SolarEdgeSensorDescription(
            key="energy_lifetime",
            translation_key="energy_lifetime",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=kwh,
            value_fn=lambda d: _get(d, "measurements", "energy_life_time"),
            attrs_fn=lambda d: {"last_reporting_time": _get(d, "measurements", "last_reporting_time")},
        ),
        SolarEdgeSensorDescription(
            key="yield_today",
            translation_key="yield_today",
            coordinator="energy",
            state_class=m,
            native_unit_of_measurement="kWh/kWp",
            suggested_display_precision=2,
            value_fn=lambda d: _get(d, "energy_today", "summary", "yield_"),
        ),
        SolarEdgeSensorDescription(
            key="yield_this_month",
            translation_key="yield_this_month",
            coordinator="energy",
            state_class=m,
            native_unit_of_measurement="kWh/kWp",
            suggested_display_precision=1,
            value_fn=lambda d: _get(d, "measurements", "monthly_yield"),
        ),
        SolarEdgeSensorDescription(
            key="yield_this_year",
            translation_key="yield_this_year",
            coordinator="energy",
            state_class=m,
            native_unit_of_measurement="kWh/kWp",
            suggested_display_precision=0,
            value_fn=lambda d: _get(d, "measurements", "yearly_yield"),
        ),
        SolarEdgeSensorDescription(
            key="peak_power",
            translation_key="peak_power",
            coordinator="static",
            entity_category=EntityCategory.DIAGNOSTIC,
            native_unit_of_measurement="kWp",
            value_fn=lambda d: _get(d, "model").peak_power_kw if _get(d, "model") else None,
        ),
        SolarEdgeSensorDescription(
            key="open_alerts",
            translation_key="open_alerts",
            coordinator="info",
            state_class=m,
            value_fn=lambda d: _get(d, "alerts_total"),
            attrs_fn=lambda d: {
                "alerts": [
                    {
                        "type": a.alert_type,
                        "impact": a.impact,
                        "component": a.component_type,
                        "serial": a.serial_number,
                        "since": a.creation_date,
                    }
                    for a in (_get(d, "alerts", "alerts") or [])
                ]
            },
        ),
        SolarEdgeSensorDescription(
            key="co2_saved",
            translation_key="co2_saved",
            coordinator="info",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.WEIGHT,
            state_class=total,
            native_unit_of_measurement=UnitOfMass.KILOGRAMS,
            value_fn=lambda d: _get(d, "environmental", "lifetime_co2_emission_saved"),
        ),
        SolarEdgeSensorDescription(
            key="trees_planted",
            translation_key="trees_planted",
            coordinator="info",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=total,
            suggested_display_precision=0,
            value_fn=lambda d: _get(d, "environmental", "lifetime_equivalent_trees_planted"),
        ),
        SolarEdgeSensorDescription(
            key="sunrise",
            translation_key="sunrise",
            coordinator="info",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_registry_enabled_default=False,
            value_fn=lambda d: _get(d, "weather", "sun_time", "sunrise"),
        ),
        SolarEdgeSensorDescription(
            key="sunset",
            translation_key="sunset",
            coordinator="info",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_registry_enabled_default=False,
            value_fn=lambda d: _get(d, "weather", "sun_time", "sunset"),
        ),
    ]

    if model.components.has_performance_ratio:
        sensors.append(
            SolarEdgeSensorDescription(
                key="performance_ratio",
                translation_key="performance_ratio",
                coordinator="energy",
                state_class=m,
                native_unit_of_measurement=PERCENTAGE,
                value_fn=lambda d: _pct(_get(d, "energy_today", "summary", "performance_ratio")),
            )
        )

    if model.has_meter:
        sensors += _meter_sensors()
    return sensors


def _grid_power(node: PowerFlowNode | None, direction: str) -> float | None:
    """Netvermogen in één richting (kW); 0 als de stroom de andere kant op gaat."""
    if node is None or node.current_power is None:
        return None
    return node.current_power if node.status == direction else 0.0


def _meter_sensors() -> list[SolarEdgeSensorDescription]:
    """Alleen bij een consumptie-/netmeter (``components.hasConsumptionAndGrid``)."""
    power = SensorDeviceClass.POWER
    energy = SensorDeviceClass.ENERGY
    m = SensorStateClass.MEASUREMENT
    total = SensorStateClass.TOTAL_INCREASING
    kw = UnitOfPower.KILO_WATT
    wh = UnitOfEnergy.WATT_HOUR
    return [
        SolarEdgeSensorDescription(
            key="consumption_power",
            translation_key="consumption_power",
            coordinator="live",
            device_class=power,
            state_class=m,
            native_unit_of_measurement=kw,
            suggested_unit_of_measurement=UnitOfPower.WATT,
            value_fn=lambda d: _get(d, "power_flow", "consumption", "current_power"),
        ),
        SolarEdgeSensorDescription(
            key="grid_import_power",
            translation_key="grid_import_power",
            coordinator="live",
            device_class=power,
            state_class=m,
            native_unit_of_measurement=kw,
            suggested_unit_of_measurement=UnitOfPower.WATT,
            value_fn=lambda d: _grid_power(_get(d, "power_flow", "grid"), "import"),
        ),
        SolarEdgeSensorDescription(
            key="grid_export_power",
            translation_key="grid_export_power",
            coordinator="live",
            device_class=power,
            state_class=m,
            native_unit_of_measurement=kw,
            suggested_unit_of_measurement=UnitOfPower.WATT,
            value_fn=lambda d: _grid_power(_get(d, "power_flow", "grid"), "export"),
        ),
        SolarEdgeSensorDescription(
            key="grid_status",
            translation_key="grid_status",
            coordinator="live",
            device_class=SensorDeviceClass.ENUM,
            options=["import", "export", "grid-outage", "idle"],
            value_fn=lambda d: _get(d, "power_flow", "grid", "status"),
        ),
        SolarEdgeSensorDescription(
            key="consumption_today",
            translation_key="consumption_today",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=wh,
            value_fn=lambda d: _get(d, "energy_today", "summary", "consumption"),
        ),
        SolarEdgeSensorDescription(
            key="import_today",
            translation_key="import_today",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=wh,
            value_fn=lambda d: (
                _get(d, "energy_today", "summary", "import_")
                if _get(d, "energy_today", "summary", "import_") is not None
                else _get(d, "energy_today", "summary", "consumption_distribution", "consumption_from_grid")
            ),
        ),
        SolarEdgeSensorDescription(
            key="export_today",
            translation_key="export_today",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=wh,
            value_fn=lambda d: (
                _get(d, "energy_today", "summary", "export")
                if _get(d, "energy_today", "summary", "export") is not None
                else _get(d, "energy_today", "summary", "production_distribution", "production_to_grid")
            ),
        ),
        SolarEdgeSensorDescription(
            key="self_consumption_today",
            translation_key="self_consumption_today",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=wh,
            value_fn=lambda d: _get(d, "energy_today", "summary", "production_distribution", "production_to_home"),
        ),
        SolarEdgeSensorDescription(
            key="self_consumption_ratio",
            translation_key="self_consumption_ratio",
            coordinator="energy",
            state_class=m,
            native_unit_of_measurement=PERCENTAGE,
            value_fn=lambda d: _pct(_get(d, "energy_today", "summary", "self_consumption_ratio")),
        ),
        SolarEdgeSensorDescription(
            key="self_sufficiency_ratio",
            translation_key="self_sufficiency_ratio",
            coordinator="energy",
            state_class=m,
            native_unit_of_measurement=PERCENTAGE,
            value_fn=lambda d: _pct(_get(d, "energy_today", "summary", "self_sufficiency_ratio")),
        ),
    ]


# --- Omvormer ----------------------------------------------------------------------------------


def _inverter_power(data: dict[str, Any], serial: str) -> float | None:
    """Laatste niet-null vermogen (W) van de omvormer uit ``inverters/power``."""
    power = data.get("inverters_power")
    if not power:
        return None
    serials = [i.inverter_serial for i in power.inverter_data_array]
    index = serials.index(serial) if serial in serials else 0
    for point in reversed(power.inverters_dated_power_list):
        if index < len(point.inverter_power_array) and point.inverter_power_array[index] is not None:
            return point.inverter_power_array[index]
    return None


def _inverter_sensors(inverter: InverterModel) -> list[SolarEdgeSensorDescription]:
    serial = inverter.serial
    return [
        SolarEdgeSensorDescription(
            key="power",
            translation_key="inverter_power",
            coordinator="energy",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.WATT,
            value_fn=lambda d: _inverter_power(d, serial),
        ),
        SolarEdgeSensorDescription(
            key="energy_today",
            translation_key="energy_today",
            coordinator="energy",
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            value_fn=lambda d: next(
                (
                    i.inverter_energy
                    for i in (_get(d, "inverters_energy", "inverter_energy_list") or [])
                    if i.inverter_serial == serial
                ),
                None,
            ),
        ),
        SolarEdgeSensorDescription(
            key="communication",
            translation_key="communication",
            coordinator="static",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda d: (
                next((i.details.communication for i in _get(d, "model").inverters if i.serial == serial and i.details), None)
                if _get(d, "model")
                else None
            ),
        ),
    ]


def _telemetry_sensor(inverter: InverterModel, spec: MetricSpec) -> SolarEdgeSensorDescription:
    """Een dynamisch ontdekte metric, met eigenschappen uit de metric-metadata."""
    identifier = inverter.identifier

    def value(d: dict[str, Any]) -> StateType:
        item = _get(d, "telemetry").values.get(identifier, {}).get(spec.key) if _get(d, "telemetry") else None
        return None if item is None else item.value

    def attrs(d: dict[str, Any]) -> dict[str, Any] | None:
        item = _get(d, "telemetry").values.get(identifier, {}).get(spec.key) if _get(d, "telemetry") else None
        return {"measured_at": item.measured_at if item else None, "metric": spec.key}

    return SolarEdgeSensorDescription(
        key=f"metric_{spec.key}",
        name=spec.name,
        coordinator="telemetry",
        device_class=spec.device_class,
        state_class=spec.state_class,
        native_unit_of_measurement=spec.unit,
        entity_registry_enabled_default=spec.enabled_default,
        value_fn=value,
        attrs_fn=attrs,
    )


# --- Optimizer ---------------------------------------------------------------------------------


def _optimizer_static(d: dict[str, Any], serial: str) -> OptimizerModel | None:
    """De optimizer uit het (dagelijks ververste) site-model van de static coordinator."""
    model: SiteModel | None = d.get("model")
    return next((o for o in model.optimizers if o.serial == serial), None) if model else None


def _panel(d: dict[str, Any], serial: str, attr: str) -> Any:
    """Eigenschap van het (eerste) paneel aan de optimizer, bijv. ``model`` of ``stc_power``."""
    optimizer = _optimizer_static(d, serial)
    module = optimizer.info.modules[0] if optimizer and optimizer.info and optimizer.info.modules else None
    return getattr(module, attr, None)


# 16-puntskompas per taal: (afkorting, volledige naam), beginnend bij noord, met de klok mee
_COMPASS: dict[str, tuple[tuple[str, str], ...]] = {
    "en": (
        ("N", "North"), ("NNE", "North-northeast"), ("NE", "Northeast"), ("ENE", "East-northeast"),
        ("E", "East"), ("ESE", "East-southeast"), ("SE", "Southeast"), ("SSE", "South-southeast"),
        ("S", "South"), ("SSW", "South-southwest"), ("SW", "Southwest"), ("WSW", "West-southwest"),
        ("W", "West"), ("WNW", "West-northwest"), ("NW", "Northwest"), ("NNW", "North-northwest"),
    ),
    "nl": (
        ("N", "Noord"), ("NNO", "Noordnoordoost"), ("NO", "Noordoost"), ("ONO", "Oostnoordoost"),
        ("O", "Oost"), ("OZO", "Oostzuidoost"), ("ZO", "Zuidoost"), ("ZZO", "Zuidzuidoost"),
        ("Z", "Zuid"), ("ZZW", "Zuidzuidwest"), ("ZW", "Zuidwest"), ("WZW", "Westzuidwest"),
        ("W", "West"), ("WNW", "Westnoordwest"), ("NW", "Noordwest"), ("NNW", "Noordnoordwest"),
    ),
}  # fmt: skip


def _compass(degrees: float, language: str) -> tuple[str, str]:
    points = _COMPASS.get(language.split("-", maxsplit=1)[0].lower(), _COMPASS["en"])
    return points[round((degrees % 360) / 22.5) % 16]


def compass_point(degrees: float | None, language: str = "en") -> str | None:
    """Afkorting van de windrichting (16-puntskompas; 0° = noord, 90° = oost, 180° = zuid), bijv. ``"ZZW"``."""
    return None if degrees is None else _compass(degrees, language)[0]


def panel_direction(degrees: float | None, language: str = "en") -> str | None:
    """Ligging van het paneel als uitgeschreven windrichting met de afkorting: ``"Zuidzuidwest (ZZW)"``."""
    if degrees is None:
        return None
    abbreviation, name = _compass(degrees, language)
    return f"{name} ({abbreviation})"


def _optimizer_diagnostics(serial: str, language: str) -> list[SolarEdgeSensorDescription]:
    """Vaste eigenschappen van optimizer en paneel als diagnostic-entities (uit de lay-out en optimizer-info)."""
    diagnostic = EntityCategory.DIAGNOSTIC
    return [
        SolarEdgeSensorDescription(
            key="position",
            translation_key="position",
            coordinator="static",
            entity_category=diagnostic,
            value_fn=lambda d: getattr(_optimizer_static(d, serial), "position", None),
        ),
        SolarEdgeSensorDescription(
            key="panel_manufacturer",
            translation_key="panel_manufacturer",
            coordinator="static",
            entity_category=diagnostic,
            value_fn=lambda d: _panel(d, serial, "manufacturer"),
        ),
        SolarEdgeSensorDescription(
            key="panel_model",
            translation_key="panel_model",
            coordinator="static",
            entity_category=diagnostic,
            value_fn=lambda d: _panel(d, serial, "model"),
        ),
        SolarEdgeSensorDescription(
            key="panel_stc_power",
            translation_key="panel_stc_power",
            coordinator="static",
            entity_category=diagnostic,
            device_class=SensorDeviceClass.POWER,
            native_unit_of_measurement=UnitOfPower.WATT,
            suggested_display_precision=0,
            value_fn=lambda d: _panel(d, serial, "stc_power"),
        ),
        SolarEdgeSensorDescription(
            key="panel_orientation",
            translation_key="panel_orientation",
            coordinator="static",
            entity_category=diagnostic,
            device_class=SensorDeviceClass.ENUM,
            options=["landscape", "portrait"],
            value_fn=lambda d: (_panel(d, serial, "orientation") or "").lower() or None,
        ),
        SolarEdgeSensorDescription(
            key="panel_tilt",
            translation_key="panel_tilt",
            coordinator="static",
            entity_category=diagnostic,
            native_unit_of_measurement=DEGREE,
            suggested_display_precision=1,
            value_fn=lambda d: _deg(_panel(d, serial, "tilt")),
        ),
        SolarEdgeSensorDescription(
            key="panel_azimuth",
            translation_key="panel_azimuth",
            coordinator="static",
            entity_category=diagnostic,
            native_unit_of_measurement=DEGREE,
            suggested_display_precision=0,
            value_fn=lambda d: _deg(_panel(d, serial, "azimuth")),
        ),
        SolarEdgeSensorDescription(
            key="panel_direction",
            translation_key="panel_direction",
            coordinator="static",
            entity_category=diagnostic,
            # Uitgeschreven windrichting met afkorting, bijv. "Zuid (Z)", afgeleid van de azimut
            value_fn=lambda d: panel_direction(_deg(_panel(d, serial, "azimuth")), language),
        ),
    ]


def _optimizer_sensors(optimizer: OptimizerModel) -> list[SolarEdgeSensorDescription]:
    serial = optimizer.serial

    def live(d: dict[str, Any], attr: str) -> Any:
        return getattr(d.get("live", {}).get(serial), attr, None)

    def live_attrs(d: dict[str, Any]) -> dict[str, Any]:
        return {"last_measurement": live(d, "last_measurement")}

    m = SensorStateClass.MEASUREMENT
    return [
        SolarEdgeSensorDescription(
            key="energy_today",
            translation_key="energy_today",
            coordinator="panels",
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            value_fn=lambda d: _get(d.get("energy", {}).get(serial), "energy", "value"),
        ),
        SolarEdgeSensorDescription(
            key="relative_output",
            translation_key="relative_output",
            coordinator="panels",
            state_class=m,
            native_unit_of_measurement=PERCENTAGE,
            value_fn=lambda d: _pct(_get(d.get("energy", {}).get(serial), "color")),
        ),
        SolarEdgeSensorDescription(
            key="power",
            translation_key="optimizer_power",
            coordinator="panels",
            device_class=SensorDeviceClass.POWER,
            state_class=m,
            native_unit_of_measurement=UnitOfPower.WATT,
            suggested_display_precision=1,
            value_fn=lambda d: live(d, "power"),
            attrs_fn=live_attrs,
        ),
        SolarEdgeSensorDescription(
            key="panel_voltage",
            translation_key="panel_voltage",
            coordinator="panels",
            entity_registry_enabled_default=False,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=m,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            value_fn=lambda d: live(d, "voltage"),
            attrs_fn=live_attrs,
        ),
        SolarEdgeSensorDescription(
            key="output_voltage",
            translation_key="output_voltage",
            coordinator="panels",
            entity_registry_enabled_default=False,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=m,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            value_fn=lambda d: live(d, "optimizer_voltage"),
            attrs_fn=live_attrs,
        ),
        SolarEdgeSensorDescription(
            key="current",
            translation_key="optimizer_current",
            coordinator="panels",
            entity_registry_enabled_default=False,
            device_class=SensorDeviceClass.CURRENT,
            state_class=m,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=2,
            value_fn=lambda d: live(d, "current"),
            attrs_fn=live_attrs,
        ),
    ]


# --- Batterij ----------------------------------------------------------------------------------


def _battery_sensors() -> list[SolarEdgeSensorDescription]:
    """Gegevens uit de energiestroom en de batterijverdeling (gelden voor de batterij(en) samen)."""
    wh = UnitOfEnergy.WATT_HOUR
    total = SensorStateClass.TOTAL_INCREASING
    energy = SensorDeviceClass.ENERGY
    return [
        SolarEdgeSensorDescription(
            key="state_of_charge",
            translation_key="state_of_charge",
            coordinator="live",
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            value_fn=lambda d: (
                _get(d, "power_flow").storage.charge_level if _get(d, "power_flow") and _get(d, "power_flow").storage else None
            ),
        ),
        SolarEdgeSensorDescription(
            key="battery_power",
            translation_key="battery_power",
            coordinator="live",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.KILO_WATT,
            suggested_unit_of_measurement=UnitOfPower.WATT,
            # Positief = laden, negatief = ontladen
            value_fn=lambda d: _signed_battery_power(_get(d, "power_flow").storage if _get(d, "power_flow") else None),
        ),
        SolarEdgeSensorDescription(
            key="battery_status",
            translation_key="battery_status",
            coordinator="live",
            device_class=SensorDeviceClass.ENUM,
            options=["charging", "discharging", "idle"],
            value_fn=lambda d: (
                _get(d, "power_flow").storage.status if _get(d, "power_flow") and _get(d, "power_flow").storage else None
            ),
        ),
        SolarEdgeSensorDescription(
            key="charged_from_pv_today",
            translation_key="charged_from_pv_today",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=wh,
            value_fn=lambda d: _get(d, "storage_distribution", "sources", "pv"),
        ),
        SolarEdgeSensorDescription(
            key="charged_from_grid_today",
            translation_key="charged_from_grid_today",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=wh,
            value_fn=lambda d: _get(d, "storage_distribution", "sources", "grid"),
        ),
        SolarEdgeSensorDescription(
            key="discharged_to_home_today",
            translation_key="discharged_to_home_today",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=wh,
            value_fn=lambda d: _get(d, "storage_distribution", "destinations", "building"),
        ),
        SolarEdgeSensorDescription(
            key="discharged_to_grid_today",
            translation_key="discharged_to_grid_today",
            coordinator="energy",
            device_class=energy,
            state_class=total,
            native_unit_of_measurement=wh,
            value_fn=lambda d: _get(d, "storage_distribution", "destinations", "grid"),
        ),
    ]


def _signed_battery_power(node: PowerFlowNode | None) -> float | None:
    if node is None or node.current_power is None:
        return None
    return -abs(node.current_power) if node.status == "discharging" else abs(node.current_power)


# --- Opbouw ------------------------------------------------------------------------------------


def build_sensors(runtime: SolarEdgeRuntimeData, language: str = "en") -> list[SolarEdgeSensor]:
    """Alle sensoren voor de site, volledig afgeleid uit het ontdekte :class:`SiteModel`.

    ``language`` (de taal van Home Assistant) bepaalt de windrichting-afkortingen van de paneelligging.
    """
    model = runtime.model
    site_id = model.site_id
    coordinators: dict[str, SolarEdgeCoordinator | None] = {
        "static": runtime.static,
        "live": runtime.live,
        "energy": runtime.energy,
        "telemetry": runtime.telemetry,
        "panels": runtime.panels,
        "info": runtime.info,
    }
    entities: list[SolarEdgeSensor] = []

    def add(device: DeviceInfo, device_key: str, descriptions: list[SolarEdgeSensorDescription]) -> None:
        for description in descriptions:
            coordinator = coordinators[description.coordinator]
            if coordinator is not None:
                entities.append(SolarEdgeSensor(coordinator, description, device, f"{site_id}_{device_key}_{description.key}"))

    add(site_device(model), "site", _site_sensors(model))

    for inverter in model.inverters:
        device = inverter_device(model, inverter)
        add(device, inverter.serial, _inverter_sensors(inverter))
        add(
            device,
            inverter.serial,
            [_telemetry_sensor(inverter, spec) for spec in runtime.telemetry.active_metrics(inverter.analysis_type)],
        )

    if runtime.panels is not None:
        for optimizer in model.optimizers:
            device = optimizer_device(model, optimizer)
            add(device, optimizer.serial, _optimizer_sensors(optimizer))
            add(device, optimizer.serial, _optimizer_diagnostics(optimizer.serial, language))

    if model.has_storage:
        batteries = model.batteries or [StorageDevice()]
        add(battery_device(model, batteries[0], 0), batteries[0].serial_number or "battery", _battery_sensors())

    return entities


async def async_setup_entry(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Maak alle sensoren aan."""
    async_add_entities(build_sensors(entry.runtime_data, hass.config.language))


class SolarEdgeSensor(SolarEdgeEntity, SensorEntity):
    """Een sensor waarvan de waarde via ``value_fn`` uit de data van de coordinator komt."""

    entity_description: SolarEdgeSensorDescription

    def __init__(
        self, coordinator: SolarEdgeCoordinator, description: SolarEdgeSensorDescription, device: DeviceInfo, unique_id: str
    ) -> None:
        """Koppel de beschrijving aan coordinator en device."""
        super().__init__(coordinator, device, unique_id)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        """De waarde uit de coordinatordata."""
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra eigenschappen (bijv. meetmoment, paneelgegevens)."""
        if self.entity_description.attrs_fn is None:
            return None
        attrs = self.entity_description.attrs_fn(self.coordinator.data or {})
        return {k: v for k, v in attrs.items() if v is not None} if attrs else None

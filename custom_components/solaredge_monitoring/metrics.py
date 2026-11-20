"""Vertaalt de metric-metadata van de API dynamisch naar sensor-eigenschappen.

De API beschrijft per apparaattype welke metrics er zijn (``…/device-types/{type}/metrics/supported``), met
eenheid (``units``), soort (``kind``: GAUGE/DELTA/CUMULATIVE) en datatype. Daaruit volgen ``device_class``,
``native_unit_of_measurement`` en ``state_class``, zonder hardcoded lijst. Nieuwe metrics van SolarEdge
verschijnen zo automatisch als (standaard uitgeschakelde) entities.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
)

from .api.models import MetricMeta
from .const import CORE_METRICS

# Eenheid uit de API → (device_class, HA-eenheid)
UNIT_MAP: dict[str, tuple[SensorDeviceClass | None, str | None]] = {
    "W": (SensorDeviceClass.POWER, UnitOfPower.WATT),
    "kW": (SensorDeviceClass.POWER, UnitOfPower.KILO_WATT),
    "Wh": (SensorDeviceClass.ENERGY, UnitOfEnergy.WATT_HOUR),
    "kWh": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
    "V": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    "A": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    "Hz": (SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ),
    "C": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "VA": (SensorDeviceClass.APPARENT_POWER, UnitOfApparentPower.VOLT_AMPERE),
    "VAr": (SensorDeviceClass.REACTIVE_POWER, UnitOfReactivePower.VOLT_AMPERE_REACTIVE),
    "s": (SensorDeviceClass.DURATION, UnitOfTime.SECONDS),
    "W/m^2": (SensorDeviceClass.IRRADIANCE, "W/m²"),
    "%": (None, PERCENTAGE),
}


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Hoe één API-metric als sensor verschijnt."""

    key: str  # "categorie.uri", bijv. "measurements.ac_voltage"
    category: str
    uri: str
    name: str
    device_class: SensorDeviceClass | None
    unit: str | None
    state_class: SensorStateClass | None
    enabled_default: bool
    is_text: bool
    category_name: str = ""  # weergavenaam van de categorie, bijv. "PV measurements"


# Namen van de vaste omvormersensoren (Engels, zoals de API-namen); een metric met dezelfde naam krijgt de categorie erbij
RESERVED_INVERTER_NAMES = frozenset({"power", "energy today", "communication"})


def disambiguate(specs: list[MetricSpec], reserved: frozenset[str] = RESERVED_INVERTER_NAMES) -> list[MetricSpec]:
    """Maak namen uniek per apparaat: komt een naam vaker voor (of botst hij met een vaste sensor), dan wordt het
    ``"{naam} ({categorie})"``, bijvoorbeeld ``"Power (PV measurements)"``.
    """
    counts: dict[str, int] = {}
    for spec in specs:
        counts[spec.name.lower()] = counts.get(spec.name.lower(), 0) + 1
    return [
        replace(spec, name=f"{spec.name} ({spec.category_name or spec.category})")
        if counts[spec.name.lower()] > 1 or spec.name.lower() in reserved
        else spec
        for spec in specs
    ]


def metric_key(category: str, uri: str) -> str:
    """Unieke sleutel van een metric binnen een apparaattype."""
    return f"{category}.{uri}"


def describe_metric(
    category: str, meta: MetricMeta, core: frozenset[str] = CORE_METRICS, category_name: str | None = None
) -> MetricSpec | None:
    """Sensor-eigenschappen voor een metric, of ``None`` als hij niet als sensor bruikbaar is.

    - ``kind`` DELTA (hoeveelheid sinds de vorige telemetrie) levert geen zinvolle sensor op en wordt overgeslagen.
    - ``kind`` CUMULATIVE → ``total_increasing`` (geschikt voor het Energy-dashboard).
    - ``kind`` GAUGE → ``measurement``.
    - ``dataType`` STRING → tekstsensor zonder eenheid en state_class.
    """
    if meta.kind == "DELTA":
        return None

    key = metric_key(category, meta.uri)
    is_text = meta.data_type == "STRING"
    device_class, unit = (None, None) if is_text else UNIT_MAP.get(meta.units or "", (None, meta.units or None))
    if unit == PERCENTAGE and "power_factor" in meta.uri:
        device_class = SensorDeviceClass.POWER_FACTOR

    if is_text:
        state_class = None
    elif meta.kind == "CUMULATIVE":
        state_class = SensorStateClass.TOTAL_INCREASING
    else:
        state_class = SensorStateClass.MEASUREMENT
    # Energie kan alleen total/total_increasing zijn
    if device_class == SensorDeviceClass.ENERGY and state_class == SensorStateClass.MEASUREMENT:
        state_class = SensorStateClass.TOTAL

    return MetricSpec(
        key=key,
        category=category,
        uri=meta.uri,
        name=meta.display_name or meta.uri.replace("_", " ").capitalize(),
        device_class=device_class,
        unit=unit,
        state_class=state_class,
        enabled_default=key in core,
        is_text=is_text,
        category_name=category_name or category,
    )

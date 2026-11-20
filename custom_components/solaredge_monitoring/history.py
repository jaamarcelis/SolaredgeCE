"""Historie van *Energie totaal* ophalen bij SolarEdge en importeren in de recorderstatistieken.

SolarEdge levert de lifetime-teller van elke omvormer (``lifetime_positive_ac_energy``, Wh) per 5 minuten.
Langetermijnstatistieken in Home Assistant zijn per uur, dus de stand aan het eind van elk uur wordt geïmporteerd
in de statistieken van de sensor *Energie totaal* van het site-device.

Alleen uren vóór de eerste bestaande uurrij worden geïmporteerd. Daarna worden de sommen van de bestaande rijen
verschoven, zodat de grafiek en het Energy-dashboard zonder sprong doorlopen. Een tweede run met hetzelfde aantal
dagen doet niets; een run met meer dagen vult alleen het stuk ervoor aan.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics, statistics_during_period
from homeassistant.const import Platform, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .api import ApiError, SolarEdgeMonitoringClient, measurement_datasource
from .api.models import GenerateChartResponse
from .const import DOMAIN
from .discovery import SiteModel

_LOGGER = logging.getLogger(__name__)

LIFETIME_METRIC = ("measurements", "lifetime_positive_ac_energy")
CHUNK = timedelta(days=7)  # generate-chart weigert meer dan ~14 dagen per request (EXPORT_LIMIT_REACHED)
MIN_CHUNK = timedelta(days=1)
HOUR = timedelta(hours=1)
DEFAULT_DAYS = 90
MAX_DAYS = 365


class HistoryImportError(HomeAssistantError):
    """Import mislukt; ``translation_key`` verwijst naar ``exceptions`` in strings.json."""

    def __init__(self, key: str, **placeholders: Any) -> None:
        """Vertaalbare fout van deze integratie."""
        super().__init__(translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders or None)


@dataclass(slots=True)
class ExistingStart:
    """De eerste bestaande uurrij in het venster: begin, stand en som."""

    start: datetime
    state: float | None
    sum: float


@dataclass(slots=True)
class ImportPlan:
    """Wat er geïmporteerd en aangepast wordt; puur berekend, zonder HA."""

    rows: list[StatisticData]
    sum_adjustment: float  # op te tellen bij de bestaande rijen vanaf ``adjust_from``
    adjust_from: datetime | None


@dataclass(slots=True)
class ImportResult:
    """Resultaat voor de actie-response en de melding."""

    statistic_id: str
    imported_hours: int
    first_hour: datetime | None
    last_hour: datetime | None
    sum_adjustment: float

    def as_dict(self) -> dict[str, Any]:
        """Voor de response van de actie."""
        return {
            "statistic_id": self.statistic_id,
            "imported_hours": self.imported_hours,
            "from": self.first_hour.isoformat() if self.first_hour else None,
            "to": (self.last_hour + HOUR).isoformat() if self.last_hour else None,
            "sum_adjustment_kwh": round(self.sum_adjustment, 3),
        }


# --- Pure berekeningen --------------------------------------------------------------------------


def lifetime_series(charts: list[GenerateChartResponse]) -> list[tuple[datetime, float]]:
    """Totale lifetime (kWh) per tijdstip: de som over alle omvormers, met forward-fill per omvormer.

    Tijdstippen vóór de eerste waarde van een omvormer tellen niet mee (anders zou de som daar te laag zijn).
    """
    per_device: dict[str, dict[datetime, float]] = {}
    for chart in charts:
        if not chart.meta:
            continue
        for index, dataset in enumerate(chart.meta.datasets_meta):
            try:
                column = next(i for i, m in enumerate(dataset.metrics) if m.uri == LIFETIME_METRIC[1])
            except StopIteration:
                continue
            device = dataset.report_object.entity_id if dataset.report_object else str(index)
            values = per_device.setdefault(device, {})
            for point in chart.points(index):
                value = point.values[column] if column < len(point.values) else None
                if isinstance(value, int | float) and not isinstance(value, bool):
                    values[point.timestamp] = float(value) / 1000

    if not per_device:
        return []
    timestamps = sorted({t for values in per_device.values() for t in values})
    last: dict[str, float] = {}
    series: list[tuple[datetime, float]] = []
    for timestamp in timestamps:
        for device, values in per_device.items():
            if timestamp in values:
                last[device] = values[timestamp]
        if len(last) == len(per_device):
            series.append((timestamp, sum(last.values())))
    return series


def value_at(series: list[tuple[datetime, float]], moment: datetime) -> float | None:
    """De laatst bekende waarde op of vóór ``moment``."""
    index = bisect.bisect_right([t for t, _ in series], moment)
    return series[index - 1][1] if index else None


def build_import(
    series: list[tuple[datetime, float]],
    window_start: datetime,
    existing: ExistingStart,
) -> ImportPlan:
    """Uurrijen vóór de eerste bestaande rij, en de verschuiving van de bestaande sommen.

    - ``base`` = de eerste waarde in het venster; ``sum`` van uur h = L(eind h) − base.
    - ``state`` wordt verschoven met ``offset`` zodat hij aansluit op de stand van de sensor in de eerste
      bestaande rij (de site-teller en de omvormertellers wijken iets van elkaar af).
    - De bestaande rijen vanaf ``existing.start`` krijgen ``(L(eind t0) − base) − sum(t0)`` erbij.
    """
    window_start = window_start.replace(minute=0, second=0, microsecond=0)
    in_window = [(t, v) for t, v in series if t >= window_start]
    if not in_window or existing.start <= window_start:
        return ImportPlan([], 0.0, None)

    base = in_window[0][1]
    t0_end = existing.start + HOUR
    lifetime_t0 = value_at(series, t0_end)
    offset = existing.state - lifetime_t0 if existing.state is not None and lifetime_t0 is not None else 0.0

    times = [t for t, _ in in_window]
    rows: list[StatisticData] = []
    hour = window_start
    while hour < existing.start:
        index = bisect.bisect_right(times, hour + HOUR)
        # Alleen uren met minstens één meting erin (na het begin van het uur)
        if index and times[index - 1] > hour:
            value = in_window[index - 1][1]
            rows.append(StatisticData(start=hour, state=round(value + offset, 3), sum=round(value - base, 3)))
        hour += HOUR

    if not rows:
        return ImportPlan([], 0.0, None)
    # Zonder data voor het eind van t0: neem aan dat de som van t0 alleen de productie binnen t0 bevat
    expected_t0 = lifetime_t0 - base if lifetime_t0 is not None else rows[-1]["sum"] + existing.sum
    return ImportPlan(rows, round(expected_t0 - existing.sum, 3), existing.start)


def chunks(start: datetime, end: datetime, size: timedelta = CHUNK) -> list[tuple[datetime, datetime]]:
    """``[start, end)`` in blokken van hoogstens ``size``."""
    result = []
    while start < end:
        result.append((start, min(start + size, end)))
        start += size
    return result


# --- Ophalen en importeren ----------------------------------------------------------------------


def site_energy_entity_id(hass: HomeAssistant, model: SiteModel) -> str:
    """Het entity_id van *Energie totaal* (de statistic_id van de sensor)."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(Platform.SENSOR, DOMAIN, f"{model.site_id}_site_energy_lifetime")
    if entity_id is None:
        raise HistoryImportError("energy_entity_not_found")
    entry = registry.async_get(entity_id)
    if entry is not None and entry.disabled:
        raise HistoryImportError("energy_entity_disabled", entity_id=entity_id)
    return entity_id


async def fetch_lifetime(
    client: SolarEdgeMonitoringClient, model: SiteModel, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """De lifetime per 5 minuten over ``[start, end)``, in blokken van 7 dagen (kleiner bij EXPORT_LIMIT_REACHED)."""
    by_type: dict[str, list[str]] = {}
    for inverter in model.inverters:
        by_type.setdefault(inverter.analysis_type, []).append(inverter.identifier)
    if not by_type:
        raise HistoryImportError("no_inverters")
    datasources = [
        measurement_datasource(model.site_id, device_type, identifiers, [LIFETIME_METRIC], "FIVE_MINUTES")
        for device_type, identifiers in by_type.items()
    ]

    charts: list[GenerateChartResponse] = []
    pending = chunks(start, end)
    while pending:
        chunk_start, chunk_end = pending.pop(0)
        try:
            charts.append(await client.analysis.generate_chart(model.site_id, chunk_start, chunk_end, datasources))
        except ApiError as err:
            size = chunk_end - chunk_start
            if err.code != "EXPORT_LIMIT_REACHED" or size <= MIN_CHUNK:
                raise
            pending[:0] = chunks(chunk_start, chunk_end, max(size / 2, MIN_CHUNK))
    _LOGGER.debug("Historie opgehaald in %d blokken (%s – %s)", len(charts), start, end)
    return lifetime_series(charts)


async def _first_existing(hass: HomeAssistant, statistic_id: str, window_start: datetime) -> ExistingStart | None:
    """De eerste uurrij vanaf ``window_start``, of ``None`` als de sensor nog geen uurstatistieken heeft."""
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        window_start,
        None,
        {statistic_id},
        "hour",
        {"energy": UnitOfEnergy.KILO_WATT_HOUR},
        {"state", "sum"},
    )
    rows = stats.get(statistic_id) or []
    first = next((r for r in rows if r.get("sum") is not None), None)
    if first is None:
        return None
    return ExistingStart(dt_util.utc_from_timestamp(first["start"]), first.get("state"), float(first["sum"]))


async def async_import_history(
    hass: HomeAssistant, client: SolarEdgeMonitoringClient, model: SiteModel, days: int = DEFAULT_DAYS
) -> ImportResult:
    """Haal ``days`` dagen historie op en importeer die in de statistieken van *Energie totaal*."""
    if "recorder" not in hass.config.components:
        raise HistoryImportError("recorder_not_running")
    statistic_id = site_energy_entity_id(hass, model)

    now = dt_util.utcnow()
    window_start = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    existing = await _first_existing(hass, statistic_id, window_start)
    if existing is None:
        raise HistoryImportError("no_statistics_yet", entity_id=statistic_id)
    if existing.start <= window_start:
        return ImportResult(statistic_id, 0, None, None, 0.0)

    # Tot en met het eind van t0, nodig voor de verschuiving van de bestaande rijen
    series = await fetch_lifetime(client, model, window_start, min(existing.start + 2 * HOUR, now))
    if not series:
        raise HistoryImportError("no_history_data")

    plan = build_import(series, window_start, existing)
    if not plan.rows:
        return ImportResult(statistic_id, 0, None, None, 0.0)

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=statistic_id,
        unit_class="energy",
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    async_import_statistics(hass, metadata, plan.rows)
    if plan.adjust_from is not None and plan.sum_adjustment:
        get_instance(hass).async_adjust_statistics(
            statistic_id, plan.adjust_from, plan.sum_adjustment, UnitOfEnergy.KILO_WATT_HOUR
        )
    _LOGGER.info(
        "%d uren historie geïmporteerd voor %s; bestaande sommen verschoven met %.3f kWh",
        len(plan.rows),
        statistic_id,
        plan.sum_adjustment,
    )
    return ImportResult(statistic_id, len(plan.rows), plan.rows[0]["start"], plan.rows[-1]["start"], plan.sum_adjustment)

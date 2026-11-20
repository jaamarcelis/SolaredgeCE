"""Historie uit SolarEdge importeren in de recorderstatistieken van de sensoren van een device.

Per device bepaalt :func:`importable_metrics` welke sensoren een historische bron hebben (``generate-chart`` per
5 minuten). Wat er geschreven wordt hangt af van het soort metric:

- ``gauge`` (vermogen, spanning, temperatuur, …): gemiddelde, minimum en maximum per periode;
- ``cumulative`` (lifetime-tellers): stand en een doorlopende som;
- ``delta`` (energie per meting, voor sensoren die elke dag op 0 beginnen): som van de delta's, stand = dagtotaal.

Het interval bepaalt de rijen:

- ``five_minutes``: kortetermijnstatistieken per 5 minuten (de recorder ruimt die na ``keep_days`` op) plus uurrijen;
- ``hour``: een uurrij per uur;
- ``day``: één rij per dag. Bij sommen staat die in het laatste uur van de (lokale) dag met de stand aan het eind
  van de dag, bij gemiddelden in het eerste uur met het daggemiddelde;
- ``max``: zo veel en zo nauwkeurig mogelijk: per 5 minuten zo ver als SolarEdge dat levert (uurrijen, plus
  5-minutenrijen binnen ``keep_days``), daarvóór per dag tot de eerste productiedag.

De limieten van SolarEdge (periode per request, hoe ver terug) staan in :mod:`.history_limits`; nieuwe limieten
worden uit foutmeldingen geleerd en de blokken passen zich daaraan aan.

Bij sommen sluit de reeks aan op de bestaande rij vóór de periode, worden bestaande rijen binnen de periode met
dezelfde reeks herschreven en schuiven alle rijen erna mee. Dezelfde import nog een keer doen verandert dus niets.
De periode eindigt uiterlijk bij de laatste uurrij van de recorder: daar telt de recorder zelf verder.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import itertools
import logging
from typing import Any, Literal

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.sensor.const import UNIT_CONVERTERS
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .api import ApiError, SolarEdgeMonitoringClient, measurement_datasource
from .api.models import GenerateChartResponse
from .const import (
    DOMAIN,
    HISTORY_INTERVAL_DAY,
    HISTORY_INTERVAL_FIVE_MINUTES,
    HISTORY_INTERVAL_HOUR,
    HISTORY_INTERVAL_MAX,
)
from .discovery import OPTIMIZER_TYPE, SiteModel
from .history_limits import HistoryLimits, granularity_class
from .metrics import UNIT_MAP

_LOGGER = logging.getLogger(__name__)

MIN_CHUNK = timedelta(days=1)
FIVE_MINUTES = timedelta(minutes=5)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
LOOKBACK = timedelta(days=1)  # 's nachts meet de omvormer niet: de stand van het begin komt van de dag ervoor
NEAR = timedelta(days=7)  # ankerpunten dichter bij de periode worden in dezelfde requests meegenomen
PREV_SEARCH = (timedelta(days=2), timedelta(days=35), timedelta(days=400), None)

type Interval = Literal["max", "five_minutes", "hour", "day"]
type Kind = Literal["gauge", "cumulative", "delta"]
type Series = list[tuple[datetime, float]]

KIND_BY_API = {"GAUGE": "gauge", "CUMULATIVE": "cumulative", "DELTA": "delta"}

# Teller → de delta-metric met dezelfde energie. Per dag is de teller een daggemiddelde, de delta de dagproductie.
DELTA_PAIRS = {"measurements.lifetime_positive_ac_energy": "measurements.positive_ac_energy_since_last_telemetry"}

# Vaste sensoren met een historische bron: sleutel van de sensor → metric ("categorie.uri")
SITE_SOURCES = {
    "energy_lifetime": "measurements.lifetime_positive_ac_energy",
    "energy_today": "measurements.positive_ac_energy_since_last_telemetry",
    "current_power": "pv_measurements.power",
    "ac_power": "measurements.active_power",
}
INVERTER_SOURCES = {
    "power": "pv_measurements.power",
    "energy_today": "measurements.positive_ac_energy_since_last_telemetry",
}
OPTIMIZER_SOURCES = {
    "power": "measurements.output_power",
    "panel_voltage": "measurements.panel_voltage",
    "output_voltage": "measurements.output_voltage",
    "current": "measurements.panel_current",
    "energy_today": "measurements.energy_since_last_telem",
}


class HistoryImportError(HomeAssistantError):
    """Import mislukt; ``translation_key`` verwijst naar ``exceptions`` in strings.json."""

    def __init__(self, key: str, **placeholders: Any) -> None:
        """Vertaalbare fout van deze integratie."""
        super().__init__(translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders or None)


@dataclass(frozen=True, slots=True)
class ImportableMetric:
    """Een sensor met een historische bron in SolarEdge."""

    unique_id: str
    entity_id: str
    name: str
    metric: str  # "categorie.uri" in generate-chart
    kind: Kind
    api_unit: str | None  # Home Assistant-eenheid van de API-waarden
    device_class: SensorDeviceClass | None
    sources: tuple[tuple[str, tuple[str, ...]], ...]  # (analysis-apparaattype, identifiers); waarden worden opgeteld
    delta_metric: str | None = None  # energie per meting; voor dagresolutie (zie DELTA_PAIRS)

    @property
    def has_sum(self) -> bool:
        """Of de statistiek een som heeft (anders gemiddelde/min/max)."""
        return self.kind != "gauge"


@dataclass(slots=True)
class Row:
    """Een bestaande statistiekrij."""

    start: datetime
    state: float | None
    sum: float


@dataclass(slots=True)
class Existing:
    """De bestaande statistieken rond de periode."""

    prev: Row | None  # laatste uurrij vóór de periode
    next: Row | None  # eerste uurrij vanaf het eind van de periode
    hourly: list[datetime] = field(default_factory=list)  # bestaande uurrijen binnen de periode
    short_term: list[datetime] = field(default_factory=list)  # bestaande 5-minutenrijen tot ``next``


@dataclass(slots=True)
class ImportPlan:
    """Wat er geschreven en verschoven wordt; puur berekend, zonder HA."""

    hourly: list[StatisticData]
    short_term: list[StatisticData]
    sum_adjustment: float = 0.0  # op te tellen bij alle rijen vanaf ``adjust_from``
    adjust_from: datetime | None = None


@dataclass(slots=True)
class MetricResult:
    """Resultaat per sensor."""

    entity_id: str
    name: str
    hourly_rows: int
    short_term_rows: int
    sum_adjustment: float | None
    first: datetime | None = None  # begin van de oudste geschreven rij
    notes: list[dict[str, Any]] = field(default_factory=list)  # limieten en meldingen van SolarEdge

    def as_dict(self) -> dict[str, Any]:
        """Voor de response van de actie."""
        result: dict[str, Any] = {
            "entity_id": self.entity_id,
            "hourly_rows": self.hourly_rows,
            "short_term_rows": self.short_term_rows,
            "first": self.first.isoformat() if self.first else None,
        }
        if self.sum_adjustment is not None:
            result["sum_adjustment"] = round(self.sum_adjustment, 3)
        if self.notes:
            result["notes"] = self.notes
        return result


@dataclass(slots=True)
class ImportResult:
    """Resultaat van een import over één of meer sensoren."""

    interval: Interval
    start: datetime
    end: datetime
    metrics: list[MetricResult]

    def as_dict(self) -> dict[str, Any]:
        """Voor de response van de actie."""
        return {
            "interval": self.interval,
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "metrics": [m.as_dict() for m in self.metrics],
        }


# --- Welke sensoren historie hebben -------------------------------------------------------------


def _device_kind(model: SiteModel, device: dr.DeviceEntry) -> tuple[str, str] | None:
    """(soort, sleutel) van een device van deze site: ("site", …), ("inverter", serial) of ("optimizer", serial)."""
    identifiers = {i for d, i in device.identifiers if d == DOMAIN}
    if f"site_{model.site_id}" in identifiers:
        return "site", "site"
    for inverter in model.inverters:
        if inverter.serial in identifiers:
            return "inverter", inverter.serial
    for optimizer in model.optimizers:
        if optimizer.serial in identifiers:
            return "optimizer", optimizer.serial
    return None


def importable_metrics(hass: HomeAssistant, model: SiteModel, device_id: str) -> list[ImportableMetric]:
    """De sensoren van ``device_id`` waarvan SolarEdge historie heeft, in de volgorde van het device."""
    device = dr.async_get(hass).async_get(device_id)
    found = _device_kind(model, device) if device else None
    if found is None:
        return []
    kind, key = found
    by_type: dict[str, list[str]] = {}
    for inverter in model.inverters:
        by_type.setdefault(inverter.analysis_type, []).append(inverter.identifier)

    candidates: list[tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...]]] = []  # (sensor-sleutel, metric, bronnen)
    if kind == "site":
        sources = tuple((t, tuple(ids)) for t, ids in by_type.items())
        candidates = [(k, m, sources) for k, m in SITE_SOURCES.items()]
    elif kind == "inverter":
        inverter = next(i for i in model.inverters if i.serial == key)
        sources = ((inverter.analysis_type, (inverter.identifier,)),)
        candidates = [(k, m, sources) for k, m in INVERTER_SOURCES.items()]
        candidates += [(f"metric_{s.key}", s.key, sources) for s in model.metrics.get(inverter.analysis_type, [])]
    else:
        optimizer = next(o for o in model.optimizers if o.serial == key)
        sources = ((OPTIMIZER_TYPE, (optimizer.identifier,)),)
        candidates = [(k, m, sources) for k, m in OPTIMIZER_SOURCES.items()]

    registry = er.async_get(hass)
    result: list[ImportableMetric] = []
    for sensor_key, metric, sources in candidates:
        available = [model.history_metrics.get(t, {}).get(metric) for t, _ in sources]
        if not available or any(a is None for a in available):
            continue
        api_units, api_kind = available[0]  # type: ignore[misc]
        metric_kind = KIND_BY_API.get(api_kind)
        device_class, api_unit = UNIT_MAP.get(api_units, (None, api_units or None))
        if metric_kind == "delta" and device_class != SensorDeviceClass.ENERGY:
            metric_kind = "gauge"  # SolarEdge meldt bijv. output_power (W) van optimizers als DELTA
        if metric_kind is None:
            continue
        unique_id = f"{model.site_id}_{key}_{sensor_key}"
        entity_id = registry.async_get_entity_id(Platform.SENSOR, DOMAIN, unique_id)
        entry = registry.async_get(entity_id) if entity_id else None
        if entry is None or not _matches(entry, metric_kind):
            continue
        name = entry.name or entry.original_name or entry.entity_id
        delta_metric = metric if metric_kind == "delta" else None
        if metric_kind == "cumulative" and (pair := DELTA_PAIRS.get(metric)):
            pairs = [model.history_metrics.get(t, {}).get(pair) for t, _ in sources]
            if all(p is not None and p[0] == api_units and p[1] == "DELTA" for p in pairs):
                delta_metric = pair
        result.append(
            ImportableMetric(
                unique_id, entry.entity_id, str(name), metric, metric_kind, api_unit, device_class, sources, delta_metric
            )
        )
    return result


def _matches(entry: er.RegistryEntry, kind: Kind) -> bool:
    """Past het soort metric bij de state_class van de sensor? Tekstsensoren hebben geen statistieken."""
    state_class = (entry.capabilities or {}).get("state_class")
    if kind == "gauge":
        return state_class == SensorStateClass.MEASUREMENT
    return state_class in (SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING)


# --- Pure berekeningen --------------------------------------------------------------------------


def metric_series(charts: list[GenerateChartResponse], uri: str, *, delta: bool = False) -> Series:
    """De waarden van ``uri`` per tijdstip, opgeteld over alle apparaten.

    Standen en momentwaarden worden per apparaat doorgetrokken (forward-fill); tijdstippen vóór de eerste waarde van
    een apparaat tellen niet mee. Bij ``delta`` is het resultaat de lopende som van alle delta's.
    """
    per_device: dict[str, dict[datetime, float]] = {}
    for chart in charts:
        if not chart.meta:
            continue
        for index, dataset in enumerate(chart.meta.datasets_meta):
            try:
                column = next(i for i, m in enumerate(dataset.metrics) if m.uri == uri)
            except StopIteration:
                continue
            device = dataset.report_object.entity_id if dataset.report_object else str(index)
            values = per_device.setdefault(device, {})
            for point in chart.points(index):
                value = point.values[column] if column < len(point.values) else None
                if isinstance(value, int | float) and not isinstance(value, bool):
                    values[point.timestamp] = float(value)

    if not per_device:
        return []
    timestamps = sorted({t for values in per_device.values() for t in values})
    series: Series = []
    if delta:
        total = 0.0
        for timestamp in timestamps:
            total += sum(values.get(timestamp, 0.0) for values in per_device.values())
            series.append((timestamp, total))
        return series
    last: dict[str, float] = {}
    for timestamp in timestamps:
        for device, values in per_device.items():
            if timestamp in values:
                last[device] = values[timestamp]
        if len(last) == len(per_device):
            series.append((timestamp, sum(last.values())))
    return series


def value_at(series: Series, moment: datetime) -> float | None:
    """De laatst bekende waarde op of vóór ``moment``."""
    index = bisect.bisect_right([t for t, _ in series], moment)
    return series[index - 1][1] if index else None


def interval_starts(
    start: datetime,
    end: datetime,
    interval: Interval,
    fine_from: datetime | None = None,
    short_from: datetime | None = None,
) -> tuple[list[datetime], list[datetime]]:
    """Begintijden van de eigen sommenrijen in ``[start, end)``: (uurrijen, 5-minutenrijen).

    Bij ``day`` is dat het laatste uur van elke lokale dag, zodat de dagstand op het eind van de dag valt. Bij ``max``
    dagrijen vóór ``fine_from``, uurrijen daarna en 5-minutenrijen vanaf ``short_from``.
    """
    if interval == HISTORY_INTERVAL_MAX:
        boundary = min(max(fine_from or start, start), end)
        day_rows, _ = interval_starts(start, boundary, HISTORY_INTERVAL_DAY)
        hour_rows, _ = interval_starts(boundary, end, HISTORY_INTERVAL_HOUR)
        short_start = max(boundary, short_from or boundary)
        _, short_rows = interval_starts(short_start, end, HISTORY_INTERVAL_FIVE_MINUTES) if short_start < end else ([], [])
        return day_rows + hour_rows, short_rows

    hourly: list[datetime] = []
    short_term: list[datetime] = []
    if interval == HISTORY_INTERVAL_DAY:
        day = dt_util.as_local(start).date()
        while True:
            last_hour = dt_util.as_utc(dt_util.start_of_local_day(day + DAY)) - HOUR
            if last_hour >= end:
                break
            if last_hour >= start:
                hourly.append(last_hour)
            day += DAY
        return hourly, short_term

    moment = start
    while moment < end:
        hourly.append(moment)
        moment += HOUR
    if interval == HISTORY_INTERVAL_FIVE_MINUTES:
        moment = start
        while moment < end:
            short_term.append(moment)
            moment += FIVE_MINUTES
    return hourly, short_term


def daily_total(series: Series) -> Callable[[datetime, float], float]:
    """Stand voor sensoren die elke (lokale) dag op 0 beginnen: lopende som min de som aan het begin van de dag."""

    def state(row_end: datetime, value: float) -> float:
        midnight = dt_util.as_utc(dt_util.start_of_local_day(dt_util.as_local(row_end - timedelta(microseconds=1))))
        at_midnight = value_at(series, midnight)
        return value - (at_midnight if at_midnight is not None else series[0][1])

    return state


def build_import(
    series: Series,
    start: datetime,
    end: datetime,
    interval: Interval,
    existing: Existing,
    state_of: Callable[[datetime, float], float] | None = None,
    fine_from: datetime | None = None,
    short_from: datetime | None = None,
) -> ImportPlan:
    """Sommenrijen voor ``[start, end)`` en de verschuiving van de rijen erna.

    ``series`` is een oplopende stand ``L(t)``.

    - Som: aansluitend op de laatste rij vóór de periode, ``prev.sum + L(t) − L(eind prev)``. Zonder eerdere rij
      begint de som op 0 bij de eerste meting.
    - Stand: ``state_of(eind, L)`` of anders ``L(t) + offset``; de offset laat de stand aansluiten op de sensor.
    - Bestaande rijen binnen de periode worden met dezelfde reeks herschreven; rijen vanaf ``next`` verschoven.
    """
    if not series or start >= end:
        return ImportPlan([], [])
    last_time = series[-1][0]
    first_time = series[0][0]

    def lifetime(moment: datetime) -> float | None:
        return value_at(series, moment)

    prev, nxt = existing.prev, existing.next
    prev_l = lifetime(prev.start + HOUR) if prev else None
    next_l = lifetime(nxt.start + HOUR) if nxt else None

    if state_of is not None:
        offset = 0.0
    elif nxt is not None and nxt.state is not None and next_l is not None:
        offset = nxt.state - next_l
    elif prev is not None and prev.state is not None and prev_l is not None:
        offset = prev.state - prev_l
    else:
        offset = 0.0

    if prev is not None and prev_l is not None:
        anchor_l, anchor_sum = prev_l, prev.sum
    elif prev is not None and prev.state is not None and state_of is None:
        anchor_l, anchor_sum = prev.state - offset, prev.sum  # geen meting bij prev: via de stand
    else:
        start_l = lifetime(start)
        anchor_l = start_l if start_l is not None else next((v for t, v in series if t >= start), series[-1][1])
        anchor_sum = prev.sum if prev is not None else 0.0

    def row(row_start: datetime, length: timedelta) -> StatisticData | None:
        row_end = row_start + length
        if row_end <= first_time or row_start >= last_time:
            return None  # geen meting in of vóór deze rij, of al voorbij de laatste
        value = lifetime(row_end)
        if value is None:
            return None
        state = state_of(row_end, value) if state_of is not None else value + offset
        return StatisticData(start=row_start, state=round(state, 6), sum=round(anchor_sum + value - anchor_l, 6))

    own_hourly, own_short = interval_starts(start, end, interval, fine_from, short_from)
    hourly_starts = sorted(set(own_hourly) | {t for t in existing.hourly if start <= t < end})
    short_limit = nxt.start if nxt else end
    short_starts = sorted(set(own_short) | {t for t in existing.short_term if start <= t < short_limit})

    hourly = [r for t in hourly_starts if (r := row(t, HOUR))]
    short_term = [r for t in short_starts if (r := row(t, FIVE_MINUTES))]

    adjustment = 0.0
    if nxt is not None and next_l is not None and (hourly or short_term):
        adjustment = round(anchor_sum + next_l - anchor_l - nxt.sum, 6)
    return ImportPlan(hourly, short_term, adjustment, nxt.start if nxt and adjustment else None)


def build_gauge_import(
    series: Series, start: datetime, end: datetime, interval: Interval, *, mean_only: bool = False
) -> ImportPlan:
    """Gemiddelde, minimum en maximum van de metingen in elke periode (meting op ``t`` hoort bij ``[t, t+5m)``).

    ``mean_only``: de metingen zijn al daggemiddelden (dagresolutie); minimum en maximum zijn dan onbekend.
    """

    def aggregate(row_start: datetime, row_end: datetime) -> StatisticData | None:
        lo = bisect.bisect_left(times, row_start)
        hi = bisect.bisect_left(times, row_end)
        values = [v for _, v in series[lo:hi]]
        if not values:
            return None
        if mean_only:
            return StatisticData(start=row_start, mean=round(sum(values) / len(values), 6))
        return StatisticData(
            start=row_start, mean=round(sum(values) / len(values), 6), min=round(min(values), 6), max=round(max(values), 6)
        )

    times = [t for t, _ in series]
    hourly: list[StatisticData] = []
    short_term: list[StatisticData] = []
    if interval == HISTORY_INTERVAL_DAY:
        day = dt_util.as_local(start).date()
        while (day_start := dt_util.as_utc(dt_util.start_of_local_day(day))) < end:
            if day_start >= start and (
                r := aggregate(day_start, min(dt_util.as_utc(dt_util.start_of_local_day(day + DAY)), end))
            ):
                hourly.append(r)
            day += DAY
        return ImportPlan(hourly, short_term)

    moment = start
    while moment < end:
        if r := aggregate(moment, moment + HOUR):
            hourly.append(r)
        moment += HOUR
    if interval == HISTORY_INTERVAL_FIVE_MINUTES:
        moment = start
        while moment < end:
            if r := aggregate(moment, moment + FIVE_MINUTES):
                short_term.append(r)
            moment += FIVE_MINUTES
    return ImportPlan(hourly, short_term)


def chunks(start: datetime, end: datetime, size: timedelta = timedelta(days=31)) -> list[tuple[datetime, datetime]]:
    """``[start, end)`` in blokken van hoogstens ``size``."""
    result = []
    while start < end:
        result.append((start, min(start + size, end)))
        start += size
    return result


def fetch_ranges(
    start: datetime, end: datetime, existing: Existing, now: datetime, *, contiguous: bool = False
) -> list[tuple[datetime, datetime]]:
    """Op te halen bereiken: de periode (met een dag ervoor) plus de ankerpunten ervoor en erna.

    Verre ankerpunten krijgen een eigen klein bereik, behalve bij ``contiguous`` (delta's moeten aaneensluiten).
    """
    main_start, main_end = start - LOOKBACK, end
    extra: list[tuple[datetime, datetime]] = []
    if existing.prev is not None:
        prev_end = existing.prev.start + HOUR
        if contiguous or prev_end >= start - NEAR:
            main_start = min(main_start, prev_end - LOOKBACK)
        else:
            extra.append((prev_end - LOOKBACK, prev_end + FIVE_MINUTES))
    if existing.next is not None:
        next_end = existing.next.start + HOUR
        if contiguous or next_end <= end + NEAR:
            main_end = max(main_end, next_end + FIVE_MINUTES)
        else:
            extra.append((next_end - LOOKBACK, next_end + FIVE_MINUTES))
    ranges = [(main_start, main_end), *extra]
    return [(s, min(e, now)) for s, e in ranges if s < now]


def local_midnight(day: date) -> datetime:
    """Begin van een lokale dag, in UTC."""
    return dt_util.as_utc(dt_util.start_of_local_day(day))


def day_of(moment: datetime) -> date:
    """De lokale dag waarin een periode die op ``moment`` eindigt valt."""
    return dt_util.as_local(moment - timedelta(microseconds=1)).date()


def day_segments(
    start: datetime, end: datetime, fine_days: set[date]
) -> tuple[list[tuple[datetime, datetime]], list[tuple[datetime, datetime]]]:
    """Hele lokale dagen van ``[start, end)`` in (dag-, 5-minuten-)bereiken; ``fine_days`` per 5 minuten."""
    coarse: list[tuple[datetime, datetime]] = []
    fine: list[tuple[datetime, datetime]] = []
    day = dt_util.as_local(start).date()
    last = day_of(end)
    while day <= last:
        is_fine = day in fine_days
        first = day
        while day + DAY <= last and (day + DAY in fine_days) == is_fine:
            day += DAY
        segment_end = local_midnight(day + DAY)
        if is_fine:
            fine.append((local_midnight(first), min(segment_end, end)))
        else:
            coarse.append((local_midnight(first), segment_end))
        day += DAY
    return coarse, fine


def _points(charts: list[GenerateChartResponse], uri: str) -> dict[str, list[tuple[datetime, float]]]:
    """Per apparaat de numerieke waarden van ``uri``, op tijd gesorteerd."""
    per_device: dict[str, dict[datetime, float]] = {}
    for chart in charts:
        if not chart.meta:
            continue
        for index, dataset in enumerate(chart.meta.datasets_meta):
            column = next((i for i, m in enumerate(dataset.metrics) if m.uri == uri), None)
            if column is None:
                continue
            device = dataset.report_object.entity_id if dataset.report_object else str(index)
            values = per_device.setdefault(device, {})
            for point in chart.points(index):
                value = point.values[column] if column < len(point.values) else None
                if isinstance(value, int | float) and not isinstance(value, bool):
                    values[point.timestamp] = float(value)
    return {device: sorted(values.items()) for device, values in per_device.items()}


def delta_series(
    coarse: list[GenerateChartResponse],
    fine: list[GenerateChartResponse],
    uri: str,
    *,
    fine_uri: str | None = None,
) -> Series:
    """Lopende som over alle apparaten: dagtotalen van ``uri`` (op het eind van de dag gezet) plus de 5-minutendata.

    De 5-minutendata zijn delta's van ``uri``, of bij ``fine_uri`` standen van een teller; dan tellen de verschillen
    tussen opeenvolgende standen (preciezer dan de delta's), behalve over dagen die al als dagtotaal meetellen.
    Op de grens telt het dagtotaal: een 5-minutenwaarde precies om middernacht na een dagtotaal valt weg ('s nachts 0).
    """
    events: dict[datetime, float] = {}
    for values in _points(coarse, uri).values():
        for timestamp, value in values:
            moment = local_midnight(dt_util.as_local(timestamp).date() + DAY)
            events[moment] = events.get(moment, 0.0) + value
    day_ends = sorted(events)
    day_end_set = set(day_ends)

    for values in _points(fine, fine_uri or uri).values():
        previous: tuple[datetime, float] | None = None
        for timestamp, value in values:
            if fine_uri is None:
                # Om middernacht na een dagtotaal: die 5 minuten zitten er al in (of zijn 's nachts 0)
                if timestamp not in day_end_set:
                    events[timestamp] = events.get(timestamp, 0.0) + value
                continue
            # Een teller: het verschil met de vorige stand, tenzij er een dagtotaal tussen zit
            if previous is not None and bisect.bisect_right(day_ends, timestamp) == bisect.bisect_right(day_ends, previous[0]):
                events[timestamp] = events.get(timestamp, 0.0) + value - previous[1]
            previous = (timestamp, value)

    series: Series = []
    total = 0.0
    for moment in sorted(events):
        total += events[moment]
        series.append((moment, total))
    return series


# --- Ophalen en importeren ----------------------------------------------------------------------


def _note(notes: list[dict[str, Any]], note: dict[str, Any]) -> None:
    if note not in notes:
        notes.append(note)


def _coarser_than_requested(chart: GenerateChartResponse) -> bool:
    """Gaf SolarEdge dagwaarden terug terwijl om een fijnere resolutie gevraagd werd (stille terugval)?"""
    stamps = sorted({p.timestamp for i in range(len(chart.data)) for p in chart.points(i)})
    if len(stamps) < 3:
        return False
    return min(b - a for a, b in itertools.pairwise(stamps)) >= DAY


async def fetch_charts(
    client: SolarEdgeMonitoringClient,
    site_id: int,
    metric: ImportableMetric,
    ranges: list[tuple[datetime, datetime]],
    *,
    uri: str | None = None,
    granularity: str = "FIVE_MINUTES",
    limits: HistoryLimits | None = None,
    notes: list[dict[str, Any]] | None = None,
) -> list[GenerateChartResponse]:
    """generate-chart over ``ranges`` in zo groot mogelijke blokken binnen de limieten.

    - Blokken zo groot als de bekende periode per request; ouder dan de leeftijdsgrens wordt overgeslagen.
    - Een foutmelding met een limiet wordt geleerd, waarna het blok opnieuw wordt opgedeeld.
    - Komt er stilletjes dagresolutie terug, dan wordt het blok kleiner.
    """
    limits = limits or HistoryLimits(None, None)
    notes = notes if notes is not None else []
    full = uri or metric.metric
    category, name = full.split(".", 1)
    device_types = [device_type for device_type, _ in metric.sources]
    datasources = [
        measurement_datasource(site_id, device_type, identifiers, [(category, name)], granularity)
        for device_type, identifiers in metric.sources
    ]
    now = dt_util.utcnow()

    def plan(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        found = [limits.get(device_type, full, granularity) for device_type in device_types]
        ages = [f.max_age_days for f in found if f.max_age_days is not None]
        if ages:
            oldest = now - timedelta(days=min(ages)) + DAY  # een dag marge: de grens is inclusief "vandaag"
            if start < oldest:
                _note(notes, {"code": "age_limit", "metric": full, "granularity": granularity, "days": int(min(ages))})
                start = oldest
        if start >= end:
            return []
        sizes = [f.max_range_days for f in found if f.max_range_days is not None]
        return chunks(start, end, timedelta(days=min(sizes)) if sizes else timedelta(days=31))

    charts: list[GenerateChartResponse] = []
    pending = [c for start, end in ranges for c in plan(start, end)]
    while pending:
        chunk_start, chunk_end = pending.pop(0)
        size = chunk_end - chunk_start
        try:
            chart = await client.analysis.generate_chart(site_id, chunk_start, chunk_end, datasources)
        except ApiError as err:
            message = err.detail or str(err)
            if err.code in ("EXPORT_LIMIT_REACHED", "DATASET_TOO_LARGE"):
                learned = any([limits.learn(device_type, full, granularity, message) for device_type in device_types])
                replan = plan(chunk_start, chunk_end)
                if learned and replan != [(chunk_start, chunk_end)]:
                    _note(notes, {"code": err.code, "metric": full, "granularity": granularity, "message": message})
                    pending[:0] = replan
                    continue
                if "export tool" in message or "only available" in message:
                    # Ouder dan wat de grafiek levert: overslaan en melden
                    _note(notes, {"code": err.code, "metric": full, "granularity": granularity, "message": message})
                    continue
                if size > MIN_CHUNK:
                    pending[:0] = chunks(chunk_start, chunk_end, max(size / 2, MIN_CHUNK))
                    continue
            raise
        if granularity_class(granularity) == "fine" and size > DAY and _coarser_than_requested(chart):
            days = min(31.0, max(1.0, size.days // 2))  # grens onbekend: halveren
            for device_type in device_types:
                limits.learn_range(device_type, full, granularity, days, f"dagwaarden bij {size.days} dagen per request")
            _note(notes, {"code": "downsampled", "metric": full, "granularity": granularity, "days": int(days)})
            pending[:0] = plan(chunk_start, chunk_end)
            continue
        charts.append(chart)
    _LOGGER.debug("%s: %d blokken per %s opgehaald (%s)", metric.entity_id, len(charts), granularity, ranges)
    return charts


def _rows(hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime | None, period: str) -> list[Row]:
    stats = statistics_during_period(hass, start, end, {statistic_id}, period, None, {"state", "sum"})
    return [
        Row(dt_util.utc_from_timestamp(r["start"]), r.get("state"), float(r["sum"]))
        for r in stats.get(statistic_id) or []
        if r.get("sum") is not None
    ]


def load_existing(hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime) -> tuple[Existing, datetime | None]:
    """Bestaande sommenrijen rond ``[start, end)`` (in de executor), plus het begin van de laatste uurrij."""
    prev: Row | None = None
    for lookback in PREV_SEARCH:
        since = start - lookback if lookback else datetime(2000, 1, 1, tzinfo=start.tzinfo)
        before = _rows(hass, statistic_id, since, start, "hour")
        if before:
            prev = before[-1]
            break

    after = _rows(hass, statistic_id, start, None, "hour")
    last_start = after[-1].start if after else (prev.start if prev else None)
    if last_start is None:
        return Existing(None, None), None
    end = min(end, last_start)
    nxt = next((r for r in after if r.start >= end), None)
    hourly = [r.start for r in after if r.start < end]
    short_until = nxt.start if nxt else end
    short_term = [r.start for r in _rows(hass, statistic_id, start, short_until, "5minute")] if start < short_until else []
    return Existing(prev, nxt, hourly, short_term), last_start


def local_period(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Van het begin van ``start_date`` tot het eind van ``end_date`` (lokale tijd), in UTC."""
    return local_midnight(start_date), local_midnight(end_date + DAY)


async def _metadata(hass: HomeAssistant, metric: ImportableMetric) -> tuple[StatisticMetaData, Callable[[float], float]]:
    """Metadata van de statistiek (bestaand, of afgeleid van de sensor) en de omrekening vanaf de API-eenheid."""
    found = await get_instance(hass).async_add_executor_job(lambda: get_metadata(hass, statistic_ids={metric.entity_id}))
    converter = UNIT_CONVERTERS.get(metric.device_class)
    if metric.entity_id in found:
        metadata = found[metric.entity_id][1]
        if metadata.get("has_sum") != metric.has_sum or (
            not metric.has_sum and metadata.get("mean_type", StatisticMeanType.NONE) == StatisticMeanType.NONE
        ):
            raise HistoryImportError("statistics_type_mismatch", entity_id=metric.entity_id)
        unit = metadata.get("unit_of_measurement")
    else:
        state = hass.states.get(metric.entity_id)
        entry = er.async_get(hass).async_get(metric.entity_id)
        unit = (state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None) or (
            entry.unit_of_measurement if entry else None
        )
        unit = unit or metric.api_unit
        unit_class = converter.UNIT_CLASS if converter and unit in converter.VALID_UNITS else None
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE if metric.has_sum else StatisticMeanType.ARITHMETIC,
            has_sum=metric.has_sum,
            name=None,
            source="recorder",
            statistic_id=metric.entity_id,
            unit_class=unit_class,
            unit_of_measurement=unit,
        )

    if unit == metric.api_unit:
        return metadata, lambda value: value
    if converter and unit in converter.VALID_UNITS and metric.api_unit in converter.VALID_UNITS:
        return metadata, converter.converter_factory(metric.api_unit, unit)
    raise HistoryImportError("unit_mismatch", entity_id=metric.entity_id, unit=str(unit), api_unit=str(metric.api_unit))


@dataclass(slots=True)
class Reach:
    """Hoe ver terug een sensor per resolutie gaat (``None`` = onbeperkt, of geen bron bij ``day_uri``)."""

    fine_from: datetime | None  # eerste hele dag die per 5 minuten kan
    day_from: datetime | None  # eerste moment dat per dag kan
    day_uri: str | None  # metric voor dagresolutie (momentwaarde zelf, of de delta bij energie)


def reach(metric: ImportableMetric, limits: HistoryLimits, now: datetime) -> Reach:
    """De leeftijdsgrenzen van een sensor, uit de (bekende of geleerde) limieten."""

    def oldest(uri: str, granularity: str) -> datetime | None:
        ages = [limits.get(t, uri, granularity).max_age_days for t, _ in metric.sources]
        known = [a for a in ages if a is not None]
        return now - timedelta(days=min(known)) + DAY if known else None

    fine = oldest(metric.metric, "FIVE_MINUTES")
    fine_from = local_midnight(dt_util.as_local(fine).date() + DAY) if fine else None
    day_uri = metric.metric if not metric.has_sum else metric.delta_metric
    day_from = oldest(day_uri, "DAY") if day_uri else None
    return Reach(fine_from, day_from, day_uri)


async def async_import_metric(
    hass: HomeAssistant,
    client: SolarEdgeMonitoringClient,
    model: SiteModel,
    metric: ImportableMetric,
    start: datetime,
    end: datetime,
    interval: Interval,
    limits: HistoryLimits | None = None,
) -> MetricResult:
    """Importeer één sensor over ``[start, end)`` (UTC, hele uren)."""
    limits = limits or HistoryLimits(None, None)
    metadata, convert = await _metadata(hass, metric)
    now = dt_util.utcnow()
    recorder = get_instance(hass)
    notes: list[dict[str, Any]] = []
    span = reach(metric, limits, now)
    # 5-minutenrijen alleen binnen keep_days van de recorder (ouder ruimt hij op), op een heel uur
    short_from = (now - timedelta(days=getattr(recorder, "keep_days", 10))).replace(minute=0, second=0, microsecond=0) + HOUR
    fine_uri = metric.metric.split(".", 1)[1]

    async def fetch(ranges: list[tuple[datetime, datetime]], uri: str, granularity: str) -> list[GenerateChartResponse]:
        return await fetch_charts(
            client, model.site_id, metric, ranges, uri=uri, granularity=granularity, limits=limits, notes=notes
        )

    if interval in (HISTORY_INTERVAL_FIVE_MINUTES, HISTORY_INTERVAL_HOUR) and span.fine_from and start < span.fine_from - DAY:
        days = int((now - span.fine_from).days) + 1
        raise HistoryImportError("too_old_for_interval", days=str(days))
    if interval in (HISTORY_INTERVAL_DAY, HISTORY_INTERVAL_MAX):
        # Zo ver terug als dag (of, zonder dagbron, 5 minuten) gaat
        earliest = span.day_from if span.day_uri else (span.fine_from or start)
        if earliest and start < earliest:
            _note(
                notes, {"code": "age_limit", "metric": metric.metric, "granularity": "DAY", "from": earliest.date().isoformat()}
            )
            start = local_midnight(dt_util.as_local(earliest).date() + DAY)
    boundary = max(span.fine_from or start, start) if interval == HISTORY_INTERVAL_MAX else None

    if not metric.has_sum:
        if interval == HISTORY_INTERVAL_MAX:
            boundary = min(boundary or start, end)
            day_charts = await fetch([(start, boundary)], metric.metric, "DAY") if start < boundary else []
            fine_charts = await fetch([(boundary, end)], metric.metric, "FIVE_MINUTES") if boundary < end else []
            day_series = [(t, convert(v)) for t, v in metric_series(day_charts, fine_uri)]
            fine_series = [(t, convert(v)) for t, v in metric_series(fine_charts, fine_uri)]
            plan = build_gauge_import(day_series, start, boundary, HISTORY_INTERVAL_DAY, mean_only=True)
            hourly = build_gauge_import(fine_series, boundary, end, HISTORY_INTERVAL_HOUR)
            short_start = max(boundary, short_from)
            short = (
                build_gauge_import(fine_series, short_start, end, HISTORY_INTERVAL_FIVE_MINUTES) if short_start < end else None
            )
            plan = ImportPlan(plan.hourly + hourly.hourly, short.short_term if short else [])
        else:
            day_resolution = interval == HISTORY_INTERVAL_DAY
            granularity = "DAY" if day_resolution else "FIVE_MINUTES"
            charts = await fetch([(start, end)], metric.metric, granularity) if start < end else []
            series = [(t, convert(v)) for t, v in metric_series(charts, fine_uri)]
            plan = build_gauge_import(series, start, end, interval, mean_only=day_resolution)
        adjustment: float | None = None
    else:
        existing, last_start = await recorder.async_add_executor_job(load_existing, hass, metric.entity_id, start, end)
        if last_start is None:
            raise HistoryImportError("no_statistics_yet", entity_id=metric.entity_id)
        end = min(end, last_start)  # de recorder telt vanaf zijn laatste uurrij zelf verder
        if start >= end:
            return MetricResult(metric.entity_id, metric.name, 0, 0, 0.0, notes=notes)
        delta = metric.kind == "delta"
        if interval in (HISTORY_INTERVAL_DAY, HISTORY_INTERVAL_MAX) and metric.delta_metric is not None:
            ((fetch_start, fetch_end),) = fetch_ranges(start, end, existing, now, contiguous=True)
            # Per 5 minuten: bij max alles wat kan, en altijd dagen met bestaande uurrijen of een ankerpunt midden op de dag
            fine_moments = [*existing.hourly]
            for row in (existing.prev, existing.next):
                if row is not None and row.start + HOUR != local_midnight(dt_util.as_local(row.start + HOUR).date()):
                    fine_moments.append(row.start)
            fine_days = {dt_util.as_local(m).date() for m in fine_moments if not span.fine_from or m >= span.fine_from}
            if interval == HISTORY_INTERVAL_MAX:
                day = dt_util.as_local(max(boundary or fetch_start, fetch_start)).date()
                while local_midnight(day) < fetch_end:
                    fine_days.add(day)
                    day += DAY
            coarse_ranges, fine_ranges = day_segments(fetch_start, fetch_end, fine_days)
            uri = metric.delta_metric
            coarse = await fetch(coarse_ranges, uri, "DAY") if coarse_ranges else []
            # 5-minutendagen van een teller: de standen zelf (de delta's per 5 minuten lopen iets achter op de teller)
            fine_metric = metric.metric if metric.kind == "cumulative" else uri
            fine = await fetch(fine_ranges, fine_metric, "FIVE_MINUTES") if fine_ranges else []
            fine_name = fine_metric.split(".", 1)[1] if metric.kind == "cumulative" else None
            raw = delta_series(coarse, fine, uri.split(".", 1)[1], fine_uri=fine_name)
        else:
            if interval == HISTORY_INTERVAL_MAX and span.fine_from and start < span.fine_from:
                start = span.fine_from  # geen dagbron: alleen wat per 5 minuten kan
                _note(notes, {"code": "no_day_source", "metric": metric.metric})
            ranges = fetch_ranges(start, end, existing, now, contiguous=delta)
            charts = await fetch(ranges, metric.metric, "FIVE_MINUTES")
            raw = metric_series(charts, fine_uri, delta=delta)
        series = [(t, convert(v)) for t, v in raw]
        state_of = daily_total(series) if delta and series else None
        plan = build_import(series, start, end, interval, existing, state_of, boundary, short_from)
        adjustment = plan.sum_adjustment

    if plan.hourly:
        recorder.async_import_statistics(metadata, plan.hourly, Statistics)
    if plan.short_term:
        recorder.async_import_statistics(metadata, plan.short_term, StatisticsShortTerm)
    if plan.adjust_from is not None:
        recorder.async_adjust_statistics(
            metric.entity_id, plan.adjust_from, plan.sum_adjustment, metadata["unit_of_measurement"] or ""
        )
    first = min((r["start"] for r in plan.hourly), default=None)
    _LOGGER.info(
        "Historie %s – %s (%s) geïmporteerd voor %s: %d uurrijen, %d 5-minutenrijen, verschuiving %s, vanaf %s, meldingen %s",
        start,
        end,
        interval,
        metric.entity_id,
        len(plan.hourly),
        len(plan.short_term),
        adjustment,
        first,
        notes,
    )
    return MetricResult(metric.entity_id, metric.name, len(plan.hourly), len(plan.short_term), adjustment, first, notes)


async def async_import_history(
    hass: HomeAssistant,
    client: SolarEdgeMonitoringClient,
    model: SiteModel,
    metrics: list[ImportableMetric],
    start_date: date | None,
    end_date: date,
    interval: Interval = HISTORY_INTERVAL_HOUR,
    limits: HistoryLimits | None = None,
) -> ImportResult:
    """Importeer ``metrics`` van ``start_date`` t/m ``end_date`` (lokale dagen) met ``interval``.

    Zonder ``start_date`` begint de import bij de eerste productiedag van de installatie.
    """
    if "recorder" not in hass.config.components:
        raise HistoryImportError("recorder_not_running")
    if not metrics:
        raise HistoryImportError("no_importable_metrics")
    if start_date is None:
        availability = await client.dashboard.get_data_availability(model.site_id)
        start_date = availability.production_data_available_from or (end_date - timedelta(days=365))
    if start_date > end_date:
        raise HistoryImportError("invalid_period")

    start, end = local_period(start_date, end_date)
    end = min(end, dt_util.utcnow().replace(minute=0, second=0, microsecond=0))
    results = [await async_import_metric(hass, client, model, metric, start, end, interval, limits) for metric in metrics]
    return ImportResult(interval, start, max(start, end), results)

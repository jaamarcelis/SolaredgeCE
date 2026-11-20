"""Historie van *Energie totaal* ophalen bij SolarEdge en importeren in de recorderstatistieken.

SolarEdge levert de lifetime-teller van elke omvormer (``lifetime_positive_ac_energy``, Wh) per 5 minuten. Daaruit
worden rijen gemaakt voor de statistieken van de sensor *Energie totaal* van het site-device, met een instelbaar
interval:

- ``five_minutes``: kortetermijnstatistieken per 5 minuten (de recorder ruimt die na ``keep_days`` op) plus uurrijen;
- ``hour``: een uurrij per uur;
- ``day``: één uurrij per dag, in het laatste uur van de (lokale) dag, met de stand aan het eind van de dag.

Sommen sluiten aan op de bestaande rij vóór de periode. Bestaande rijen binnen de periode worden herschreven met
dezelfde reeks, en alle rijen erna worden verschoven, zodat grafieken en het Energy-dashboard zonder sprong doorlopen.
Dezelfde import nog een keer doen verandert dus niets. De periode eindigt uiterlijk bij de laatste uurrij van de
recorder: daar telt de recorder zelf verder.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import Any, Literal

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import Platform, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .api import ApiError, SolarEdgeMonitoringClient, measurement_datasource
from .api.models import GenerateChartResponse
from .const import DOMAIN, HISTORY_INTERVAL_DAY, HISTORY_INTERVAL_FIVE_MINUTES, HISTORY_INTERVAL_HOUR
from .discovery import SiteModel

_LOGGER = logging.getLogger(__name__)

LIFETIME_METRIC = ("measurements", "lifetime_positive_ac_energy")
CHUNK = timedelta(days=7)  # generate-chart weigert meer dan ~14 dagen per request (EXPORT_LIMIT_REACHED)
MIN_CHUNK = timedelta(days=1)
FIVE_MINUTES = timedelta(minutes=5)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
LOOKBACK = timedelta(days=1)  # 's nachts meet de omvormer niet: de stand van het begin komt van de dag ervoor
NEAR = timedelta(days=7)  # ankerpunten dichter bij de periode worden in dezelfde requests meegenomen
PREV_SEARCH = (timedelta(days=2), timedelta(days=35), timedelta(days=400), None)

type Interval = Literal["five_minutes", "hour", "day"]


class HistoryImportError(HomeAssistantError):
    """Import mislukt; ``translation_key`` verwijst naar ``exceptions`` in strings.json."""

    def __init__(self, key: str, **placeholders: Any) -> None:
        """Vertaalbare fout van deze integratie."""
        super().__init__(translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders or None)


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
    sum_adjustment: float  # op te tellen bij alle rijen vanaf ``adjust_from``
    adjust_from: datetime | None


@dataclass(slots=True)
class ImportResult:
    """Resultaat voor de actie-response en de melding."""

    statistic_id: str
    interval: Interval
    start: datetime
    end: datetime
    hourly_rows: int
    short_term_rows: int
    sum_adjustment: float

    def as_dict(self) -> dict[str, Any]:
        """Voor de response van de actie."""
        return {
            "statistic_id": self.statistic_id,
            "interval": self.interval,
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "hourly_rows": self.hourly_rows,
            "short_term_rows": self.short_term_rows,
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


def interval_starts(start: datetime, end: datetime, interval: Interval) -> tuple[list[datetime], list[datetime]]:
    """Begintijden van de eigen rijen in ``[start, end)``: (uurrijen, 5-minutenrijen).

    Bij ``day`` is dat het laatste uur van elke lokale dag, zodat de dagstand op het eind van de dag valt.
    """
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


def build_import(
    series: list[tuple[datetime, float]],
    start: datetime,
    end: datetime,
    interval: Interval,
    existing: Existing,
) -> ImportPlan:
    """Rijen voor ``[start, end)`` en de verschuiving van de rijen erna.

    - Som: aansluitend op de laatste rij vóór de periode, ``prev.sum + L(t) − L(eind prev)``. Zonder eerdere rij
      begint de som op 0 bij de eerste meting.
    - Stand: ``L(t) + offset``; de offset laat de stand aansluiten op de sensor (site- en omvormerteller wijken af).
    - Bestaande rijen binnen de periode worden met dezelfde reeks herschreven; rijen vanaf ``next`` verschoven.
    """
    if not series or start >= end:
        return ImportPlan([], [], 0.0, None)
    last_time = series[-1][0]
    first_time = series[0][0]

    def lifetime(moment: datetime) -> float | None:
        return value_at(series, moment)

    prev, nxt = existing.prev, existing.next
    prev_l = lifetime(prev.start + HOUR) if prev else None
    next_l = lifetime(nxt.start + HOUR) if nxt else None

    if nxt is not None and nxt.state is not None and next_l is not None:
        offset = nxt.state - next_l
    elif prev is not None and prev.state is not None and prev_l is not None:
        offset = prev.state - prev_l
    else:
        offset = 0.0

    if prev is not None and prev_l is not None:
        anchor_l, anchor_sum = prev_l, prev.sum
    elif prev is not None and prev.state is not None:
        anchor_l, anchor_sum = prev.state - offset, prev.sum  # geen meting bij prev: via de stand
    else:
        start_l = lifetime(start)
        anchor_l = start_l if start_l is not None else next((v for t, v in series if t >= start), series[-1][1])
        anchor_sum = 0.0

    def row(row_start: datetime, length: timedelta) -> StatisticData | None:
        row_end = row_start + length
        if row_end <= first_time or row_start >= last_time:
            return None  # geen meting in of vóór deze rij, of al voorbij de laatste
        value = lifetime(row_end)
        if value is None:
            return None
        return StatisticData(start=row_start, state=round(value + offset, 3), sum=round(anchor_sum + value - anchor_l, 3))

    own_hourly, own_short = interval_starts(start, end, interval)
    hourly_starts = sorted(set(own_hourly) | {t for t in existing.hourly if start <= t < end})
    short_limit = nxt.start if nxt else end
    short_starts = sorted(set(own_short) | {t for t in existing.short_term if start <= t < short_limit})

    hourly = [r for t in hourly_starts if (r := row(t, HOUR))]
    short_term = [r for t in short_starts if (r := row(t, FIVE_MINUTES))]

    adjustment = 0.0
    if nxt is not None and next_l is not None and (hourly or short_term):
        adjustment = round(anchor_sum + next_l - anchor_l - nxt.sum, 3)
    return ImportPlan(hourly, short_term, adjustment, nxt.start if nxt and adjustment else None)


def chunks(start: datetime, end: datetime, size: timedelta = CHUNK) -> list[tuple[datetime, datetime]]:
    """``[start, end)`` in blokken van hoogstens ``size``."""
    result = []
    while start < end:
        result.append((start, min(start + size, end)))
        start += size
    return result


def fetch_ranges(start: datetime, end: datetime, existing: Existing, now: datetime) -> list[tuple[datetime, datetime]]:
    """Op te halen bereiken: de periode (met een dag ervoor) plus kleine stukken rond verre ankerpunten."""
    main_start, main_end = start - LOOKBACK, end
    extra: list[tuple[datetime, datetime]] = []
    if existing.prev is not None:
        prev_end = existing.prev.start + HOUR
        if prev_end >= start - NEAR:
            main_start = min(main_start, prev_end - LOOKBACK)
        else:
            extra.append((prev_end - LOOKBACK, prev_end + FIVE_MINUTES))
    if existing.next is not None:
        next_end = existing.next.start + HOUR
        if next_end <= end + NEAR:
            main_end = max(main_end, next_end + FIVE_MINUTES)
        else:
            extra.append((next_end - LOOKBACK, next_end + FIVE_MINUTES))
    ranges = [(main_start, main_end), *extra]
    return [(s, min(e, now)) for s, e in ranges if s < now]


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
    client: SolarEdgeMonitoringClient, model: SiteModel, ranges: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, float]]:
    """De lifetime per 5 minuten over ``ranges``, in blokken van 7 dagen (kleiner bij EXPORT_LIMIT_REACHED)."""
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
    pending = [c for start, end in ranges for c in chunks(start, end)]
    while pending:
        chunk_start, chunk_end = pending.pop(0)
        try:
            charts.append(await client.analysis.generate_chart(model.site_id, chunk_start, chunk_end, datasources))
        except ApiError as err:
            size = chunk_end - chunk_start
            if err.code != "EXPORT_LIMIT_REACHED" or size <= MIN_CHUNK:
                raise
            pending[:0] = chunks(chunk_start, chunk_end, max(size / 2, MIN_CHUNK))
    _LOGGER.debug("Historie opgehaald in %d blokken (%s)", len(charts), ranges)
    return lifetime_series(charts)


def _rows(hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime | None, period: str) -> list[Row]:
    stats = statistics_during_period(
        hass, start, end, {statistic_id}, period, {"energy": UnitOfEnergy.KILO_WATT_HOUR}, {"state", "sum"}
    )
    return [
        Row(dt_util.utc_from_timestamp(r["start"]), r.get("state"), float(r["sum"]))
        for r in stats.get(statistic_id) or []
        if r.get("sum") is not None
    ]


def load_existing(hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime) -> tuple[Existing, datetime | None]:
    """Bestaande rijen rond ``[start, end)`` (in de executor), plus het begin van de laatste uurrij."""
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
    return (
        dt_util.as_utc(dt_util.start_of_local_day(start_date)),
        dt_util.as_utc(dt_util.start_of_local_day(end_date + DAY)),
    )


async def async_import_history(
    hass: HomeAssistant,
    client: SolarEdgeMonitoringClient,
    model: SiteModel,
    start_date: date,
    end_date: date,
    interval: Interval = HISTORY_INTERVAL_HOUR,
) -> ImportResult:
    """Haal de historie van ``start_date`` t/m ``end_date`` op en importeer die in de statistieken van *Energie totaal*."""
    if "recorder" not in hass.config.components:
        raise HistoryImportError("recorder_not_running")
    if start_date > end_date:
        raise HistoryImportError("invalid_period")
    statistic_id = site_energy_entity_id(hass, model)

    now = dt_util.utcnow()
    start, end = local_period(start_date, end_date)
    end = min(end, now.replace(minute=0, second=0, microsecond=0))
    existing, last_start = await get_instance(hass).async_add_executor_job(load_existing, hass, statistic_id, start, end)
    if last_start is None:
        raise HistoryImportError("no_statistics_yet", entity_id=statistic_id)
    end = min(end, last_start)  # de recorder telt vanaf zijn laatste uurrij zelf verder
    if start >= end:
        return ImportResult(statistic_id, interval, start, max(start, end), 0, 0, 0.0)

    series = await fetch_lifetime(client, model, fetch_ranges(start, end, existing, now))
    if not series:
        raise HistoryImportError("no_history_data")
    plan = build_import(series, start, end, interval, existing)

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=statistic_id,
        unit_class="energy",
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    recorder = get_instance(hass)
    if plan.hourly:
        recorder.async_import_statistics(metadata, plan.hourly, Statistics)
    if plan.short_term:
        recorder.async_import_statistics(metadata, plan.short_term, StatisticsShortTerm)
    if plan.adjust_from is not None:
        recorder.async_adjust_statistics(statistic_id, plan.adjust_from, plan.sum_adjustment, UnitOfEnergy.KILO_WATT_HOUR)
    _LOGGER.info(
        "Historie %s – %s (%s) geïmporteerd voor %s: %d uurrijen, %d 5-minutenrijen; sommen erna verschoven met %.3f kWh",
        start,
        end,
        interval,
        statistic_id,
        len(plan.hourly),
        len(plan.short_term),
        plan.sum_adjustment,
    )
    return ImportResult(statistic_id, interval, start, end, len(plan.hourly), len(plan.short_term), plan.sum_adjustment)

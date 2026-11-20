"""Statistieken importeren: welke sensoren historie hebben, de pure berekeningen en de import in de recorder."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import itertools
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.solaredge_monitoring.api import ApiError
from custom_components.solaredge_monitoring.api.models import GenerateChartResponse, parse
from custom_components.solaredge_monitoring.const import DOMAIN
from custom_components.solaredge_monitoring.history_limits import HistoryLimits
from custom_components.solaredge_monitoring.statistics_import import (
    Existing,
    ImportableMetric,
    Row,
    build_gauge_import,
    build_import,
    chunks,
    daily_total,
    day_segments,
    delta_series,
    fetch_charts,
    fetch_ranges,
    importable_metrics,
    interval_starts,
    metric_series,
)
from tests.fake_session import Call, FakeResponse, FakeSession
from tests.helpers import SITE_ID
from tests.integration.conftest import build_session, site_responses
from tests.integration.test_setup import entity_id, setup

T = datetime(2026, 8, 1, tzinfo=UTC)
H = timedelta(hours=1)
M5 = timedelta(minutes=5)
LIFETIME_START_WH = 22_000_000  # stand aan het begin van de nep-historie
WH_PER_5MIN = 100  # 1,2 kWh per uur
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
LIFETIME = "measurements.lifetime_positive_ac_energy"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: Any, enable_custom_integrations: None) -> None:
    """Zoals in conftest, maar met de recorder vóór ``hass`` (anders weigert de recorder-fixture)."""


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def chart(
    points: dict[str, list[tuple[datetime, float | None]]], uri: str = "lifetime_positive_ac_energy"
) -> GenerateChartResponse:
    """Nep-generate-chart met per apparaat de waarden van ``uri``."""
    raw = {
        "meta": {
            "datasetsMeta": [
                {
                    "reportObject": {"entityType": "INVERTER", "entityId": device},
                    "metrics": [{"uri": uri}],
                    "datasetName": "INVERTER:measurements",
                }
                for device in points
            ]
        },
        "data": [[[_iso(t), v] for t, v in series] for series in points.values()],
    }
    return parse(GenerateChartResponse, raw)


# --- Pure functies ------------------------------------------------------------------------------


def test_metric_series_sums_devices_with_forward_fill() -> None:
    series = metric_series(
        [chart({"A": [(T, 1000), (T + M5, None), (T + 2 * M5, 1200)], "B": [(T + M5, 5000), (T + 2 * M5, None)]})],
        "lifetime_positive_ac_energy",
    )
    # T zelf ontbreekt: B heeft daar nog geen waarde
    assert series == [(T + M5, 6000.0), (T + 2 * M5, 6200.0)]


def test_metric_series_delta_is_running_total() -> None:
    series = metric_series(
        [chart({"A": [(T, 10), (T + M5, 20)], "B": [(T + M5, 5)]}, "energy"), chart({"A": [(T + 2 * M5, 1)]}, "energy")],
        "energy",
        delta=True,
    )
    assert series == [(T, 10.0), (T + M5, 35.0), (T + 2 * M5, 36.0)]


def test_chunks() -> None:
    assert chunks(T, T + timedelta(days=15), timedelta(days=7)) == [
        (T, T + timedelta(days=7)),
        (T + timedelta(days=7), T + timedelta(days=14)),
        (T + timedelta(days=14), T + timedelta(days=15)),
    ]


def _series(hours: int, start: float = 100.0, begin: datetime = T) -> list[tuple[datetime, float]]:
    """Elke 5 minuten 0,1 erbij (1,2 per uur)."""
    return [(begin + i * M5, round(start + 0.1 * i, 3)) for i in range(hours * 12 + 1)]


def test_build_import_without_previous_rows() -> None:
    # Eén bestaande rij om 04:00 (de recorder zag een deel van het uur: som 0,5); site-teller 86 hoger
    nxt = Row(T + 4 * H, state=100 + 6.0 + 86, sum=0.5)
    plan = build_import(_series(6), T, T + 4 * H, "hour", Existing(None, nxt))

    assert [r["start"] for r in plan.hourly] == [T + h * H for h in range(4)]
    assert [r["sum"] for r in plan.hourly] == pytest.approx([1.2, 2.4, 3.6, 4.8])
    assert [r["state"] for r in plan.hourly] == pytest.approx([187.2, 188.4, 189.6, 190.8])
    assert plan.short_term == []
    assert plan.adjust_from == nxt.start
    assert plan.sum_adjustment == pytest.approx(6.0 - 0.5)


def test_build_import_continues_previous_row_and_rewrites_existing() -> None:
    prev = Row(T, state=101.2 + 86, sum=50.0)  # eind 01:00, L = 101,2
    nxt = Row(T + 4 * H, state=106.0 + 86, sum=10.0)  # som uit een oude reeks
    existing = Existing(prev, nxt, hourly=[T + 2 * H])
    plan = build_import(_series(6), T + H, T + 4 * H, "hour", existing)

    assert [r["start"] for r in plan.hourly] == [T + H, T + 2 * H, T + 3 * H]
    assert [r["sum"] for r in plan.hourly] == pytest.approx([51.2, 52.4, 53.6])
    assert plan.sum_adjustment == pytest.approx(54.8 - 10.0)


def test_build_import_same_data_twice_changes_nothing() -> None:
    prev = Row(T, state=101.2, sum=50.0)
    nxt = Row(T + 4 * H, state=106.0, sum=54.8)
    plan = build_import(_series(6), T + H, T + 4 * H, "hour", Existing(prev, nxt))
    assert plan.sum_adjustment == 0
    assert plan.adjust_from is None


def test_build_import_five_minutes_writes_short_term_and_hours() -> None:
    plan = build_import(_series(3), T, T + 2 * H, "five_minutes", Existing(None, Row(T + 2 * H, None, 0.0)))
    assert len(plan.hourly) == 2
    assert len(plan.short_term) == 24
    assert plan.short_term[0]["sum"] == pytest.approx(0.1)
    assert plan.short_term[11]["sum"] == plan.hourly[0]["sum"]


def test_build_import_day_rows_at_last_local_hour(hass: HomeAssistant) -> None:
    day = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 8, 1)))
    nxt = Row(day + timedelta(days=3), state=None, sum=0.0)
    plan = build_import(_series(24 * 3, begin=day), day, day + timedelta(days=3), "day", Existing(None, nxt))
    assert [r["start"] for r in plan.hourly] == [day + timedelta(days=d + 1) - H for d in range(3)]
    assert [r["sum"] for r in plan.hourly] == pytest.approx([28.8, 57.6, 86.4])


def test_build_import_daily_total_state(hass: HomeAssistant) -> None:
    day = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 8, 1)))
    series = _series(48, start=0.0, begin=day - H)
    plan = build_import(series, day, day + 2 * timedelta(days=1), "hour", Existing(None, None), daily_total(series))
    states = [r["state"] for r in plan.hourly]
    assert states[0] == pytest.approx(1.2)
    assert states[23] == pytest.approx(28.8)  # laatste uur van de dag: dagtotaal
    assert states[24] == pytest.approx(1.2)  # nieuwe dag begint opnieuw
    assert [r["sum"] for r in plan.hourly][24] == pytest.approx(30.0)  # de som loopt door


def test_build_import_skips_rows_before_first_measurement() -> None:
    series = [(t, v) for t, v in _series(6) if t >= T + 2 * H]
    plan = build_import(series, T, T + 4 * H, "hour", Existing(None, Row(T + 4 * H, None, 0.0)))
    assert [r["start"] for r in plan.hourly] == [T + 2 * H, T + 3 * H]
    assert [r["sum"] for r in plan.hourly] == pytest.approx([1.2, 2.4])


def test_build_gauge_import(hass: HomeAssistant) -> None:
    series = [(T + i * M5, float(i % 12)) for i in range(24)]
    plan = build_gauge_import(series, T, T + 2 * H, "five_minutes")
    assert [(r["mean"], r["min"], r["max"]) for r in plan.hourly] == [(5.5, 0.0, 11.0), (5.5, 0.0, 11.0)]
    assert len(plan.short_term) == 24
    assert plan.short_term[3] == {"start": T + 3 * M5, "mean": 3.0, "min": 3.0, "max": 3.0}

    day = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 8, 1)))
    series = [(day + i * M5, 1.0 if i < 288 else 3.0) for i in range(576)]
    plan = build_gauge_import(series, day, day + timedelta(days=2), "day")
    assert [(r["start"], r["mean"]) for r in plan.hourly] == [(day, 1.0), (day + timedelta(days=1), 3.0)]


def test_interval_starts_five_minutes() -> None:
    hourly, short_term = interval_starts(T, T + 2 * H, "five_minutes")
    assert hourly == [T, T + H]
    assert len(short_term) == 24


def test_fetch_ranges() -> None:
    now = T + timedelta(days=60)
    existing = Existing(Row(T - timedelta(days=30), None, 0.0), Row(T + timedelta(days=20), None, 0.0))
    ranges = fetch_ranges(T, T + timedelta(days=3), existing, now)
    assert ranges[0] == (T - timedelta(days=1), T + timedelta(days=3))
    assert len(ranges) == 3
    # Delta's moeten aaneensluiten: één bereik van vóór prev tot na next
    (contiguous,) = fetch_ranges(T, T + timedelta(days=3), existing, now, contiguous=True)
    assert contiguous == (T - timedelta(days=31) + H, T + timedelta(days=20) + H + M5)


async def test_fetch_charts_halves_chunk_on_export_limit() -> None:
    metric = ImportableMetric("u", "sensor.x", "X", LIFETIME, "cumulative", "Wh", None, (("ONE_PHASE_INVERTER", ("A",)),))
    client = MagicMock()
    ranges: list[tuple[datetime, datetime]] = []

    async def generate_chart(_site: int, start: datetime, end: datetime, _ds: list[Any]) -> GenerateChartResponse:
        if end - start > timedelta(days=4):
            raise ApiError("te groot", 400, "EXPORT_LIMIT_REACHED")
        ranges.append((start, end))
        return chart({"A": [(start, 1000)]})

    client.analysis.generate_chart = AsyncMock(side_effect=generate_chart)
    charts = await fetch_charts(client, SITE_ID, metric, [(T, T + timedelta(days=7))])
    assert ranges == [(T, T + timedelta(days=3.5)), (T + timedelta(days=3.5), T + timedelta(days=7))]
    assert len(charts) == 2


# --- Met Home Assistant en de recorder ----------------------------------------------------------


def fake_value(uri: str, moment: datetime) -> float:
    """Nepwaarden: oplopende teller, vaste delta's, en een zaagtand per uur voor momentwaarden."""
    if uri.startswith("lifetime"):
        return LIFETIME_START_WH + WH_PER_5MIN * int((moment - EPOCH) / M5)
    if "since_last" in uri:
        return WH_PER_5MIN
    return 230.0 + moment.minute / 5


def fake_day_value(uri: str, day_start: datetime) -> float:
    """Dagresolutie zoals SolarEdge: tellers en momentwaarden als daggemiddelde, delta's als dagtotaal."""
    day_end = local_midnight(dt_util.as_local(day_start).date() + timedelta(days=1))
    moments = [day_start + i * M5 for i in range(int((day_end - day_start) / M5))]
    values = [fake_value(uri, t) for t in moments]
    return sum(values) if "since_last" in uri else sum(values) / len(values)


def _days(start: datetime, end: datetime) -> list[date]:
    day = dt_util.as_local(start).date()
    result = []
    while local_midnight(day) < end:
        result.append(day)
        day += timedelta(days=1)
    return result


FAKE_FINE_AGE = {"days": 730}  # leeftijdsgrens van 5-minutendata van omvormers in de nep-API


def fake_limit(device_type: str, granularity: str, start: datetime, end: datetime) -> dict[str, str] | None:
    """De limieten die SolarEdge hanteert (zie docs/historie-limieten.md), met de echte foutteksten."""
    now = dt_util.utcnow()
    days = FAKE_FINE_AGE["days"]
    if device_type == "OPTIMIZER":
        if start < now - timedelta(days=90):
            message = "Optimizer x data older than 90 days is not available in the chart view. Use the export tool."
            return {"errorCode": "DATASET_TOO_LARGE", "message": message}
        if granularity != "DAY" and end - start > timedelta(days=14):
            message = "Optimizer x data at hourly or finer resolution is limited to 14 days. Please select a shorter date range."
            return {"errorCode": "EXPORT_LIMIT_REACHED", "message": message}
    elif granularity != "DAY" and start < now - timedelta(days=days):
        message = f"One phase inverter x data at hourly or finer resolution is only available for the last {days} days."
        return {"errorCode": "EXPORT_LIMIT_REACHED", "message": message}
    return None


def history_session(available_from: date | None = None) -> FakeSession:
    """Nep-API waarin generate-chart voor elk gevraagd apparaat en elke metric 5-minutenwaarden geeft."""
    session = build_session(site_responses())
    requests: list[tuple[datetime, datetime, str]] = []

    def respond(call: Call) -> FakeResponse:
        body = call.json
        start = dt_util.parse_datetime(body["reportPeriod"]["from"])
        end = dt_util.parse_datetime(body["reportPeriod"]["to"])
        granularity = body["datasources"][0]["alignmentGranularity"]
        requests.append((start, end, granularity))
        device_type = body["datasources"][0]["datasourcePopulation"]["deviceType"]
        if error := fake_limit(device_type, granularity, start, end):
            return FakeResponse(400, json.dumps(error))
        if granularity != "DAY" and end - start > timedelta(days=31):
            granularity = "DAY"  # zoals SolarEdge: stilletjes dagwaarden
            for datasource in body["datasources"]:
                datasource["alignmentGranularity"] = "DAY"
        moments = []
        moment = start
        while moment < end:
            moments.append(moment)
            moment += M5
        metas, data = [], []
        for datasource in body["datasources"]:
            uris = [m["uri"] for m in datasource["metrics"]]
            if datasource["alignmentGranularity"] == "DAY":
                days = [local_midnight(d) for d in _days(start, end)]
                rows = [[_iso(t), *(fake_day_value(u, t) for u in uris)] for t in days]
            else:
                rows = [[_iso(t), *(fake_value(u, t) for u in uris)] for t in moments]
            category = datasource["metrics"][0]["metricsCategoryName"]
            population = datasource["datasourcePopulation"]
            for serial in population["deviceSerials"]:
                metas.append(
                    {
                        "reportObject": {"entityType": population["deviceType"], "entityId": serial},
                        "metrics": [{"uri": u} for u in uris],
                        "datasetName": f"{population['deviceType']}:{category}",
                    }
                )
                data.append(rows)
        return FakeResponse(200, json.dumps({"meta": {"datasetsMeta": metas}, "data": data}))

    session.add_callback("POST", r"generate-chart$", respond)
    session._routes.insert(0, session._routes.pop())  # vóór de standaard-fixture
    first_day = available_from or dt_util.now().date() - timedelta(days=1000)
    session.add("GET", r"data-availability/sites", json_body={"productionDataAvailableFrom": first_day.isoformat()})
    session._routes.insert(0, session._routes.pop())
    session.history_requests = requests  # type: ignore[attr-defined]
    return session


async def existing_rows(
    hass: HomeAssistant, statistic_id: str, t0: datetime, hours: int, unit: str = "kWh", state_offset: float = 86
) -> None:
    """Bestaande uurrijen zoals de recorder ze maakt: som vanaf 0,5, stand van de site-teller."""
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=statistic_id,
        unit_class="energy",
        unit_of_measurement=unit,
    )
    factor = 1000 if unit == "kWh" else 1
    rows = [
        {
            "start": t0 + h * H,
            "state": fake_value("lifetime", t0 + (h + 1) * H) / factor + state_offset,
            "sum": 0.5 + 1200 / factor * h,
        }
        for h in range(hours)
    ]
    async_import_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)


async def stats(
    hass: HomeAssistant, statistic_id: str, start: datetime, period: str = "hour", types: set[str] | None = None
) -> list[dict[str, Any]]:
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {statistic_id}, period, None, types or {"state", "sum"}
    )
    return result.get(statistic_id, [])


def device_id(hass: HomeAssistant, entry: MockConfigEntry, identifier: str) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, identifier), entry.entry_id)
    assert device
    return device.id


async def import_statistics(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(DOMAIN, "import_statistics", data, blocking=True, return_response=True)
    await async_wait_recording_done(hass)
    return response


def assert_consistent(rows: list[dict[str, Any]], per_hour: float = 1.2) -> None:
    """Som en stand lopen met ``per_hour`` per uur op, ook over gaten heen."""
    for a, b in itertools.pairwise(rows):
        hours = (b["start"] - a["start"]) / 3600
        assert b["sum"] - a["sum"] == pytest.approx(per_hour * hours), (a, b)
        assert b["state"] - a["state"] == pytest.approx(per_hour * hours), (a, b)


def local_midnight(day: date) -> datetime:
    return dt_util.as_utc(dt_util.start_of_local_day(day))


async def test_importable_metrics_per_device(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    model = config_entry.runtime_data.model

    site = importable_metrics(hass, model, device_id(hass, config_entry, f"site_{SITE_ID}"))
    assert {m.unique_id.removeprefix(f"{SITE_ID}_site_"): m.kind for m in site} == {
        "energy_lifetime": "cumulative",
        "energy_today": "delta",
        "current_power": "gauge",
    }  # ac_power: active_power zit niet in de ondersteunde metrics van deze omvormer

    inverter = importable_metrics(hass, model, device_id(hass, config_entry, "ABCD1234-EF"))
    keys = {m.unique_id.removeprefix(f"{SITE_ID}_ABCD1234-EF_"): m.kind for m in inverter}
    assert keys["power"] == "gauge"
    assert keys["energy_today"] == "delta"
    assert keys["metric_measurements.ac_voltage"] == "gauge"
    assert keys[f"metric_{LIFETIME}"] == "cumulative"
    assert "metric_measurements.status" not in keys  # tekst: geen statistieken

    optimizer = importable_metrics(hass, model, device_id(hass, config_entry, "11112222-AB"))
    assert {m.unique_id.removeprefix(f"{SITE_ID}_11112222-AB_"): m.metric for m in optimizer} == {
        "power": "measurements.output_power",
        "panel_voltage": "measurements.panel_voltage",
        "energy_today": "measurements.energy_since_last_telem",
    }
    assert optimizer[0].sources == (("OPTIMIZER", ("11112222",)),)


async def test_import_site_energy_hour(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    session = history_session()
    await setup(hass, config_entry, session)
    statistic_id = entity_id(hass, f"{SITE_ID}_site_energy_lifetime")
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - 4 * H
    await existing_rows(hass, statistic_id, t0, 3)
    site = device_id(hass, config_entry, f"site_{SITE_ID}")

    start_date = dt_util.now().date() - timedelta(days=10)
    start = local_midnight(start_date)
    response = await import_statistics(hass, device_id=site, entity_id=[statistic_id], start=start_date.isoformat())

    end = t0 + 2 * H  # eindigt bij de laatste uurrij van de recorder
    assert response["interval"] == "hour"
    assert response["from"] == start.isoformat()
    (result,) = response["metrics"]
    assert result["entity_id"] == statistic_id
    assert result["hourly_rows"] == int((end - start) / H)
    assert result["short_term_rows"] == 0
    imports = [r for r in session.history_requests if r[1] - r[0] > 3 * H]  # zonder de telemetrie-requests
    assert len(imports) == 1  # 11 dagen passen in één blok van 31

    rows = await stats(hass, statistic_id, start)
    assert len(rows) == result["hourly_rows"] + 1
    assert rows[0]["sum"] == pytest.approx(1.2)
    assert_consistent(rows)
    assert rows[-1]["state"] == pytest.approx(fake_value("lifetime", end + H) / 1000 + 86)

    # Nog een keer: dezelfde waarden, niets verschoven
    again = await import_statistics(hass, device_id=site, entity_id=[statistic_id], start=start_date.isoformat())
    assert again["metrics"][0]["sum_adjustment"] == 0

    # Een eerdere periode per dag, met een gat ertussen, sluit ook aan
    earlier = await import_statistics(
        hass,
        device_id=site,
        entity_id=[statistic_id],
        start=(start_date - timedelta(days=5)).isoformat(),
        end=(start_date - timedelta(days=3)).isoformat(),
        interval="day",
    )
    assert earlier["metrics"][0]["hourly_rows"] == 3
    # Per dag opgehaald, behalve de dag waar de reeks op de bestaande uurrijen aansluit
    day_requests = [r for r in session.history_requests if r[2] == "DAY"]
    assert day_requests
    assert day_requests[0][0] == local_midnight(start_date - timedelta(days=6))
    rows = await stats(hass, statistic_id, start - timedelta(days=5))
    assert len(rows) == result["hourly_rows"] + 1 + 3
    assert_consistent(rows)


async def test_import_inverter_gauge_five_minutes(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    statistic_id = entity_id(hass, f"{SITE_ID}_ABCD1234-EF_metric_measurements.ac_voltage")
    start_date = dt_util.now().date() - timedelta(days=1)
    response = await import_statistics(
        hass,
        device_id=device_id(hass, config_entry, "ABCD1234-EF"),
        entity_id=[statistic_id],
        start=start_date.isoformat(),
        interval="five_minutes",
    )
    (result,) = response["metrics"]
    assert "sum_adjustment" not in result
    assert result["short_term_rows"] == result["hourly_rows"] * 12

    start = local_midnight(start_date)
    hourly = await stats(hass, statistic_id, start, types={"mean", "min", "max"})
    assert len(hourly) == result["hourly_rows"]
    assert (hourly[0]["mean"], hourly[0]["min"], hourly[0]["max"]) == pytest.approx((235.5, 230.0, 241.0))
    short_term = await stats(hass, statistic_id, start, "5minute", {"mean"})
    assert short_term[1]["mean"] == pytest.approx(231.0)


async def test_import_inverter_energy_today_resets_daily(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    statistic_id = entity_id(hass, f"{SITE_ID}_ABCD1234-EF_energy_today")
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - 2 * H
    await existing_rows(hass, statistic_id, t0, 1, unit="Wh", state_offset=0)

    start_date = dt_util.now().date() - timedelta(days=3)
    await import_statistics(
        hass,
        device_id=device_id(hass, config_entry, "ABCD1234-EF"),
        entity_id=[statistic_id],
        start=start_date.isoformat(),
        end=(start_date + timedelta(days=1)).isoformat(),
    )
    rows = await stats(hass, statistic_id, local_midnight(start_date))
    assert len(rows) == 48 + 1  # twee dagen plus de bestaande rij
    assert rows[0]["state"] == pytest.approx(1200)
    assert rows[23]["state"] == pytest.approx(28800)  # dagtotaal
    assert rows[24]["state"] == pytest.approx(1200)  # nieuwe dag
    for a, b in itertools.pairwise(rows):
        assert b["sum"] - a["sum"] == pytest.approx(1200 * (b["start"] - a["start"]) / 3600)


async def test_import_all_metrics_of_optimizer(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    power = entity_id(hass, f"{SITE_ID}_11112222-AB_power")
    energy = entity_id(hass, f"{SITE_ID}_11112222-AB_energy_today")
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - 2 * H
    await existing_rows(hass, energy, t0, 1, unit="Wh", state_offset=0)

    response = await import_statistics(
        hass,
        device_id=device_id(hass, config_entry, "11112222-AB"),
        start=(dt_util.now().date() - timedelta(days=1)).isoformat(),
    )
    assert {m["entity_id"] for m in response["metrics"]} == {
        power,
        energy,
        entity_id(hass, f"{SITE_ID}_11112222-AB_panel_voltage"),
    }
    assert all(m["hourly_rows"] > 0 for m in response["metrics"])


async def test_import_errors(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    site = device_id(hass, config_entry, f"site_{SITE_ID}")
    inverter = device_id(hass, config_entry, "ABCD1234-EF")
    lifetime = entity_id(hass, f"{SITE_ID}_site_energy_lifetime")

    async def call(**data: Any) -> None:
        await hass.services.async_call(DOMAIN, "import_statistics", data, blocking=True)

    with pytest.raises(ServiceValidationError) as err:
        await call(device_id=inverter, entity_id=[lifetime], start="2026-01-01")
    assert err.value.translation_key == "metric_not_importable"

    other = dr.async_get(hass).async_get_or_create(config_entry_id=config_entry.entry_id, identifiers={("x", "y")})
    with pytest.raises(ServiceValidationError) as err:
        await call(device_id=other.id, start="2026-01-01")
    assert err.value.translation_key == "no_importable_metrics"

    with pytest.raises(ServiceValidationError) as err:
        await call(device_id=site, start="2026-02-01", end="2026-01-01")
    assert err.value.translation_key == "invalid_period"

    with pytest.raises(HomeAssistantError) as err:
        await call(device_id=site, entity_id=[lifetime], start="2026-01-01")
    assert err.value.translation_key == "no_statistics_yet"


async def test_options_flow_import(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    optimizer = device_id(hass, config_entry, "11112222-AB")
    power = entity_id(hass, f"{SITE_ID}_11112222-AB_power")

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "import_device"})
    assert result["step_id"] == "import_device"
    devices = next(v for k, v in result["data_schema"].schema.items() if str(k) == "device")
    assert optimizer in {o["value"] for o in devices.config["options"]}

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"device": optimizer})
    assert result["step_id"] == "import_metrics"
    metrics = next(v for k, v in result["data_schema"].schema.items() if str(k) == "metrics")
    # Alleen de sensoren van deze optimizer
    assert {o["value"] for o in metrics.config["options"]} == {
        power,
        entity_id(hass, f"{SITE_ID}_11112222-AB_panel_voltage"),
        entity_id(hass, f"{SITE_ID}_11112222-AB_energy_today"),
    }

    start = (dt_util.now().date() - timedelta(days=1)).isoformat()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"metrics": [power], "interval": "hour", "start": start}
    )
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "import_finished"
    assert "hourly rows" in result["description_placeholders"]["summary"]
    await async_wait_recording_done(hass)
    assert await stats(hass, power, local_midnight(dt_util.now().date() - timedelta(days=1)), types={"mean"})


async def test_options_flow_rejects_invalid_period(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "import_device"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": device_id(hass, config_entry, "11112222-AB")}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "metrics": [entity_id(hass, f"{SITE_ID}_11112222-AB_power")],
            "interval": "hour",
            "start": "2026-02-01",
            "end": "2026-01-01",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"end": "invalid_period"}


async def test_button_uses_options(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=config_entry.title,
        unique_id=config_entry.unique_id,
        data=dict(config_entry.data),
        options={"history_days": 2, "history_interval": "day"},
    )
    await setup(hass, entry, history_session())
    statistic_id = entity_id(hass, f"{SITE_ID}_site_energy_lifetime")
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - 3 * H
    await existing_rows(hass, statistic_id, t0, 2)

    button = entity_id(hass, f"{SITE_ID}_site_import_energy_history", "button")
    with patch("custom_components.solaredge_monitoring.services.persistent_notification.async_create") as notify:
        await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    notify.assert_called_once()
    assert "history imported" in notify.call_args.args[2]
    today = dt_util.now().date()
    rows = await stats(hass, statistic_id, local_midnight(today - timedelta(days=2)))
    # Dag-interval: de eerste rij is het laatste uur van eergisteren
    assert rows[0]["start"] == (local_midnight(today - timedelta(days=1)) - H).timestamp()
    assert_consistent(rows)


def test_day_segments(hass: HomeAssistant) -> None:
    day = date(2026, 8, 1)
    start, end = local_midnight(day), local_midnight(day + timedelta(days=5)) + 3 * H
    coarse, fine = day_segments(start, end, {day + timedelta(days=2), day + timedelta(days=5)})
    assert coarse == [
        (local_midnight(day), local_midnight(day + timedelta(days=2))),
        (local_midnight(day + timedelta(days=3)), local_midnight(day + timedelta(days=5))),
    ]
    assert fine == [
        (local_midnight(day + timedelta(days=2)), local_midnight(day + timedelta(days=3))),
        (local_midnight(day + timedelta(days=5)), end),
    ]


def test_delta_series_puts_day_totals_at_day_end(hass: HomeAssistant) -> None:
    day = local_midnight(date(2026, 8, 1))
    next_day = local_midnight(date(2026, 8, 2))
    coarse = [chart({"A": [(day, 1000)], "B": [(day, 500)]}, "energy")]
    fine = [chart({"A": [(next_day + M5, 10), (next_day + 2 * M5, 20)]}, "energy")]
    assert delta_series(coarse, fine, "energy") == [(next_day, 1500.0), (next_day + M5, 1510.0), (next_day + 2 * M5, 1530.0)]


async def test_hour_interval_too_old(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "import_statistics",
            {
                "device_id": device_id(hass, config_entry, "ABCD1234-EF"),
                "entity_id": [entity_id(hass, f"{SITE_ID}_ABCD1234-EF_metric_measurements.ac_voltage")],
                "start": (dt_util.now().date() - timedelta(days=800)).isoformat(),
                "interval": "hour",
            },
            blocking=True,
        )
    assert err.value.translation_key == "too_old_for_interval"


async def test_day_interval_uses_day_resolution(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    session = history_session()
    await setup(hass, config_entry, session)
    voltage = entity_id(hass, f"{SITE_ID}_ABCD1234-EF_metric_measurements.ac_voltage")
    lifetime = entity_id(hass, f"{SITE_ID}_ABCD1234-EF_metric_{LIFETIME}")
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - 2 * H
    await existing_rows(hass, lifetime, t0, 1, unit="Wh", state_offset=0)

    start_date = dt_util.now().date() - timedelta(days=1000)  # ouder dan 730 dagen: kan alleen per dag
    end_date = start_date + timedelta(days=9)
    response = await import_statistics(
        hass,
        device_id=device_id(hass, config_entry, "ABCD1234-EF"),
        entity_id=[voltage, lifetime],
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        interval="day",
    )
    assert [m["hourly_rows"] for m in response["metrics"]] == [10, 10]
    imports = [r for r in session.history_requests if r[1] - r[0] > 3 * H]
    assert {r[2] for r in imports} == {"DAY"} | ({"FIVE_MINUTES"} if any(r[2] == "FIVE_MINUTES" for r in imports) else set())
    assert all(r[0] >= dt_util.utcnow() - timedelta(days=730) for r in imports if r[2] == "FIVE_MINUTES")

    means = await stats(hass, voltage, local_midnight(start_date), types={"mean", "min", "max"})
    assert len(means) == 10
    assert means[0]["mean"] == pytest.approx(235.5)
    assert means[0]["min"] is None

    rows = await stats(hass, lifetime, local_midnight(start_date))
    sums = [r["sum"] for r in rows[:10]]
    assert all(b - a == pytest.approx(28800) for a, b in itertools.pairwise(sums))
    # De reeks sluit aan op de bestaande rij: alles daarna schuift mee zonder terugval
    assert all(b["sum"] >= a["sum"] for a, b in itertools.pairwise(rows))


# --- Limieten en interval maximaal --------------------------------------------------------------


def test_history_limits_known_and_learned() -> None:
    limits = HistoryLimits(None, None)
    inverter = "ONE_PHASE_INVERTER"
    assert limits.get(inverter, "measurements.ac_voltage", "FIVE_MINUTES").max_age_days == 730
    assert limits.get(inverter, "measurements.ac_voltage", "DAY").max_range_days == 32
    assert limits.get(inverter, LIFETIME, "DAY").max_range_days == 366
    assert limits.get(inverter, "measurements.power_factor", "DAY").max_range_days == 7
    assert limits.get("OPTIMIZER", "measurements.output_power", "HOUR").max_range_days == 14

    message = "Three phase inverter x data at hourly or finer resolution is limited to 10 days. Please select …"
    assert limits.learn("THREE_PHASE_INVERTER", "measurements.x", "FIVE_MINUTES", message)
    assert limits.get("THREE_PHASE_INVERTER", "measurements.x", "HOUR").max_range_days == 10
    assert limits.get("THREE_PHASE_INVERTER", "measurements.x", "DAY").max_range_days == 366  # alleen fijn
    assert limits.learn("X", "m.y", "DAY", "Y data is only available for the last 100 days. Please …")
    assert limits.get("X", "m.y", "FIVE_MINUTES").max_age_days == 100
    assert limits.get("X", "m.y", "DAY").max_age_days == 100
    assert not limits.learn("X", "m.y", "DAY", "Y data is only available for the last 100 days.")  # niets nieuws


def _metric(device_type: str = "THREE_PHASE_INVERTER", metric: str = "measurements.x") -> ImportableMetric:
    return ImportableMetric("u", "sensor.x", "X", metric, "gauge", "W", None, ((device_type, ("A",)),))


async def test_fetch_charts_learns_range_limit() -> None:
    client = MagicMock()
    ranges: list[tuple[datetime, datetime]] = []

    async def generate_chart(_site: int, start: datetime, end: datetime, _ds: list[Any]) -> GenerateChartResponse:
        if end - start > timedelta(days=10):
            raise ApiError("x", 400, "EXPORT_LIMIT_REACHED", "X data at hourly or finer resolution is limited to 10 days.")
        ranges.append((start, end))
        return chart({"A": [(start, 1.0)]})

    client.analysis.generate_chart = AsyncMock(side_effect=generate_chart)
    limits = HistoryLimits(None, None)
    notes: list[dict[str, Any]] = []
    start = dt_util.utcnow() - timedelta(days=25)
    await fetch_charts(client, SITE_ID, _metric(), [(start, start + timedelta(days=25))], limits=limits, notes=notes)
    assert [(e - s).days for s, e in ranges] == [10, 10, 5]
    assert notes[0]["code"] == "EXPORT_LIMIT_REACHED"
    # De volgende keer meteen goed
    client.analysis.generate_chart.reset_mock()
    await fetch_charts(client, SITE_ID, _metric(), [(start, start + timedelta(days=20))], limits=limits)
    assert client.analysis.generate_chart.call_count == 2


async def test_fetch_charts_detects_silent_daily_values() -> None:
    client = MagicMock()
    sizes: list[int] = []

    async def generate_chart(_site: int, start: datetime, end: datetime, _ds: list[Any]) -> GenerateChartResponse:
        sizes.append((end - start).days)
        step = timedelta(days=1) if end - start > timedelta(days=20) else M5
        return chart({"A": [(start + i * step, 1.0) for i in range(5)]})

    client.analysis.generate_chart = AsyncMock(side_effect=generate_chart)
    notes: list[dict[str, Any]] = []
    start = dt_util.utcnow() - timedelta(days=40)
    charts = await fetch_charts(client, SITE_ID, _metric(), [(start, start + timedelta(days=40))], notes=notes)
    assert sizes[0] == 31  # standaard 31 dagen per request
    assert all(size <= 20 for size in sizes[2:])
    assert any(n["code"] == "downsampled" for n in notes)
    assert len(charts) == len(sizes) - 1  # het blok met dagwaarden telt niet mee


async def test_fetch_charts_skips_data_older_than_the_limit() -> None:
    client = MagicMock()
    client.analysis.generate_chart = AsyncMock(return_value=chart({"A": [(T, 1.0)]}))
    notes: list[dict[str, Any]] = []
    now = dt_util.utcnow()
    await fetch_charts(
        client, SITE_ID, _metric("OPTIMIZER", "measurements.output_power"), [(now - timedelta(days=200), now)], notes=notes
    )
    first_start = client.analysis.generate_chart.call_args_list[0].args[1]
    assert first_start >= now - timedelta(days=90)
    assert notes == [{"code": "age_limit", "metric": "measurements.output_power", "granularity": "FIVE_MINUTES", "days": 90}]


def test_interval_starts_max(hass: HomeAssistant) -> None:
    day = date(2026, 8, 1)
    start, fine_from, end = local_midnight(day), local_midnight(day + timedelta(days=3)), local_midnight(day + timedelta(days=4))
    hourly, short_term = interval_starts(start, end, "max", fine_from, end - 2 * H)
    assert hourly[:3] == [local_midnight(day + timedelta(days=d + 1)) - H for d in range(3)]
    assert hourly[3:] == [fine_from + h * H for h in range(24)]
    assert len(short_term) == 24


async def test_import_max_site_energy(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    # Kleinere leeftijdsgrens dan in het echt (730 dagen), zodat de test snel is; de integratie leert hem uit de fout
    session = history_session(dt_util.now().date() - timedelta(days=60))
    await setup(hass, config_entry, session)
    statistic_id = entity_id(hass, f"{SITE_ID}_site_energy_lifetime")
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - 2 * H
    await existing_rows(hass, statistic_id, t0, 1)

    with (
        patch.dict(FAKE_FINE_AGE, {"days": 40}),
        patch("custom_components.solaredge_monitoring.history_limits.known_limit", side_effect=_short_fine_limit),
    ):
        response = await import_statistics(
            hass, device_id=device_id(hass, config_entry, f"site_{SITE_ID}"), entity_id=[statistic_id], interval="max"
        )
    (result,) = response["metrics"]
    first_day = dt_util.now().date() - timedelta(days=60)
    assert response["from"] == local_midnight(first_day).isoformat()
    assert result["first"] == (local_midnight(first_day + timedelta(days=1)) - H).isoformat()  # dagrij van de eerste dag

    imports = [r for r in session.history_requests if r[1] - r[0] > 3 * H]
    assert {r[2] for r in imports} == {"DAY", "FIVE_MINUTES"}
    assert not [r for r in imports if r[2] != "DAY" and r[0] < dt_util.utcnow() - timedelta(days=40)]

    rows = await stats(hass, statistic_id, local_midnight(first_day))
    daily = [r for r in rows if r["start"] < (dt_util.utcnow() - timedelta(days=41)).timestamp()]
    hourly = [r for r in rows if r["start"] >= (dt_util.utcnow() - timedelta(days=38)).timestamp()]
    assert len(daily) in (18, 19)  # één rij per dag
    assert len(hourly) > 37 * 24
    assert_consistent(rows)
    short_term = await stats(hass, statistic_id, dt_util.utcnow() - timedelta(days=20), "5minute")
    assert short_term
    assert dt_util.utc_from_timestamp(short_term[0]["start"]) >= dt_util.utcnow() - timedelta(days=11)
    assert all(dt_util.utc_from_timestamp(r["start"]).minute % 5 == 0 for r in short_term)
    assert all(dt_util.utc_from_timestamp(r["start"]).second == 0 for r in short_term)


def _short_fine_limit(device_type: str, metric: str, cls: str) -> Any:
    from custom_components.solaredge_monitoring.history_limits import Limit  # noqa: PLC0415

    return Limit(max_range_days=31 if cls == "fine" else 366, max_age_days=40 if cls == "fine" else None)


async def test_import_max_optimizer_power(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    session = history_session()
    await setup(hass, config_entry, session)
    power = entity_id(hass, f"{SITE_ID}_11112222-AB_power")
    response = await import_statistics(
        hass, device_id=device_id(hass, config_entry, "11112222-AB"), entity_id=[power], interval="max"
    )
    (result,) = response["metrics"]
    assert {n["code"] for n in result["notes"]} >= {"age_limit"}
    assert all(r[1] - r[0] <= timedelta(days=14) for r in session.history_requests if r[2] != "DAY")
    assert not [r for r in session.history_requests if r[0] < dt_util.utcnow() - timedelta(days=90)]
    rows = await stats(hass, power, dt_util.utcnow() - timedelta(days=100), types={"mean"})
    assert dt_util.utc_from_timestamp(rows[0]["start"]) >= dt_util.utcnow() - timedelta(days=90)
    assert len(rows) > 85 * 24

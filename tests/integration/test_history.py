"""Historie-import van *Energie totaal*: pure berekening, ophalen in blokken en de import in de recorder."""

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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.solaredge_monitoring.api import ApiError
from custom_components.solaredge_monitoring.api.models import GenerateChartResponse, parse
from custom_components.solaredge_monitoring.const import DOMAIN
from custom_components.solaredge_monitoring.discovery import InverterModel
from custom_components.solaredge_monitoring.history import (
    Existing,
    Row,
    build_import,
    chunks,
    fetch_lifetime,
    fetch_ranges,
    interval_starts,
    lifetime_series,
)
from tests.fake_session import Call, FakeResponse, FakeSession
from tests.helpers import SITE_ID, load_fixture
from tests.integration.conftest import build_session, site_responses
from tests.integration.test_setup import entity_id, setup

T = datetime(2026, 8, 1, tzinfo=UTC)
H = timedelta(hours=1)
LIFETIME_START_WH = 22_000_000  # stand aan het begin van de nep-historie
WH_PER_5MIN = 100  # 1,2 kWh per uur
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: Any, enable_custom_integrations: None) -> None:
    """Zoals in conftest, maar met de recorder vóór ``hass`` (anders weigert de recorder-fixture)."""


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def chart(points: dict[str, list[tuple[datetime, float | None]]]) -> GenerateChartResponse:
    """Nep-generate-chart met per omvormer de lifetime (Wh)."""
    raw = {
        "meta": {
            "datasetsMeta": [
                {
                    "reportObject": {"entityType": "INVERTER", "entityId": device},
                    "metrics": [{"uri": "lifetime_positive_ac_energy", "kind": "CUMULATIVE", "units": "Wh"}],
                    "datasetName": "INVERTER:measurements",
                }
                for device in points
            ]
        },
        "data": [[[_iso(t), v] for t, v in series] for series in points.values()],
    }
    return parse(GenerateChartResponse, raw)


# --- Pure functies ------------------------------------------------------------------------------


def test_lifetime_series_sums_inverters_with_forward_fill() -> None:
    m = timedelta(minutes=5)
    series = lifetime_series(
        [
            chart(
                {
                    "A": [(T, 1000), (T + m, None), (T + 2 * m, 1200)],
                    "B": [(T + m, 5000), (T + 2 * m, None)],
                }
            )
        ]
    )
    # T zelf ontbreekt: B heeft daar nog geen waarde
    assert series == [(T + m, 6.0), (T + 2 * m, 6.2)]


def test_lifetime_series_merges_chunks() -> None:
    m = timedelta(minutes=5)
    series = lifetime_series([chart({"A": [(T, 1000)]}), chart({"A": [(T + m, 1100)]})])
    assert series == [(T, 1.0), (T + m, 1.1)]


def test_chunks() -> None:
    assert chunks(T, T + timedelta(days=15)) == [
        (T, T + timedelta(days=7)),
        (T + timedelta(days=7), T + timedelta(days=14)),
        (T + timedelta(days=14), T + timedelta(days=15)),
    ]


def _series(hours: int, start_kwh: float = 100.0, begin: datetime = T) -> list[tuple[datetime, float]]:
    """Elke 5 minuten 0,1 kWh erbij (1,2 kWh per uur)."""
    return [(begin + timedelta(minutes=5 * i), round(start_kwh + 0.1 * i, 3)) for i in range(hours * 12 + 1)]


def test_build_import_without_previous_rows() -> None:
    # Eén bestaande rij om 04:00 (de recorder zag een deel van het uur: som 0,5); site-teller 86 kWh hoger
    nxt = Row(T + 4 * H, state=100 + 6.0 + 86, sum=0.5)
    plan = build_import(_series(6), T, T + 4 * H, "hour", Existing(None, nxt))

    assert [r["start"] for r in plan.hourly] == [T + h * H for h in range(4)]
    assert [r["sum"] for r in plan.hourly] == [1.2, 2.4, 3.6, 4.8]
    assert [r["state"] for r in plan.hourly] == [187.2, 188.4, 189.6, 190.8]
    assert plan.short_term == []
    assert plan.adjust_from == nxt.start
    assert plan.sum_adjustment == pytest.approx(6.0 - 0.5)


def test_build_import_continues_previous_row_and_rewrites_existing() -> None:
    prev = Row(T, state=101.2 + 86, sum=50.0)  # eind 01:00, L = 101,2
    nxt = Row(T + 4 * H, state=106.0 + 86, sum=10.0)  # som uit een oude reeks
    existing = Existing(prev, nxt, hourly=[T + 2 * H])  # bestaande rij binnen de periode wordt herschreven
    plan = build_import(_series(6), T + H, T + 4 * H, "hour", existing)

    assert [(r["start"], r["sum"]) for r in plan.hourly] == [(T + H, 51.2), (T + 2 * H, 52.4), (T + 3 * H, 53.6)]
    assert plan.sum_adjustment == pytest.approx(54.8 - 10.0)


def test_build_import_same_data_twice_changes_nothing() -> None:
    prev = Row(T, state=101.2, sum=50.0)
    nxt = Row(T + 4 * H, state=106.0, sum=54.8)
    plan = build_import(_series(6), T + H, T + 4 * H, "hour", Existing(prev, nxt))
    assert plan.sum_adjustment == 0
    assert plan.adjust_from is None
    assert [r["sum"] for r in plan.hourly] == [51.2, 52.4, 53.6]


def test_build_import_five_minutes_writes_short_term_and_hours() -> None:
    nxt = Row(T + 2 * H, state=None, sum=0.0)
    plan = build_import(_series(3), T, T + 2 * H, "five_minutes", Existing(None, nxt))
    assert len(plan.hourly) == 2
    assert len(plan.short_term) == 24
    assert plan.short_term[0]["sum"] == pytest.approx(0.1)
    # De laatste 5 minuten van een uur hebben dezelfde som als de uurrij
    assert plan.short_term[11]["sum"] == plan.hourly[0]["sum"]


def test_build_import_day_rows_at_last_local_hour(hass: HomeAssistant) -> None:
    day = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 8, 1)))
    nxt = Row(day + timedelta(days=3), state=None, sum=0.0)
    plan = build_import(_series(24 * 3, begin=day), day, day + timedelta(days=3), "day", Existing(None, nxt))
    assert [r["start"] for r in plan.hourly] == [day + timedelta(days=d + 1) - H for d in range(3)]
    assert [r["sum"] for r in plan.hourly] == pytest.approx([28.8, 57.6, 86.4])


def test_build_import_skips_rows_before_first_measurement() -> None:
    series = [(t, v) for t, v in _series(6) if t >= T + 2 * H]
    plan = build_import(series, T, T + 4 * H, "hour", Existing(None, Row(T + 4 * H, None, 0.0)))
    # Data vanaf 02:00: de som begint daar op 0, uren zonder meting erin worden overgeslagen
    assert [r["start"] for r in plan.hourly] == [T + h * H for h in (2, 3)]
    assert [r["sum"] for r in plan.hourly] == pytest.approx([1.2, 2.4])


def test_interval_starts_five_minutes() -> None:
    hourly, short_term = interval_starts(T, T + 2 * H, "five_minutes")
    assert hourly == [T, T + H]
    assert len(short_term) == 24
    assert short_term[1] == T + timedelta(minutes=5)


def test_fetch_ranges_far_anchors_get_their_own_request() -> None:
    now = T + timedelta(days=60)
    existing = Existing(Row(T - timedelta(days=30), None, 0.0), Row(T + timedelta(days=20), None, 0.0))
    ranges = fetch_ranges(T, T + timedelta(days=3), existing, now)
    assert ranges[0] == (T - timedelta(days=1), T + timedelta(days=3))
    assert len(ranges) == 3


async def test_fetch_lifetime_halves_chunk_on_export_limit() -> None:
    model = MagicMock(site_id=SITE_ID, inverters=[InverterModel("A-1", "A", "Inverter 1", None, "ONE_PHASE_INVERTER")])
    client = MagicMock()
    ranges: list[tuple[datetime, datetime]] = []

    async def generate_chart(_site: int, start: datetime, end: datetime, _ds: list[Any]) -> GenerateChartResponse:
        if end - start > timedelta(days=4):
            raise ApiError("te groot", 400, "EXPORT_LIMIT_REACHED")
        ranges.append((start, end))
        return chart({"A": [(start, 1000)]})

    client.analysis.generate_chart = AsyncMock(side_effect=generate_chart)
    series = await fetch_lifetime(client, model, [(T, T + timedelta(days=7))])
    assert ranges == [
        (T, T + timedelta(days=3.5)),
        (T + timedelta(days=3.5), T + timedelta(days=7)),
    ]
    assert len(series) == 2


# --- Import in de recorder ----------------------------------------------------------------------


def lifetime_wh(moment: datetime) -> int:
    return LIFETIME_START_WH + WH_PER_5MIN * int((moment - EPOCH) / timedelta(minutes=5))


def history_session() -> FakeSession:
    """Nep-API waarin generate-chart voor de lifetime-metric 5-minutenwaarden voor het gevraagde bereik geeft."""
    telemetry = load_fixture("telemetry-chart.json")
    session = build_session(site_responses())
    responses: list[tuple[datetime, datetime]] = []

    def respond(call: Call) -> FakeResponse:
        body = call.json
        uris = [m["uri"] for ds in body["datasources"] for m in ds["metrics"]]
        if uris != ["lifetime_positive_ac_energy"]:
            return FakeResponse(200, json.dumps(telemetry))
        start = dt_util.parse_datetime(body["reportPeriod"]["from"])
        end = dt_util.parse_datetime(body["reportPeriod"]["to"])
        responses.append((start, end))
        points = []
        moment = start
        while moment < end:
            points.append((moment, lifetime_wh(moment)))
            moment += timedelta(minutes=5)
        raw = {
            "meta": {
                "datasetsMeta": [
                    {
                        "reportObject": {"entityType": "INVERTER", "entityId": "ABCD1234"},
                        "metrics": [{"uri": "lifetime_positive_ac_energy"}],
                        "datasetName": "INVERTER:measurements",
                    }
                ]
            },
            "data": [[[_iso(t), v] for t, v in points]],
        }
        return FakeResponse(200, json.dumps(raw))

    session.add_callback("POST", r"generate-chart$", respond)
    session._routes.insert(0, session._routes.pop())  # vóór de standaard-fixture
    session.history_ranges = responses  # type: ignore[attr-defined]
    return session


async def _existing_rows(hass: HomeAssistant, statistic_id: str, t0: datetime, hours: int) -> None:
    """Bestaande uurrijen zoals de recorder ze maakt: som vanaf 0,5, stand van de site-teller (86 kWh hoger)."""
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=statistic_id,
        unit_class="energy",
        unit_of_measurement="kWh",
    )
    rows = []
    for h in range(hours):
        start = t0 + h * H
        rows.append({"start": start, "state": lifetime_wh(start + H) / 1000 + 86, "sum": 0.5 + 1.2 * h})
    async_import_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)


async def _stats(hass: HomeAssistant, statistic_id: str, start: datetime, period: str = "hour") -> list[dict[str, Any]]:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {statistic_id}, period, None, {"state", "sum"}
    )
    return stats.get(statistic_id, [])


def _site_device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, f"site_{SITE_ID}"), entry.entry_id)
    assert device
    return device.id


async def _import(hass: HomeAssistant, entry: MockConfigEntry, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        "import_energy_history",
        {"device_id": _site_device_id(hass, entry), **data},
        blocking=True,
        return_response=True,
    )
    await async_wait_recording_done(hass)
    return response


def assert_consistent(rows: list[dict[str, Any]]) -> None:
    """Som en stand lopen met 1,2 kWh per uur op, ook over gaten heen."""
    for a, b in itertools.pairwise(rows):
        hours = (b["start"] - a["start"]) / 3600
        assert b["sum"] - a["sum"] == pytest.approx(1.2 * hours), (a, b)
        assert b["state"] - a["state"] == pytest.approx(1.2 * hours), (a, b)


def local_midnight(day: date) -> datetime:
    return dt_util.as_utc(dt_util.start_of_local_day(day))


async def test_import_service_hour(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    session = history_session()
    await setup(hass, config_entry, session)
    statistic_id = entity_id(hass, f"{SITE_ID}_site_energy_lifetime")
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - 4 * H
    await _existing_rows(hass, statistic_id, t0, 3)

    start_date = dt_util.now().date() - timedelta(days=10)
    start = local_midnight(start_date)
    response = await _import(hass, config_entry, start=start_date.isoformat())

    # Eindigt bij de laatste uurrij van de recorder; de twee bestaande rijen ervoor worden herschreven
    end = t0 + 2 * H
    assert response["statistic_id"] == statistic_id
    assert response["interval"] == "hour"
    assert response["from"] == start.isoformat()
    assert response["to"] == end.isoformat()
    assert response["hourly_rows"] == int((end - start) / H)
    assert response["short_term_rows"] == 0
    assert len(session.history_ranges) == 2  # 11 dagen in blokken van 7

    rows = await _stats(hass, statistic_id, start)
    assert len(rows) == response["hourly_rows"] + 1
    assert rows[0]["sum"] == pytest.approx(1.2)
    assert_consistent(rows)
    assert rows[-1]["state"] == pytest.approx(lifetime_wh(end + H) / 1000 + 86)

    # Nog een keer: dezelfde waarden, niets verschoven
    again = await _import(hass, config_entry, start=start_date.isoformat())
    assert again["sum_adjustment_kwh"] == 0
    assert [r["sum"] for r in await _stats(hass, statistic_id, start)] == pytest.approx([r["sum"] for r in rows])

    # Een eerdere periode per dag, met een gat ertussen, sluit ook aan
    earlier = await _import(
        hass,
        config_entry,
        start=(start_date - timedelta(days=5)).isoformat(),
        end=(start_date - timedelta(days=3)).isoformat(),
        interval="day",
    )
    assert earlier["hourly_rows"] == 3
    rows = await _stats(hass, statistic_id, start - timedelta(days=5))
    assert len(rows) == response["hourly_rows"] + 1 + 3
    assert_consistent(rows)


async def test_import_service_five_minutes(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    statistic_id = entity_id(hass, f"{SITE_ID}_site_energy_lifetime")
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - 2 * H
    await _existing_rows(hass, statistic_id, t0, 1)

    start_date = dt_util.now().date() - timedelta(days=1)
    response = await _import(hass, config_entry, start=start_date.isoformat(), interval="five_minutes")
    assert response["short_term_rows"] == response["hourly_rows"] * 12

    start = local_midnight(start_date)
    short_term = await _stats(hass, statistic_id, start, "5minute")
    hourly = await _stats(hass, statistic_id, start)
    assert len(short_term) == response["short_term_rows"]
    for a, b in itertools.pairwise(short_term):
        assert b["sum"] - a["sum"] == pytest.approx(0.1)
    assert short_term[11]["sum"] == pytest.approx(hourly[0]["sum"])


async def test_import_without_statistics(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "import_energy_history",
            {"device_id": _site_device_id(hass, config_entry), "start": "2026-01-01"},
            blocking=True,
        )
    assert err.value.translation_key == "no_statistics_yet"


async def test_import_rejects_invalid_input(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    inverter = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "ABCD1234-EF"), config_entry.entry_id)
    assert inverter
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "import_energy_history", {"device_id": inverter.id, "start": "2026-01-01"}, blocking=True
        )
    assert err.value.translation_key == "not_a_site_device"

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "import_energy_history",
            {"device_id": _site_device_id(hass, config_entry), "start": "2026-02-01", "end": "2026-01-01"},
            blocking=True,
        )
    assert err.value.translation_key == "invalid_period"


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
    await _existing_rows(hass, statistic_id, t0, 2)

    button = entity_id(hass, f"{SITE_ID}_site_import_energy_history", "button")
    with patch("custom_components.solaredge_monitoring.services.persistent_notification.async_create") as notify:
        await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    notify.assert_called_once()
    message, title = notify.call_args.args[1], notify.call_args.args[2]
    assert statistic_id in message
    assert "history imported" in title
    today = dt_util.now().date()
    rows = await _stats(hass, statistic_id, local_midnight(today - timedelta(days=2)))
    # Dag-interval: de eerste rij is het laatste uur van eergisteren
    assert rows[0]["start"] == (local_midnight(today - timedelta(days=1)) - H).timestamp()
    assert_consistent(rows)

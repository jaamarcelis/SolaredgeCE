"""Historie-import van *Energie totaal*: pure berekening, ophalen in blokken en de import in de recorder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    ExistingStart,
    build_import,
    chunks,
    fetch_lifetime,
    lifetime_series,
)
from tests.fake_session import Call, FakeResponse, FakeSession
from tests.helpers import SITE_ID, load_fixture
from tests.integration.conftest import build_session, site_responses
from tests.integration.test_setup import entity_id, setup

T = datetime(2026, 8, 1, tzinfo=UTC)
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


def _hourly_series(hours: int, start_kwh: float = 100.0) -> list[tuple[datetime, float]]:
    """Elke 5 minuten 0,1 kWh erbij."""
    return [(T + timedelta(minutes=5 * i), round(start_kwh + 0.1 * i, 3)) for i in range(hours * 12 + 1)]


def test_build_import_before_existing_rows() -> None:
    series = _hourly_series(6)
    t0 = T + timedelta(hours=4)
    # Bestaande rij t0: de recorder zag maar een deel van het uur (som 0,5) en de site-teller ligt 86 kWh hoger
    plan = build_import(series, T, ExistingStart(t0, state=100 + 6.0 + 86, sum=0.5))

    assert [r["start"] for r in plan.rows] == [T + timedelta(hours=h) for h in range(4)]
    assert [r["sum"] for r in plan.rows] == [1.2, 2.4, 3.6, 4.8]
    assert [r["state"] for r in plan.rows] == [187.2, 188.4, 189.6, 190.8]
    assert plan.adjust_from == t0
    assert plan.sum_adjustment == pytest.approx(6.0 - 0.5)


def test_build_import_nothing_when_rows_exist_from_window_start() -> None:
    plan = build_import(_hourly_series(6), T, ExistingStart(T, 106.0, 1.2))
    assert plan.rows == [] and plan.sum_adjustment == 0 and plan.adjust_from is None


def test_build_import_skips_hours_without_data() -> None:
    # Data pas vanaf 02:00; de meting om 02:00 is het eind van uur 01:00
    series = [(t, v) for t, v in _hourly_series(6) if t >= T + timedelta(hours=2)]
    plan = build_import(series, T, ExistingStart(T + timedelta(hours=4), None, 0.0))
    assert [r["start"] for r in plan.rows] == [T + timedelta(hours=h) for h in (1, 2, 3)]
    assert [r["sum"] for r in plan.rows] == pytest.approx([0.0, 1.2, 2.4])


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
    series = await fetch_lifetime(client, model, T, T + timedelta(days=7))
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
    """Bestaande uurrijen zoals de recorder ze maakt: som vanaf 0, stand van de site-teller (86 kWh hoger)."""
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
        start = t0 + timedelta(hours=h)
        rows.append({"start": start, "state": lifetime_wh(start + timedelta(hours=1)) / 1000 + 86, "sum": 0.5 + 1.2 * h})
    async_import_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)


async def _hourly(hass: HomeAssistant, statistic_id: str, start: datetime) -> list[dict[str, Any]]:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {statistic_id}, "hour", None, {"state", "sum"}
    )
    return stats.get(statistic_id, [])


def _site_device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, f"site_{SITE_ID}"), entry.entry_id)
    assert device
    return device.id


async def test_import_service(recorder_mock: Any, hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    session = history_session()
    await setup(hass, config_entry, session)
    statistic_id = entity_id(hass, f"{SITE_ID}_site_energy_lifetime")

    now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    t0 = now_hour - timedelta(hours=4)
    await _existing_rows(hass, statistic_id, t0, 3)

    response = await hass.services.async_call(
        DOMAIN,
        "import_energy_history",
        {"device_id": _site_device_id(hass, config_entry), "days": 10},
        blocking=True,
        return_response=True,
    )
    await async_wait_recording_done(hass)

    window_start = now_hour - timedelta(days=10)
    assert response["statistic_id"] == statistic_id
    assert response["imported_hours"] == 10 * 24 - 4
    assert response["from"] == window_start.isoformat()
    assert response["to"] == t0.isoformat()
    assert response["sum_adjustment_kwh"] == pytest.approx((10 * 24 - 3) * 1.2 - 0.5)
    # Opgehaald in blokken van 7 dagen
    assert len(session.history_ranges) == 2

    rows = await _hourly(hass, statistic_id, window_start)
    assert len(rows) == 10 * 24 - 1
    sums = [r["sum"] for r in rows]
    assert sums[0] == pytest.approx(1.2)
    assert all(b - a == pytest.approx(1.2) for a, b in itertools.pairwise(sums))
    states = [r["state"] for r in rows]
    assert all(b - a == pytest.approx(1.2) for a, b in itertools.pairwise(states))
    assert states[-1] == pytest.approx(lifetime_wh(t0 + timedelta(hours=3)) / 1000 + 86)

    # Nog een keer: niets meer te doen
    again = await hass.services.async_call(
        DOMAIN,
        "import_energy_history",
        {"device_id": _site_device_id(hass, config_entry), "days": 10},
        blocking=True,
        return_response=True,
    )
    assert again["imported_hours"] == 0
    await async_wait_recording_done(hass)
    assert [r["sum"] for r in await _hourly(hass, statistic_id, window_start)] == pytest.approx(sums)


async def test_import_without_statistics(recorder_mock: Any, hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "import_energy_history", {"device_id": _site_device_id(hass, config_entry)}, blocking=True
        )
    assert err.value.translation_key == "no_statistics_yet"


async def test_import_rejects_other_devices(recorder_mock: Any, hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    inverter = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "ABCD1234-EF"), config_entry.entry_id)
    assert inverter
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "import_energy_history", {"device_id": inverter.id}, blocking=True)
    assert err.value.translation_key == "not_a_site_device"


async def test_button_notifies(recorder_mock: Any, hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await setup(hass, config_entry, history_session())
    statistic_id = entity_id(hass, f"{SITE_ID}_site_energy_lifetime")
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    await _existing_rows(hass, statistic_id, t0, 2)

    button = entity_id(hass, f"{SITE_ID}_site_import_energy_history", "button")
    with patch("custom_components.solaredge_monitoring.services.persistent_notification.async_create") as notify:
        await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    notify.assert_called_once()
    message, title = notify.call_args.args[1], notify.call_args.args[2]
    assert "2157" in message and statistic_id in message
    assert "history imported" in title

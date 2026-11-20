"""Request-paden, querystrings, bodies en foutafhandeling van de API-client."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from custom_components.solaredge_monitoring.api import (
    ApiError,
    AuthenticationError,
    CloudflareError,
    NotApplicableError,
    SolarEdgeMonitoringClient,
    measurement_datasource,
)
from custom_components.solaredge_monitoring.api.const import MeasurementType
from custom_components.solaredge_monitoring.api.transport import build_query
from tests.fake_session import FakeSession
from tests.helpers import SITE_ID, load_fixture


def make(session: FakeSession) -> SolarEdgeMonitoringClient:
    return SolarEdgeMonitoringClient(session, bearer_token="test-token")  # type: ignore[arg-type]


def test_build_query():
    query = build_query(
        [("a", "x y"), ("skip", None), ("b", True), ("d", date(2026, 1, 2)), ("m", "p"), ("m", "q"), ("f[]", "z")]
    )
    assert query == "a=x%20y&b=true&d=2026-01-02&m=p&m=q&f%5B%5D=z"


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda c: c.user.get_user(), "user-management/user"),
        (lambda c: c.sites.get_site(SITE_ID), f"cni/ui-api/sites/{SITE_ID}"),
        (lambda c: c.dashboard.get_components(SITE_ID), f"dashboard/site-details/{SITE_ID}/components"),
        (lambda c: c.dashboard.get_equipment(SITE_ID), f"dashboard/v2/site-details/{SITE_ID}/equipment"),
        (
            lambda c: c.dashboard.get_equipment_inverter(SITE_ID, "A-1"),
            f"dashboard/site-details/{SITE_ID}/equipment-inverter/A-1",
        ),
        (lambda c: c.dashboard.get_live_power(SITE_ID), f"dashboard/live-power/sites/{SITE_ID}"),
        (lambda c: c.dashboard.get_power_flow(SITE_ID), f"dashboard/power-flow/v2/sites/{SITE_ID}"),
        (
            lambda c: c.dashboard.get_battery(SITE_ID, date(2026, 11, 20), date(2026, 11, 20)),
            f"dashboard/battery/sites/{SITE_ID}?start-date=2026-11-20&end-date=2026-11-20",
        ),
        (
            lambda c: c.dashboard.get_inverters_power(SITE_ID, date(2026, 11, 20), date(2026, 11, 20)),
            f"dashboard/inverters/power/sites/{SITE_ID}?start-date=2026-11-20&end-date=2026-11-20&normalized=false"
            "&chart-time-unit=quarter-hours&page-number=0",
        ),
        (lambda c: c.weather.get_weather(SITE_ID), f"dashboard/weather/sites/{SITE_ID}/weather"),
        (
            lambda c: c.analysis.get_device_type_metrics(SITE_ID, "OPTIMIZER"),
            f"cni/ui-api/pages/site/analysis/site/{SITE_ID}/device-types/OPTIMIZER/metrics/supported",
        ),
        (lambda c: c.layout.get_logical_layout(SITE_ID), f"layout/logical/generic/v2/site/{SITE_ID}?include-optimizers=true"),
        (
            lambda c: c.layout.get_energy_by_inverter(SITE_ID, date(2026, 11, 20), date(2026, 11, 20), ["A-1", "B-2"]),
            f"layout/energy/site/{SITE_ID}/by-inverter?start-date=2026-11-20&end-date=2026-11-20&inverter-serials=A-1%2CB-2"
            "&include-max-temperature=false&include-color=true",
        ),
        (lambda c: c.alerts.get_total_count(SITE_ID), f"alerts/site/{SITE_ID}/alertTotalCount"),
        (
            lambda c: c.user.are_feature_flags_enabled(["a", "b"]),
            "installer-infra/is-feature-flags-enabled?feature-flag-names%5B%5D=a&feature-flag-names%5B%5D=b",
        ),
    ],
)
async def test_get_paths(call, expected):
    session = FakeSession().add("GET", ".*", json_body={})
    await call(make(session))
    (sent,) = session.calls
    assert (sent.method, sent.path, sent.data) == ("GET", expected, None)


async def test_energy_repeats_measurement_types():
    session = FakeSession().add("GET", "energy", json_body=load_fixture("energy.json"))
    energy = await make(session).dashboard.get_energy(
        SITE_ID, date(2026, 11, 17), date(2026, 11, 19), measurement_types=[MeasurementType.PRODUCTION, MeasurementType.YIELD]
    )
    assert session.calls[0].path == (
        f"dashboard/energy/sites/{SITE_ID}?chart-time-unit=days&start-date=2026-11-17&end-date=2026-11-19"
        "&measurement-types=production&measurement-types=yield&isCniViewer=true"
    )
    assert energy.summary.production == 4968.0
    assert energy.summary.yield_ == 1.29375
    assert len(energy.chart.measurements) == 3


async def test_sites_measurements_body():
    session = FakeSession().add("POST", "sitesMeasurements", json_body=load_fixture("sites-measurements.json"))
    result = await make(session).site_list.get_site_measurements(SITE_ID)
    sent = session.calls[0]
    assert (sent.method, sent.path) == ("POST", "sitelist/sitesMeasurements")
    assert sent.json["sites"] == [SITE_ID]
    assert len(sent.json["measurementsTypes"]) == 14
    assert result.energy_life_time == 22643.48


async def test_search_sites_sends_default_filter_and_keeps_extra_fields():
    session = FakeSession().add("POST", "searchSites", json_body=load_fixture("site-search.json"))
    result = await make(session).site_list.search_sites()
    sent = session.calls[0]
    assert sent.path.startswith("sitelist/searchSites?v=")
    assert sent.json["pageRequest"]["pageNum"] == 1
    assert sent.json["peakPowerFilter"]["max"] == 1_000_000
    assert sent.json["geoBoundingBox"] is None
    site = result.page[0]
    assert site.solar_field_id == SITE_ID
    assert site.optimizer_count == 12
    assert site.extra["someNewField"] == 42


async def test_generate_chart_body_and_latest():
    session = FakeSession().add("POST", "generate-chart", json_body=load_fixture("generate-chart.json"))
    chart = await make(session).analysis.generate_chart(
        SITE_ID,
        datetime(2026, 11, 18, 23, 0, tzinfo=UTC),
        datetime(2026, 11, 19, 22, 59, 59, 999000, tzinfo=UTC),
        [measurement_datasource(SITE_ID, "ONE_PHASE_INVERTER", ["ABCD1234"], [("pv_measurements", "power")])],
    )
    sent = session.calls[0]
    assert sent.path == f"cni/ui-api/pages/site/analysis/custom/site/{SITE_ID}/generate-chart"
    assert sent.json["reportPeriod"] == {"from": "2026-11-18T23:00:00.000Z", "to": "2026-11-19T22:59:59.999Z"}
    ds = sent.json["datasources"][0]
    assert ds["metrics"] == [{"metricType": "measurement", "metricsCategoryName": "pv_measurements", "uri": "power"}]
    assert ds["datasourcePopulation"]["deviceSerials"] == ["ABCD1234"]
    latest = chart.latest(0)
    assert latest["power"][1] == 1250.5
    assert latest["status"][1] == "PRODUCTION"
    assert chart.meta.datasets_meta[0].category == "pv_measurements"


async def test_optimizer_information_posts_serials():
    session = FakeSession().add("POST", "optimizers", json_body=load_fixture("optimizers-info.json"))
    info = await make(session).layout.get_optimizers_information(["11112222-AB"])
    assert (session.calls[0].path, session.calls[0].json) == ("layout/information/optimizers", ["11112222-AB"])
    live = info.serial_to_live_data["11112222-AB"]
    assert (live.voltage, live.optimizer_voltage, live.energy) == (18.75, 27.625, 2.0)
    assert info.basic_information_list[0].modules[0].stc_power == 350.0


async def test_bearer_and_browser_headers():
    session = FakeSession().add("GET", ".*", json_body={})
    await make(session).user.get_user()
    sent = session.calls[0]
    assert sent.headers["authorization"] == "Bearer test-token"
    assert sent.headers["origin"] == "https://monitoring.solaredge.com"
    assert sent.allow_redirects is False


@pytest.mark.parametrize(
    ("status", "body", "error", "code"),
    [
        (
            400,
            {"error": {"code": "BAD_ARGUMENTS", "description": "Site does not have active storage."}},
            NotApplicableError,
            "BAD_ARGUMENTS",
        ),
        (403, {"error": {"code": "EMS_FEATURE_NOT_ENABLED", "description": "x"}}, NotApplicableError, "EMS_FEATURE_NOT_ENABLED"),
        (400, {"timestamp": "t", "status": 400, "error": "Bad Request", "path": "/x"}, ApiError, "Bad Request"),
        (500, {"errorCode": "EXPORT_LIMIT_REACHED", "message": "limited"}, ApiError, "EXPORT_LIMIT_REACHED"),
    ],
)
async def test_error_formats(status, body, error, code):
    session = FakeSession().add("GET", ".*", json_body=body, status=status)
    with pytest.raises(error) as info:
        await make(session).dashboard.get_storage_profile(SITE_ID)
    assert info.value.code == code
    assert info.value.status == status


async def test_alerts_403_is_not_applicable():
    session = FakeSession().add("POST", ".*", json_body={"message": "User does not have access to alerts"}, status=403)
    with pytest.raises(NotApplicableError):
        await make(session).transport.post(f"alerts/site/{SITE_ID}/alertsCount", {})


async def test_redirect_is_authentication_error():
    session = FakeSession().add(
        "GET", ".*", status=302, text="", headers={"Location": "https://monitoring.solaredge.com/mfe/auth/"}
    )
    with pytest.raises(AuthenticationError):
        await make(session).user.get_user()


async def test_html_is_cloudflare_error():
    session = FakeSession().add("GET", ".*", text="<html>Just a moment</html>", headers={"Content-Type": "text/html"})
    with pytest.raises(CloudflareError):
        await make(session).user.get_user()


async def test_empty_body_is_none():
    session = FakeSession().add("GET", ".*", text="")
    assert await make(session).sites.get_fleet(SITE_ID) is None


async def test_null_live_power_at_night():
    session = FakeSession().add("GET", ".*", json_body={"currentAcPower": None, "maxAcPower": 3700.0})
    live = await make(session).dashboard.get_live_power(SITE_ID)
    assert live.current_ac_power is None
    assert live.max_ac_power == 3700.0

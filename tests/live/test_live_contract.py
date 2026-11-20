"""Live contracttests: ECHTE requests naar monitoring.solaredge.com (alleen lezen).

Draaien alleen met ``SOLAREDGE_LIVE_TESTS=1`` en ``SOLAREDGE_USERNAME``/``SOLAREDGE_PASSWORD`` (account zonder MFA),
optioneel ``SOLAREDGE_SITE_ID``. Buiten Home Assistant (de HA-testplugin blokkeert netwerkverkeer):

    SOLAREDGE_LIVE_TESTS=1 python -m pytest tests/live -p no:homeassistant -s

Per endpoint: de request slaagt (of is "niet van toepassing"), de response is in het model te lezen, en velden die
het model niet kent (``extra``) worden gerapporteerd. Met ``SOLAREDGE_STRICT=1`` falen die als drift. Een deel van
de modellen is bewust beperkt tot de velden die de integratie gebruikt; zet die in ``KNOWN_UNMODELED``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
import os
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from custom_components.solaredge_monitoring.api import (
    CognitoAuthenticator,
    NotApplicableError,
    SolarEdgeMonitoringClient,
    TokenManager,
    measurement_datasource,
)
from custom_components.solaredge_monitoring.api.models import ApiModel
from custom_components.solaredge_monitoring.discovery import SiteModel, async_discover

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("SOLAREDGE_LIVE_TESTS") != "1", reason="Zet SOLAREDGE_LIVE_TESTS=1"),
]

# Velden die de integratie bewust niet modelleert (per modelnaam)
KNOWN_UNMODELED: dict[str, set[str]] = {
    "UserProfile": {
        "accountGuid",
        "phone",
        "zipCode",
        "homeAddress1",
        "homeAddress2",
        "country",
        "state",
        "city",
        "role",
        "jobType",
        "trustedInstaller",
    },
    "CniSite": {"description", "currencyCode", "sitePriority"},
    "CniSiteLocation": {"elevation", "stateCode"},
    "SiteListItem": {
        "address2",
        "notes",
        "accountId",
        "fieldCity",
        "longitude",
        "latitude",
        "ptoDate",
        "siteMagnitude",
        "country",
        "state",
        "stateCode",
        "isFavorite",
        "gatewayCount",
        "loadDeviceCount",
        "localControllerCount",
        "lcStorageCount",
        "linkBigImage",
        "linkSmallImage",
    },
    "SiteSearchResult": {"numberOfSitesInSearch", "isExceedsSitesLimit", "mapCells"},
    "SiteDetails": {
        "accountName",
        "accountGuid",
        "accountId",
        "state",
        "isAdvantedgeActive",
        "longitude",
        "latitude",
        "isFavorite",
    },
    "SiteComponents": {
        "viewerType",
        "hasBillingCycleProgram",
        "showInverterGraph",
        "canNormalizeData",
        "canEditBillingPeriod",
        "hasCommercialPerformanceRatio",
    },
    "SiteMeasurements": {"prLastMonth", "prLastYear"},
}


def collect_extra(value: Any, path: str = "$") -> list[str]:
    """Alle onbekende velden in een (geneste) modelstructuur."""
    found: list[str] = []
    if isinstance(value, ApiModel):
        known = KNOWN_UNMODELED.get(type(value).__name__, set())
        found += [f"{path}.{k} ({type(value).__name__})" for k in value.extra if k not in known]
        for f in fields(value):
            if f.name != "extra":
                found += collect_extra(getattr(value, f.name), f"{path}.{f.name}")
    elif isinstance(value, list):
        for i, item in enumerate(value[:3]):
            found += collect_extra(item, f"{path}[{i}]")
    elif isinstance(value, dict):
        for k, item in list(value.items())[:3]:
            found += collect_extra(item, f"{path}[{k}]")
    elif is_dataclass(value):
        for f in fields(value):
            found += collect_extra(getattr(value, f.name), f"{path}.{f.name}")
    return found


@pytest.fixture
async def live() -> tuple[SolarEdgeMonitoringClient, SiteModel]:
    username = os.environ.get("SOLAREDGE_USERNAME")
    password = os.environ.get("SOLAREDGE_PASSWORD")
    if not username or not password:
        pytest.skip("SOLAREDGE_USERNAME en SOLAREDGE_PASSWORD zijn nodig")
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
    client = SolarEdgeMonitoringClient(session, TokenManager(CognitoAuthenticator(session), username, password))
    site_id = int(os.environ.get("SOLAREDGE_SITE_ID") or (await client.site_list.search_sites()).page[0].solar_field_id)
    model = await async_discover(client, site_id)
    yield client, model
    await session.close()


Call = Callable[[SolarEdgeMonitoringClient, SiteModel], Awaitable[Any]]


def _today(model: SiteModel) -> date:
    return datetime.now(ZoneInfo(model.timezone)).date()


def _telemetry(client: SolarEdgeMonitoringClient, model: SiteModel) -> Awaitable[Any]:
    inverter = model.inverters[0]
    now = datetime.now(UTC)
    metrics = [("measurements", "ac_voltage"), ("measurements", "temperature")]
    datasource = measurement_datasource(model.site_id, inverter.analysis_type, [inverter.identifier], metrics)
    return client.analysis.generate_chart(model.site_id, now - timedelta(hours=2), now, [datasource])


CASES: dict[str, Call] = {
    "sites.get_site": lambda c, m: c.sites.get_site(m.site_id),
    "user.get_user": lambda c, m: c.user.get_user(),
    "site_list.search_sites": lambda c, m: c.site_list.search_sites(),
    "site_list.get_site_measurements": lambda c, m: c.site_list.get_site_measurements(m.site_id),
    "dashboard.get_site_details": lambda c, m: c.dashboard.get_site_details(m.site_id),
    "dashboard.get_components": lambda c, m: c.dashboard.get_components(m.site_id),
    "dashboard.get_equipment": lambda c, m: c.dashboard.get_equipment(m.site_id),
    "dashboard.get_equipment_inverter": lambda c, m: c.dashboard.get_equipment_inverter(m.site_id, m.inverters[0].serial),
    "dashboard.get_communication": lambda c, m: c.dashboard.get_communication(m.site_id),
    "dashboard.get_live_power": lambda c, m: c.dashboard.get_live_power(m.site_id),
    "dashboard.get_power_flow": lambda c, m: c.dashboard.get_power_flow(m.site_id),
    "dashboard.get_energy": lambda c, m: c.dashboard.get_energy(m.site_id, _today(m), _today(m)),
    "dashboard.get_power": lambda c, m: c.dashboard.get_power(m.site_id, _today(m), _today(m)),
    "dashboard.get_inverters_power": lambda c, m: c.dashboard.get_inverters_power(m.site_id, _today(m), _today(m)),
    "dashboard.get_inverters_energy": lambda c, m: c.dashboard.get_inverters_energy(m.site_id, _today(m), _today(m)),
    "dashboard.get_curtailment": lambda c, m: c.dashboard.get_curtailment(m.site_id, _today(m), _today(m)),
    "dashboard.get_battery": lambda c, m: c.dashboard.get_battery(m.site_id, _today(m), _today(m)),
    "dashboard.get_storage_energy_distribution": lambda c, m: c.dashboard.get_storage_energy_distribution(
        m.site_id, _today(m), _today(m)
    ),
    "dashboard.get_environmental_benefits": lambda c, m: c.dashboard.get_environmental_benefits(m.site_id),
    "dashboard.get_data_availability": lambda c, m: c.dashboard.get_data_availability(m.site_id),
    "weather.get_weather": lambda c, m: c.weather.get_weather(m.site_id),
    "analysis.get_device_type_metrics": lambda c, m: c.analysis.get_device_type_metrics(m.site_id, m.inverters[0].analysis_type),
    "analysis.get_devices": lambda c, m: c.analysis.get_devices(m.site_id),
    "analysis.generate_chart": _telemetry,
    "layout.get_logical_layout": lambda c, m: c.layout.get_logical_layout(m.site_id),
    "layout.get_information": lambda c, m: c.layout.get_information(m.site_id),
    "layout.get_energy_by_inverter": lambda c, m: c.layout.get_energy_by_inverter(
        m.site_id, _today(m), _today(m), [i.serial for i in m.inverters]
    ),
    "layout.get_optimizers_information": lambda c, m: c.layout.get_optimizers_information([o.serial for o in m.optimizers[:3]]),
    "alerts.get_alerts": lambda c, m: c.alerts.get_alerts(m.site_id),
    "reports.get_available_reports": lambda c, m: c.reports.get_available_reports(m.site_id),
}


@pytest.mark.parametrize("name", list(CASES))
async def test_endpoint_contract(live: tuple[SolarEdgeMonitoringClient, SiteModel], name: str) -> None:
    client, model = live
    try:
        result = await CASES[name](client, model)
    except NotApplicableError as err:
        print(f"{name}: niet van toepassing ({err.code or err.status})")
        return
    extra = collect_extra(result)
    for field in extra:
        print(f"{name}: NIEUW VELD {field}")
    if os.environ.get("SOLAREDGE_STRICT") == "1":
        assert not extra, f"{name}: velden die niet in het model staan:\n" + "\n".join(extra)

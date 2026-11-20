"""Fixtures voor de integratietests: een nep-API met alle endpoints die de integratie gebruikt."""

from __future__ import annotations

from collections.abc import Generator
import copy
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solaredge_monitoring.const import CONF_SITE_ID, CONF_SITE_NAME, CONF_TOKENS, DOMAIN
from tests.fake_session import FakeSession
from tests.helpers import SITE_ID, load_fixture, make_jwt


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Laad ``custom_components`` in de tests."""


def site_responses(*, has_storage: bool = False, has_meter: bool = False) -> dict[str, Any]:
    """Responses per endpoint (regex op het pad), gebaseerd op geanonimiseerde echte data."""
    power_flow = load_fixture("power-flow.json")
    if not has_storage:
        power_flow.pop("dcStorage")
    if not has_meter:
        power_flow.pop("consumption")
        power_flow.pop("grid")
    return {
        rf"^cni/ui-api/sites/{SITE_ID}$": load_fixture("cni-site.json"),
        r"site-details/\d+/details$": {"siteId": SITE_ID, "siteName": "Testsite", "peakPower": 3.84, "status": "active"},
        r"site-details/\d+/components$": {
            "siteType": "residential",
            "hasStorage": has_storage,
            "hasConsumptionAndGrid": has_meter,
            "hasPerformanceRatio": False,
            "inverterCount": 1,
            "hasProduction": True,
        },
        r"v2/site-details/\d+/equipment$": {
            "inverters": {"totalCount": 1, "details": [{"modelName": "SE3500H", "serialNumbers": ["ABCD1234-EF"]}]},
            "optimizers": {"totalCount": 1, "details": [{"modelName": "P370", "totalCount": 1}]},
            "storage": {
                "totalCount": 1,
                "details": [
                    {
                        "manufacturer": "SolarEdge",
                        "capacity": 9.7,
                        "serialNumber": "BAT-1",
                        "partNumber": "BAT-10K1P",
                        "softwareVersion": "1.2.3",
                    }
                ],
            }
            if has_storage
            else None,
        },
        r"layout/logical/generic": load_fixture("layout-logical.json"),
        r"custom/site/\d+/v2/devices": [
            {"deviceType": "ONE_PHASE_INVERTER", "deviceSerial": "ABCD1234", "identifier": "ABCD1234"}
        ],
        r"equipment-inverter/": {
            "serialNumber": "ABCD1234-EF",
            "communication": "WIFI",
            "dsp1": "1.0.2207",
            "dsp2": "2.0.1308",
            "commBord": "4.23.36",
            "partNumber": "SE3500H-RW000BNN4",
        },
        r"layout/information/optimizers": load_fixture("optimizers-info.json"),
        r"device-types/ONE_PHASE_INVERTER/metrics/supported": load_fixture("inverter-metrics.json"),
        r"power-flow/v2/sites": power_flow,
        r"live-power/sites": {"currentAcPower": 1180.0, "maxAcPower": 3700.0},
        r"sitelist/sitesMeasurements": load_fixture("sites-measurements.json"),
        r"dashboard/energy/sites": load_fixture("energy.json"),
        r"inverters/energy/sites": {
            "inverterEnergyList": [
                {"inverterName": "Inverter 1", "inverterSerial": "ABCD1234-EF", "inverterOrder": 1, "inverterEnergy": 2493.0}
            ],
            "energyUnit": "watt-hour",
        },
        r"inverters/power/sites": {
            "invertersDatedPowerList": [
                {"measurementTime": "2026-11-20T11:45:00+01:00", "inverterPowerArray": [1100.0]},
                {"measurementTime": "2026-11-20T12:00:00+01:00", "inverterPowerArray": [None]},
            ],
            "inverterDataArray": [{"inverterName": "Inverter 1", "inverterSerial": "ABCD1234-EF", "inverterOrder": 1}],
        },
        r"curtailment/sites": {"limitReasons": [], "powerLimited": False},
        r"battery/sites": {"measurements": [{"measurementTime": "2026-11-20T11:45:00+01:00", "stateOfCharge": 63.0}]},
        r"storage/energy/distribution": {
            "destinations": {"building": 1200.0, "grid": 0.0},
            "sources": {"pv": 2100.0, "grid": 50.0},
        },
        r"generate-chart$": load_fixture("telemetry-chart.json"),
        r"layout/energy/site/\d+/by-inverter": load_fixture("energy-by-inverter.json"),
        r"weather/sites/\d+/weather$": load_fixture("weather.json"),
        r"alertTotalCount": {"totalAlertsCount": 0},
        r"alerts/partialData": {"alerts": [], "numberOfAllReturnedAlerts": 0},
        r"environmental-benefits": {"lifetimeCO2EmissionSaved": 8876.2, "lifetimeEquivalentTreesPlanted": 264.9},
        r"site-details/\d+/communication": {"connectivityStatus": "ONLINE", "active": True},
        r"sitelist/searchSites": load_fixture("site-search.json"),
    }


def build_session(responses: dict[str, Any], errors: dict[str, tuple[int, Any]] | None = None) -> FakeSession:
    """Nep-sessie; ``errors`` (regex → (status, body)) gaat voor de gewone responses."""
    session = FakeSession()
    for pattern, (status, body) in (errors or {}).items():
        session.add("GET", pattern, json_body=body, status=status)
        session.add("POST", pattern, json_body=body, status=status)
    for pattern, body in responses.items():
        session.add("GET", pattern, json_body=copy.deepcopy(body))
        session.add("POST", pattern, json_body=copy.deepcopy(body))
    return session


@pytest.fixture
def api_session() -> FakeSession:
    """Standaard: site zonder batterij en meter."""
    return build_session(site_responses())


@pytest.fixture
def patch_session(api_session: FakeSession) -> Generator[FakeSession]:
    """Laat de integratie de nep-sessie gebruiken."""
    with (
        patch("custom_components.solaredge_monitoring.async_create_clientsession", return_value=api_session),
        patch("custom_components.solaredge_monitoring.config_flow.async_get_clientsession", return_value=api_session),
    ):
        yield api_session


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Config entry met geldige tokens (geen login nodig)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Testsite",
        unique_id=str(SITE_ID),
        data={
            "username": "test@example.com",
            "password": "geheim",
            CONF_SITE_ID: SITE_ID,
            CONF_SITE_NAME: "Testsite",
            CONF_TOKENS: {
                "username": "test@example.com",
                "access_token": make_jwt(),
                "refresh_token": "r",
                "expires_at": 9_999_999_999,
                "id_token": None,
            },
        },
    )

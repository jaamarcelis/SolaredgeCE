"""Setup: verwachte devices, entities en hun eigenschappen, volledig afgeleid uit de (nep-)API."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solaredge_monitoring.const import DOMAIN
from tests.fake_session import FakeSession
from tests.helpers import SITE_ID
from tests.integration.conftest import build_session, site_responses


async def setup(hass: HomeAssistant, entry: MockConfigEntry, session: FakeSession) -> None:
    entry.add_to_hass(hass)
    with patch("custom_components.solaredge_monitoring.async_create_clientsession", return_value=session):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def entity_id(hass: HomeAssistant, unique_id: str, platform: str = "sensor") -> str:
    eid = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert eid, f"geen entity met unique_id {unique_id}"
    return eid


async def test_devices_and_hierarchy(hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession) -> None:
    await setup(hass, config_entry, api_session)
    assert config_entry.state is ConfigEntryState.LOADED
    registry = dr.async_get(hass)

    site = registry.async_get_device_by_identifier((DOMAIN, f"site_{SITE_ID}"), config_entry.entry_id)
    inverter = registry.async_get_device_by_identifier((DOMAIN, "ABCD1234-EF"), config_entry.entry_id)
    optimizer = registry.async_get_device_by_identifier((DOMAIN, "11112222-AB"), config_entry.entry_id)
    assert site and inverter and optimizer
    assert site.name == "Testsite"
    assert site.configuration_url.endswith(f"siteId={SITE_ID}")
    assert inverter.via_device_id == site.id
    assert inverter.model == "SE3500H-RW000BNN4"
    assert inverter.sw_version == "DSP1 1.0.2207 / DSP2 2.0.1308 / COMM 4.23.36"
    assert optimizer.via_device_id == inverter.id
    assert optimizer.model == "P370-5RM4MRM-NM30"
    assert optimizer.model_id == "Unknown 350W"
    # Geen batterij op deze site
    assert not any(
        d.name and d.name.startswith("Batterij") for d in dr.async_entries_for_config_entry(registry, config_entry.entry_id)
    )


async def test_site_sensors(hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession) -> None:
    await setup(hass, config_entry, api_session)

    lifetime = hass.states.get(entity_id(hass, f"{SITE_ID}_site_energy_lifetime"))
    assert lifetime.state == "22643.48"
    assert lifetime.attributes["unit_of_measurement"] == "kWh"
    assert lifetime.attributes["device_class"] == "energy"
    assert lifetime.attributes["state_class"] == "total_increasing"

    power = hass.states.get(entity_id(hass, f"{SITE_ID}_site_current_power"))
    assert float(power.state) == 1250.0  # 1,25 kW → weergegeven in W
    assert power.attributes["unit_of_measurement"] == "W"

    assert hass.states.get(entity_id(hass, f"{SITE_ID}_site_energy_today")).state == "2.492"
    assert hass.states.get(entity_id(hass, f"{SITE_ID}_site_communicating", "binary_sensor")).state == "on"
    assert hass.states.get(entity_id(hass, f"{SITE_ID}_site_power_limited", "binary_sensor")).state == "off"
    # Zonder meter geen verbruikssensoren
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{SITE_ID}_site_consumption_power") is None


async def test_inverter_sensors_and_dynamic_metrics(
    hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession
) -> None:
    await setup(hass, config_entry, api_session)
    registry = er.async_get(hass)
    prefix = f"{SITE_ID}_ABCD1234-EF_"

    assert hass.states.get(entity_id(hass, prefix + "power")).state == "1100.0"
    assert hass.states.get(entity_id(hass, prefix + "energy_today")).state == "2493.0"

    # Kernmetrics: ingeschakeld, eigenschappen uit de metadata, laatste niet-null waarde
    voltage = hass.states.get(entity_id(hass, prefix + "metric_measurements.ac_voltage"))
    assert voltage.state == "232.1"
    assert voltage.attributes["device_class"] == "voltage"
    assert voltage.attributes["unit_of_measurement"] == "V"
    assert voltage.attributes["state_class"] == "measurement"
    assert hass.states.get(entity_id(hass, prefix + "metric_measurements.temperature")).state == "36.5"
    assert hass.states.get(entity_id(hass, prefix + "metric_pv_measurements.power")).state == "1180"
    lifetime = hass.states.get(entity_id(hass, prefix + "metric_measurements.lifetime_positive_ac_energy"))
    assert lifetime.attributes["state_class"] == "total_increasing"
    assert lifetime.state == "22643590"

    # DELTA-metrics worden geen sensor
    assert registry.async_get_entity_id("sensor", DOMAIN, prefix + "metric_pv_measurements.energy_since_last_telemetry") is None
    # Niet-kernmetrics (ook een nieuwe, onbekende) bestaan wel, maar zijn standaard uitgeschakeld
    for key in ("status", "ground_fault_resistance", "brand_new_metric"):
        entry = registry.async_get(entity_id(hass, prefix + f"metric_measurements.{key}"))
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    new_metric = registry.async_get(entity_id(hass, prefix + "metric_measurements.brand_new_metric"))
    assert new_metric.original_device_class == "apparent_power"


async def test_optimizer_sensors(hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession) -> None:
    await setup(hass, config_entry, api_session)
    prefix = f"{SITE_ID}_11112222-AB_"

    energy = hass.states.get(entity_id(hass, prefix + "energy_today"))
    assert energy.state == "181.25"
    assert "position" not in energy.attributes  # staat nu in een eigen diagnostic-entity
    assert hass.states.get(entity_id(hass, prefix + "relative_output")).state == "64.7"
    assert float(hass.states.get(entity_id(hass, prefix + "power")).state) == 3.6328126875
    voltage = er.async_get(hass).async_get(entity_id(hass, prefix + "panel_voltage"))
    assert voltage.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_optimizer_diagnostic_entities(
    hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession
) -> None:
    """Vaste eigenschappen van optimizer en paneel zijn losse diagnostic-entities op het optimizer-device."""
    await setup(hass, config_entry, api_session)
    registry = er.async_get(hass)
    prefix = f"{SITE_ID}_11112222-AB_"
    optimizer = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "11112222-AB"), config_entry.entry_id)

    expected = {
        "position": ("1.1.1", None),
        "panel_manufacturer": ("Unknown", None),
        "panel_model": ("Unknown 350W", None),
        "panel_stc_power": ("350.0", "W"),
        "panel_orientation": ("landscape", None),
        "panel_tilt": ("13.0", "°"),
        "panel_azimuth": ("191.0", "°"),
        "panel_direction": ("South (S)", None),
    }
    for key, (state, unit) in expected.items():
        eid = entity_id(hass, prefix + key)
        entry = registry.async_get(eid)
        assert entry.entity_category is EntityCategory.DIAGNOSTIC, key
        assert entry.device_id == optimizer.id, key
        assert entry.disabled_by is None, key
        current = hass.states.get(eid)
        assert current.state == state, key
        assert current.attributes.get("unit_of_measurement") == unit, key


async def test_panel_direction_uses_ha_language(
    hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession
) -> None:
    hass.config.language = "nl"
    await setup(hass, config_entry, api_session)
    assert hass.states.get(entity_id(hass, f"{SITE_ID}_11112222-AB_panel_direction")).state == "Zuid (Z)"
    assert hass.states.get(entity_id(hass, f"{SITE_ID}_11112222-AB_panel_azimuth")).state == "191.0"


@pytest.mark.parametrize(
    ("degrees", "language", "expected"),
    [
        (0, "en", "North (N)"),
        (359, "en", "North (N)"),
        (90, "nl", "Oost (O)"),
        (135, "nl", "Zuidoost (ZO)"),
        (191, "nl", "Zuid (Z)"),
        (202.5, "en", "South-southwest (SSW)"),
        (247.5, "nl", "Westzuidwest (WZW)"),
        (281, "nl", "West (W)"),
        (292.5, "en", "West-northwest (WNW)"),
        (101, "nl", "Oost (O)"),
        (-90, "en", "West (W)"),
        (180, "nl-BE", "Zuid (Z)"),
        (45, "de", "Northeast (NE)"),  # onbekende taal → Engels
    ],
)
def test_panel_direction_text(degrees: float, language: str, expected: str) -> None:
    from custom_components.solaredge_monitoring.sensor import compass_point, panel_direction  # noqa: PLC0415

    assert panel_direction(degrees, language) == expected
    assert compass_point(degrees, language) == expected.split("(")[1].rstrip(")")
    assert panel_direction(None) is None


async def test_weather_entity(hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession) -> None:
    await setup(hass, config_entry, api_session)
    weather = hass.states.get(entity_id(hass, f"{SITE_ID}_site_weather", "weather"))
    assert weather.state == "rainy"
    assert weather.attributes["temperature"] == 9.7
    assert weather.attributes["humidity"] == 88


async def test_battery_and_meter_site(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    session = build_session(site_responses(has_storage=True, has_meter=True))
    await setup(hass, config_entry, session)
    registry = dr.async_get(hass)

    battery = registry.async_get_device_by_identifier((DOMAIN, "BAT-1"), config_entry.entry_id)
    assert battery is not None
    assert battery.model == "BAT-10K1P 9.7 kWh"
    assert battery.sw_version == "1.2.3"

    prefix = f"{SITE_ID}_BAT-1_"
    assert float(hass.states.get(entity_id(hass, prefix + "state_of_charge")).state) == 64
    assert float(hass.states.get(entity_id(hass, prefix + "battery_power")).state) == 850.0  # laden, positief
    assert hass.states.get(entity_id(hass, prefix + "battery_status")).state == "charging"
    assert hass.states.get(entity_id(hass, prefix + "charged_from_pv_today")).state == "2100.0"

    site = f"{SITE_ID}_site_"
    assert float(hass.states.get(entity_id(hass, site + "consumption_power")).state) == 400.0
    assert hass.states.get(entity_id(hass, site + "grid_status")).state == "idle"
    assert float(hass.states.get(entity_id(hass, site + "grid_export_power")).state) == 0.0


async def test_not_applicable_endpoint_is_skipped(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    session = build_session(
        site_responses(), errors={r"curtailment/sites": (400, {"error": {"code": "BAD_ARGUMENTS", "description": "n.v.t."}})}
    )
    await setup(hass, config_entry, session)
    assert config_entry.state is ConfigEntryState.LOADED
    assert "curtailment" in config_entry.runtime_data.energy.not_applicable
    assert hass.states.get(entity_id(hass, f"{SITE_ID}_site_power_limited", "binary_sensor")).state == "unknown"


async def test_auth_failure_starts_reauth(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    session = build_session(site_responses(), errors={r"cni/ui-api/sites/": (302, None)})
    config_entry.add_to_hass(hass)
    with patch("custom_components.solaredge_monitoring.async_create_clientsession", return_value=session):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_api_down_is_retried(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    session = build_session(site_responses(), errors={r"cni/ui-api/sites/": (500, {"error": "Internal"})})
    config_entry.add_to_hass(hass)
    with patch("custom_components.solaredge_monitoring.async_create_clientsession", return_value=session):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload(hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession) -> None:
    await setup(hass, config_entry, api_session)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics_redacts_personal_data(
    hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession
) -> None:
    from custom_components.solaredge_monitoring.diagnostics import async_get_config_entry_diagnostics  # noqa: PLC0415

    await setup(hass, config_entry, api_session)
    result = await async_get_config_entry_diagnostics(hass, config_entry)
    assert result["entry"]["data"]["password"] == "**REDACTED**"
    assert result["entry"]["data"]["tokens"] == "**REDACTED**"
    assert result["model"]["site"]["location"]["latitude"] == "**REDACTED**"
    assert result["model"]["inverters"][0]["serial"] == "ABCD1234-EF"


async def test_reload_without_deprecated_device_info(
    hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession, caplog
) -> None:
    """Herladen (bijv. na het in-/uitschakelen van een entity) mag geen deprecated via_device gebruiken."""
    await setup(hass, config_entry, api_session)
    with patch("custom_components.solaredge_monitoring.async_create_clientsession", return_value=api_session):
        assert await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(entity_id(hass, f"{SITE_ID}_ABCD1234-EF_power")).state == "1100.0"
    assert "via_device" not in caplog.text
    assert "Error adding entity" not in caplog.text


async def test_metric_names_do_not_collide_with_fixed_sensors(
    hass: HomeAssistant, config_entry: MockConfigEntry, api_session: FakeSession
) -> None:
    """pv_measurements.power heet in de API ook "Power"; dat moet onderscheidbaar zijn van de vaste sensor."""
    await setup(hass, config_entry, api_session)
    registry = er.async_get(hass)
    dc = registry.async_get(entity_id(hass, f"{SITE_ID}_ABCD1234-EF_metric_pv_measurements.power"))
    assert dc.original_name == "Power (PV measurements)"
    assert not dc.entity_id.endswith("_2")

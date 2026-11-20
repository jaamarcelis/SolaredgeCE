"""Config flow: login, MFA, siteselectie, foutmeldingen en reauth."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solaredge_monitoring.api import AuthTokens, InvalidCredentialsError, MfaRequiredError
from custom_components.solaredge_monitoring.const import CONF_SITE_ID, CONF_TOKENS, DOMAIN
from tests.fake_session import FakeSession
from tests.helpers import SITE_ID, make_jwt

AUTH = "custom_components.solaredge_monitoring.config_flow.CognitoAuthenticator"


def tokens() -> AuthTokens:
    return AuthTokens("test@example.com", make_jwt(), "refresh", 9_999_999_999)


async def start(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_user_flow_single_site(hass: HomeAssistant, patch_session: FakeSession) -> None:
    with patch(AUTH) as auth:
        auth.return_value.async_login = AsyncMock(return_value=tokens())
        result = await start(hass)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": " test@example.com ", "password": "geheim"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Testsite"
    assert result["result"].unique_id == str(SITE_ID)
    assert result["data"][CONF_SITE_ID] == SITE_ID
    assert result["data"]["username"] == "test@example.com"
    assert result["data"][CONF_TOKENS]["refresh_token"] == "refresh"
    auth.return_value.async_login.assert_awaited_once_with("test@example.com", "geheim")


async def test_invalid_password(hass: HomeAssistant, patch_session: FakeSession) -> None:
    with patch(AUTH) as auth:
        auth.return_value.async_login = AsyncMock(side_effect=InvalidCredentialsError("nope"))
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "a@b.nl", "password": "fout"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_mfa_flow(hass: HomeAssistant, patch_session: FakeSession) -> None:
    mfa = MfaRequiredError("SOFTWARE_TOKEN_MFA", "sess", "uid")
    with patch(AUTH) as auth:
        auth.return_value.async_login = AsyncMock(side_effect=mfa)
        auth.return_value.async_respond_to_mfa = AsyncMock(side_effect=[InvalidCredentialsError("bad"), tokens()])
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "a@b.nl", "password": "pw"})
        assert result["step_id"] == "mfa"
        assert result["description_placeholders"] == {"mfa_type": "authenticator-app"}

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mfa_code": "000000"})
        assert result["errors"] == {"base": "invalid_mfa"}

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mfa_code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    auth.return_value.async_respond_to_mfa.assert_awaited_with(mfa, "123456", "a@b.nl")


async def test_multiple_sites_shows_selection(hass: HomeAssistant, patch_session: FakeSession) -> None:
    patch_session._routes.insert(
        0,
        (
            "POST",
            __import__("re").compile("searchSites"),
            [
                lambda _c: __import__("tests.fake_session", fromlist=["FakeResponse"]).FakeResponse(
                    200,
                    '{"page":[{"solarFieldId":1,"name":"Huis"},{"solarFieldId":2,"name":"Schuur"}]}',
                )
            ],
            True,
        ),
    )
    with patch(AUTH) as auth:
        auth.return_value.async_login = AsyncMock(return_value=tokens())
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "a@b.nl", "password": "pw"})
        assert result["step_id"] == "site"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SITE_ID: "2"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Schuur"
    assert result["data"][CONF_SITE_ID] == 2


async def test_already_configured(hass: HomeAssistant, patch_session: FakeSession, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    with patch(AUTH) as auth:
        auth.return_value.async_login = AsyncMock(return_value=tokens())
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "a@b.nl", "password": "pw"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password_and_tokens(
    hass: HomeAssistant, patch_session: FakeSession, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    new_tokens = tokens()
    with (
        patch(AUTH) as auth,
        patch("custom_components.solaredge_monitoring.async_setup_entry", return_value=True),
    ):
        auth.return_value.async_login = AsyncMock(return_value=new_tokens)
        result = await config_entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "nieuw"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["password"] == "nieuw"
    assert config_entry.data[CONF_TOKENS]["access_token"] == new_tokens.access_token


async def test_options_flow_lists_discovered_metrics(
    hass: HomeAssistant, patch_session: FakeSession, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["settings", "import_device"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    schema = result["data_schema"].schema
    metric_selector = next(v for k, v in schema.items() if str(k) == "telemetry_metrics")
    values = {o["value"] for o in metric_selector.config["options"]}
    assert "measurements.ac_voltage" in values
    assert "measurements.brand_new_metric" in values
    assert "pv_measurements.energy_since_last_telemetry" not in values  # DELTA

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "live_interval": 120,
            "energy_interval": 10,
            "telemetry_interval": 30,
            "panels_interval": 30,
            "info_interval": 60,
            "include_optimizers": False,
            "telemetry_metrics": ["measurements.ac_voltage"],
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    runtime = config_entry.runtime_data
    assert runtime.panels is None
    assert runtime.live.update_interval.total_seconds() == 120
    assert [s.key for s in runtime.telemetry.active_metrics("ONE_PHASE_INVERTER")] == ["measurements.ac_voltage"]

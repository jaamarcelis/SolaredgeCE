"""Config flow: inloggen (met MFA), site kiezen, opnieuw inloggen (reauth) en opties."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    AuthenticationError,
    AuthTokens,
    CognitoAuthenticator,
    InvalidCredentialsError,
    MfaRequiredError,
    SolarEdgeError,
    SolarEdgeMonitoringClient,
)
from .const import (
    CONF_ENERGY_INTERVAL,
    CONF_INCLUDE_OPTIMIZERS,
    CONF_INFO_INTERVAL,
    CONF_LIVE_INTERVAL,
    CONF_PANELS_INTERVAL,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    CONF_TELEMETRY_INTERVAL,
    CONF_TELEMETRY_METRICS,
    CONF_TOKENS,
    DEFAULT_ENERGY_INTERVAL,
    DEFAULT_INFO_INTERVAL,
    DEFAULT_LIVE_INTERVAL,
    DEFAULT_PANELS_INTERVAL,
    DEFAULT_TELEMETRY_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_MFA_CODE = "mfa_code"

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)
MFA_SCHEMA = vol.Schema({vol.Required(CONF_MFA_CODE): str})


class SolarEdgeMonitoringConfigFlow(ConfigFlow, domain=DOMAIN):
    """Inloggen bij monitoring.solaredge.com en een site kiezen."""

    VERSION = 1

    def __init__(self) -> None:
        """Lege flow."""
        self._username = ""
        self._password = ""
        self._mfa: MfaRequiredError | None = None
        self._tokens: AuthTokens | None = None
        self._sites: dict[str, tuple[str, str | None]] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SolarEdgeOptionsFlow:
        """Opties: intervallen, optimizers en telemetriemetrics."""
        return SolarEdgeOptionsFlow()

    def _authenticator(self) -> CognitoAuthenticator:
        return CognitoAuthenticator(async_get_clientsession(self.hass))

    async def _login(self, errors: dict[str, str]) -> bool:
        """Log in met de opgegeven gegevens; ``True`` als er tokens zijn."""
        try:
            self._tokens = await self._authenticator().async_login(self._username, self._password)
            return True
        except MfaRequiredError as err:
            self._mfa = err
        except InvalidCredentialsError:
            errors["base"] = "invalid_auth"
        except (AuthenticationError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Inloggen bij SolarEdge mislukt: %s", err)
            errors["base"] = "cannot_connect"
        return False

    # --- Nieuwe installatie ----------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """E-mailadres en wachtwoord."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]
            if await self._login(errors):
                return await self.async_step_site()
            if self._mfa:
                return await self.async_step_mfa()
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input), errors=errors
        )

    async def async_step_mfa(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """MFA-code (sms of authenticator-app)."""
        errors: dict[str, str] = {}
        assert self._mfa is not None
        if user_input is not None:
            try:
                self._tokens = await self._authenticator().async_respond_to_mfa(
                    self._mfa, user_input[CONF_MFA_CODE], self._username
                )
            except InvalidCredentialsError:
                errors["base"] = "invalid_mfa"
            except (AuthenticationError, aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                self._mfa = None
                if self.source == "reauth":
                    return self._finish_reauth()
                return await self.async_step_site()
        return self.async_show_form(
            step_id="mfa",
            data_schema=MFA_SCHEMA,
            errors=errors,
            description_placeholders={"mfa_type": "sms" if self._mfa.mfa_type == "SMS_MFA" else "authenticator-app"},
        )

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Kies de site (automatisch als er maar één is)."""
        errors: dict[str, str] = {}
        if not self._sites:
            assert self._tokens is not None
            client = SolarEdgeMonitoringClient(async_get_clientsession(self.hass), bearer_token=self._tokens.access_token)
            try:
                result = await client.site_list.search_sites()
            except SolarEdgeError as err:
                _LOGGER.warning("Sites ophalen mislukt: %s", err)
                return self.async_abort(reason="cannot_connect")
            self._sites = {str(s.solar_field_id): (s.name or str(s.solar_field_id), s.time_zone) for s in result.page}
            if not self._sites:
                return self.async_abort(reason="no_sites")

        if user_input is None and len(self._sites) == 1:
            user_input = {CONF_SITE_ID: next(iter(self._sites))}

        if user_input is not None:
            site_id = user_input[CONF_SITE_ID]
            await self.async_set_unique_id(site_id)
            self._abort_if_unique_id_configured()
            name = self._sites[site_id][0]
            assert self._tokens is not None
            return self.async_create_entry(
                title=name,
                data={
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                    CONF_SITE_ID: int(site_id),
                    CONF_SITE_NAME: name,
                    CONF_TOKENS: self._tokens.as_dict(),
                },
            )

        options = [SelectOptionDict(value=sid, label=f"{name} ({sid})") for sid, (name, _) in self._sites.items()]
        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema({vol.Required(CONF_SITE_ID): SelectSelector(SelectSelectorConfig(options=options))}),
            errors=errors,
        )

    # --- Opnieuw inloggen ------------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start opnieuw inloggen (wachtwoord gewijzigd, refresh verlopen of MFA nodig)."""
        self._username = entry_data[CONF_USERNAME]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Wachtwoord opnieuw invoeren."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            if await self._login(errors):
                return self._finish_reauth()
            if self._mfa:
                return await self.async_step_mfa()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
            ),
            errors=errors,
            description_placeholders={"username": self._username},
        )

    def _finish_reauth(self) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        assert self._tokens is not None
        return self.async_update_reload_and_abort(
            entry, data_updates={CONF_PASSWORD: self._password, CONF_TOKENS: self._tokens.as_dict()}
        )


class SolarEdgeOptionsFlow(OptionsFlowWithReload):
    """Pollintervallen, optimizer-devices en welke (dynamisch ontdekte) telemetriemetrics opgehaald worden."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Eén formulier met alle opties."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        metric_options: list[SelectOptionDict] = []
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is not None:
            seen: set[str] = set()
            for specs in runtime.model.metrics.values():
                for spec in specs:
                    if spec.key not in seen:
                        seen.add(spec.key)
                        unit = f" ({spec.unit})" if spec.unit else ""
                        metric_options.append(SelectOptionDict(value=spec.key, label=f"{spec.name}{unit} – {spec.category}"))

        def minutes(default: int, maximum: int) -> NumberSelector:
            return NumberSelector(
                NumberSelectorConfig(min=1, max=maximum, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min")
            )

        schema: dict[Any, Any] = {
            vol.Required(CONF_LIVE_INTERVAL, default=options.get(CONF_LIVE_INTERVAL, DEFAULT_LIVE_INTERVAL)): NumberSelector(
                NumberSelectorConfig(min=15, max=3600, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
            ),
            vol.Required(CONF_ENERGY_INTERVAL, default=options.get(CONF_ENERGY_INTERVAL, DEFAULT_ENERGY_INTERVAL)): minutes(
                DEFAULT_ENERGY_INTERVAL, 120
            ),
            vol.Required(
                CONF_TELEMETRY_INTERVAL, default=options.get(CONF_TELEMETRY_INTERVAL, DEFAULT_TELEMETRY_INTERVAL)
            ): minutes(DEFAULT_TELEMETRY_INTERVAL, 240),
            vol.Required(CONF_PANELS_INTERVAL, default=options.get(CONF_PANELS_INTERVAL, DEFAULT_PANELS_INTERVAL)): minutes(
                DEFAULT_PANELS_INTERVAL, 240
            ),
            vol.Required(CONF_INFO_INTERVAL, default=options.get(CONF_INFO_INTERVAL, DEFAULT_INFO_INTERVAL)): minutes(
                DEFAULT_INFO_INTERVAL, 1440
            ),
            vol.Required(CONF_INCLUDE_OPTIMIZERS, default=options.get(CONF_INCLUDE_OPTIMIZERS, True)): bool,
        }
        if metric_options:
            schema[vol.Optional(CONF_TELEMETRY_METRICS, default=options.get(CONF_TELEMETRY_METRICS, []))] = SelectSelector(
                SelectSelectorConfig(options=metric_options, multiple=True)
            )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))

"""SolarEdge Monitoring: Home Assistant-integratie voor de interne API van monitoring.solaredge.com."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import (
    AuthenticationError,
    AuthTokens,
    CognitoAuthenticator,
    SolarEdgeError,
    SolarEdgeMonitoringClient,
    TokenManager,
)
from .const import CONF_SITE_ID, CONF_TOKENS, DOMAIN
from .coordinator import SolarEdgeRuntimeData
from .discovery import async_discover
from .entity import async_register_devices
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.WEATHER]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type SolarEdgeConfigEntry = ConfigEntry[SolarEdgeRuntimeData]


def create_client(hass: HomeAssistant, entry: ConfigEntry) -> SolarEdgeMonitoringClient:
    """Client met eigen sessie (cookie jar voor Cloudflare) en tokenbeheer dat nieuwe tokens in de entry bewaart."""
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())

    def save_tokens(tokens: AuthTokens) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKENS: tokens.as_dict()})

    manager = TokenManager(
        CognitoAuthenticator(session),
        entry.data[CONF_USERNAME],
        entry.data.get(CONF_PASSWORD),
        AuthTokens.from_dict(entry.data.get(CONF_TOKENS)),
        save_tokens,
    )
    return SolarEdgeMonitoringClient(session, manager)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Registreer de acties van de integratie."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Ontdek de site, start de coordinators en de platforms."""
    client = create_client(hass, entry)
    try:
        model = await async_discover(client, entry.data[CONF_SITE_ID])
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except (SolarEdgeError, aiohttp.ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady(f"SolarEdge niet bereikbaar: {err}") from err

    runtime = SolarEdgeRuntimeData.create(hass, entry, client, model)
    runtime.static.async_set_updated_data({"model": model})
    await asyncio.gather(*(c.async_config_entry_first_refresh() for c in runtime.coordinators))
    entry.runtime_data = runtime

    # Devices eerst registreren (ouders voor kinderen), daarna de entities
    async_register_devices(hass, entry, model, include_optimizers=runtime.panels is not None)

    # Na een wijziging van de opties herlaadt OptionsFlowWithReload de integratie zelf
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Stop de platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

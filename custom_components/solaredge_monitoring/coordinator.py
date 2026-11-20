"""DataUpdateCoordinators: elke soort data met een eigen, passend pollinterval.

| Coordinator | Endpoints                                                                    | Standaard |
|-------------|------------------------------------------------------------------------------|-----------|
| static      | discovery (site, apparaten, metrics)                                         | 24 uur    |
| live        | power-flow v2, live-power                                                    | 60 s      |
| energy      | sitesMeasurements, energie vandaag, omvormers, batterij, curtailment         | 5 min     |
| telemetry   | generate-chart voor omvormers (ontdekte metrics)                             | 15 min    |
| panels      | energie per paneel + live data per optimizer                                 | 15 min    |
| info        | weer, alerts, milieuvoordelen, communicatie                                  | 30 min    |
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AuthenticationError, NotApplicableError, SolarEdgeError, SolarEdgeMonitoringClient, measurement_datasource
from .api.const import ChartTimeUnit, MeasurementType
from .api.models import GenerateChartResponse
from .const import (
    CONF_ENERGY_INTERVAL,
    CONF_INCLUDE_OPTIMIZERS,
    CONF_INFO_INTERVAL,
    CONF_LIVE_INTERVAL,
    CONF_PANELS_INTERVAL,
    CONF_TELEMETRY_INTERVAL,
    CONF_TELEMETRY_METRICS,
    DEFAULT_ENERGY_INTERVAL,
    DEFAULT_INFO_INTERVAL,
    DEFAULT_LIVE_INTERVAL,
    DEFAULT_PANELS_INTERVAL,
    DEFAULT_TELEMETRY_INTERVAL,
    DOMAIN,
    OPTIMIZER_BATCH_SIZE,
    STATIC_INTERVAL,
    TELEMETRY_WINDOW,
)
from .discovery import SiteModel, async_discover

_LOGGER = logging.getLogger(__name__)


class SolarEdgeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Basis: vertaalt API-fouten naar HA-gedrag en slaat endpoints over die niet van toepassing zijn."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SolarEdgeMonitoringClient,
        model: SiteModel,
        name: str,
        interval: timedelta,
    ) -> None:
        """Maak een coordinator voor ``name`` met ``interval``."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {name}", update_interval=interval)
        self.client = client
        self.model = model
        self.not_applicable: set[str] = set()

    @property
    def site_id(self) -> int:
        """Site-ID."""
        return self.model.site_id

    def today(self) -> date:
        """Vandaag in de tijdzone van de site."""
        return dt_util.now(ZoneInfo(self.model.timezone)).date()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._fetch()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (SolarEdgeError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"{self.name}: {err}") from err

    async def _fetch(self) -> dict[str, Any]:
        raise NotImplementedError

    async def optional[T](self, key: str, awaitable: Awaitable[T]) -> T | None:
        """Haal ``awaitable`` op; is het endpoint niet van toepassing, dan wordt het voortaan overgeslagen."""
        if key in self.not_applicable:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[union-attr]
            return None
        try:
            return await awaitable
        except NotApplicableError as err:
            _LOGGER.info("%s is niet van toepassing op site %s en wordt overgeslagen: %s", key, self.site_id, err)
            self.not_applicable.add(key)
            return None


def _minutes(entry: ConfigEntry, key: str, default: int) -> timedelta:
    return timedelta(minutes=entry.options.get(key, default))


class StaticCoordinator(SolarEdgeCoordinator):
    """Ververst het site-model dagelijks; bij nieuwe of verdwenen apparaten herlaadt de integratie."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SolarEdgeMonitoringClient, model: SiteModel) -> None:
        """Start met het model van de eerste discovery."""
        super().__init__(hass, entry, client, model, "static", STATIC_INTERVAL)

    async def _fetch(self) -> dict[str, Any]:
        model = await async_discover(self.client, self.site_id)
        if model.device_signature() != self.model.device_signature() and self.config_entry:
            _LOGGER.info("Apparaten of metrics van site %s gewijzigd; integratie wordt herladen", self.site_id)
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        self.model = model
        return {"model": model}


class LiveCoordinator(SolarEdgeCoordinator):
    """Live energiestroom (kW) en actueel AC-vermogen (W)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SolarEdgeMonitoringClient, model: SiteModel) -> None:
        """Interval uit de opties (seconden)."""
        super().__init__(
            hass, entry, client, model, "live", timedelta(seconds=entry.options.get(CONF_LIVE_INTERVAL, DEFAULT_LIVE_INTERVAL))
        )

    async def _fetch(self) -> dict[str, Any]:
        return {
            "power_flow": await self.client.dashboard.get_power_flow(self.site_id),
            "live_power": await self.optional("live_power", self.client.dashboard.get_live_power(self.site_id)),
        }


class EnergyCoordinator(SolarEdgeCoordinator):
    """Energietotalen, energie vandaag (met verdeling bij een meter), per omvormer, batterij en curtailment."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SolarEdgeMonitoringClient, model: SiteModel) -> None:
        """Interval uit de opties (minuten)."""
        super().__init__(hass, entry, client, model, "energy", _minutes(entry, CONF_ENERGY_INTERVAL, DEFAULT_ENERGY_INTERVAL))

    async def _fetch(self) -> dict[str, Any]:
        today = self.today()
        site = self.site_id
        dashboard = self.client.dashboard
        types = [*MeasurementType.for_site(self.model.has_meter, self.model.has_storage), MeasurementType.YIELD]
        data: dict[str, Any] = {
            "measurements": await self.client.site_list.get_site_measurements(site),
            "energy_today": await self.optional(
                "energy_today", dashboard.get_energy(site, today, today, ChartTimeUnit.DAYS, types)
            ),
            "inverters_energy": await self.optional("inverters_energy", dashboard.get_inverters_energy(site, today, today)),
            "inverters_power": await self.optional("inverters_power", dashboard.get_inverters_power(site, today, today)),
            "curtailment": await self.optional("curtailment", dashboard.get_curtailment(site, today, today)),
        }
        if self.model.has_storage:
            data["battery"] = await self.optional("battery", dashboard.get_battery(site, today, today))
            data["storage_distribution"] = await self.optional(
                "storage_distribution", dashboard.get_storage_energy_distribution(site, today, today)
            )
        return data


@dataclass(slots=True)
class TelemetryValue:
    """Laatste niet-null waarde van een metric."""

    value: Any
    measured_at: datetime


@dataclass(slots=True)
class TelemetryData:
    """Per omvormer-ID (kort) en metric-sleutel de laatste waarde."""

    values: dict[str, dict[str, TelemetryValue]] = field(default_factory=dict)


class TelemetryCoordinator(SolarEdgeCoordinator):
    """Omvormertelemetrie via generate-chart, met de dynamisch ontdekte metrics."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SolarEdgeMonitoringClient, model: SiteModel) -> None:
        """Interval uit de opties (minuten)."""
        super().__init__(
            hass, entry, client, model, "telemetry", _minutes(entry, CONF_TELEMETRY_INTERVAL, DEFAULT_TELEMETRY_INTERVAL)
        )
        self._selected: set[str] = set(entry.options.get(CONF_TELEMETRY_METRICS) or [])

    def active_metrics(self, device_type: str) -> list:
        """De metrics die opgehaald worden: de selectie uit de opties, of alle ontdekte."""
        specs = self.model.metrics.get(device_type, [])
        return [s for s in specs if not self._selected or s.key in self._selected]

    async def _fetch(self) -> dict[str, Any]:
        end = dt_util.utcnow()
        start = end - TELEMETRY_WINDOW
        result = TelemetryData()
        by_type: dict[str, list[str]] = {}
        for inverter in self.model.inverters:
            by_type.setdefault(inverter.analysis_type, []).append(inverter.identifier)

        for device_type, identifiers in by_type.items():
            metrics = self.active_metrics(device_type)
            if not metrics:
                continue
            # Eén datasource per categorie (zoals de webapp)
            categories: dict[str, list[tuple[str, str]]] = {}
            for spec in metrics:
                categories.setdefault(spec.category, []).append((spec.category, spec.uri))
            datasources = [
                measurement_datasource(self.site_id, device_type, identifiers, pairs, "FIVE_MINUTES")
                for pairs in categories.values()
            ]
            chart = await self.client.analysis.generate_chart(self.site_id, start, end, datasources)
            _collect(chart, result)
        return {"telemetry": result}


def _collect(chart: GenerateChartResponse, result: TelemetryData) -> None:
    if not chart.meta:
        return
    for index, dataset in enumerate(chart.meta.datasets_meta):
        entity_id = dataset.report_object.entity_id if dataset.report_object else ""
        values = result.values.setdefault(entity_id, {})
        for uri, (timestamp, value) in chart.latest(index).items():
            values[f"{dataset.category}.{uri}"] = TelemetryValue(value, timestamp)


class PanelsCoordinator(SolarEdgeCoordinator):
    """Energie per paneel vandaag en de laatste meting per optimizer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SolarEdgeMonitoringClient, model: SiteModel) -> None:
        """Interval uit de opties (minuten)."""
        super().__init__(hass, entry, client, model, "panels", _minutes(entry, CONF_PANELS_INTERVAL, DEFAULT_PANELS_INTERVAL))

    async def _fetch(self) -> dict[str, Any]:
        today = self.today()
        energy: dict[str, Any] = {}
        live: dict[str, Any] = {}
        inverter_serials = [i.serial for i in self.model.inverters]
        if inverter_serials:
            by_inverter = await self.client.layout.get_energy_by_inverter(self.site_id, today, today, inverter_serials)
            for inverter in by_inverter.inverters:
                for optimizer in inverter.optimizers:
                    if optimizer.serial:
                        energy[optimizer.serial] = optimizer
        serials = [o.serial for o in self.model.optimizers]
        for start in range(0, len(serials), OPTIMIZER_BATCH_SIZE):
            info = await self.client.layout.get_optimizers_information(serials[start : start + OPTIMIZER_BATCH_SIZE])
            live.update(info.serial_to_live_data)
        return {"energy": energy, "live": live}


class InfoCoordinator(SolarEdgeCoordinator):
    """Weer, alerts, milieuvoordelen en communicatiestatus."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SolarEdgeMonitoringClient, model: SiteModel) -> None:
        """Interval uit de opties (minuten)."""
        super().__init__(hass, entry, client, model, "info", _minutes(entry, CONF_INFO_INTERVAL, DEFAULT_INFO_INTERVAL))

    async def _fetch(self) -> dict[str, Any]:
        site = self.site_id
        return {
            "weather": await self.optional("weather", self.client.weather.get_weather(site)),
            "alerts_total": await self.optional("alerts_total", self.client.alerts.get_total_count(site)),
            "alerts": await self.optional("alerts", self.client.alerts.get_alerts(site)),
            "environmental": await self.optional("environmental", self.client.dashboard.get_environmental_benefits(site)),
            "communication": await self.optional("communication", self.client.dashboard.get_communication(site)),
        }


@dataclass(slots=True)
class SolarEdgeRuntimeData:
    """Alles wat de platforms nodig hebben (``entry.runtime_data``)."""

    client: SolarEdgeMonitoringClient
    model: SiteModel
    static: StaticCoordinator
    live: LiveCoordinator
    energy: EnergyCoordinator
    telemetry: TelemetryCoordinator
    panels: PanelsCoordinator | None
    info: InfoCoordinator
    history_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # één historie-import tegelijk

    @classmethod
    def create(
        cls, hass: HomeAssistant, entry: ConfigEntry, client: SolarEdgeMonitoringClient, model: SiteModel
    ) -> SolarEdgeRuntimeData:
        """Maak alle coordinators; ``panels`` alleen als optimizers ingeschakeld zijn en er zijn."""
        include_optimizers = entry.options.get(CONF_INCLUDE_OPTIMIZERS, True) and bool(model.optimizers)
        return cls(
            client=client,
            model=model,
            static=StaticCoordinator(hass, entry, client, model),
            live=LiveCoordinator(hass, entry, client, model),
            energy=EnergyCoordinator(hass, entry, client, model),
            telemetry=TelemetryCoordinator(hass, entry, client, model),
            panels=PanelsCoordinator(hass, entry, client, model) if include_optimizers else None,
            info=InfoCoordinator(hass, entry, client, model),
        )

    @property
    def coordinators(self) -> list[SolarEdgeCoordinator]:
        """Alle actieve coordinators."""
        return [c for c in (self.live, self.energy, self.telemetry, self.panels, self.info) if c is not None]

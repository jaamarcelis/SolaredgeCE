"""Client voor de interne API van monitoring.solaredge.com (Python-versie van de .NET-client).

Alle paden zijn relatief aan ``https://monitoring.solaredge.com/services/``. Elke sub-client hoort bij één
backend-service. Methoden die ``Any`` teruggeven hebben een (nog) niet gemodelleerde response.
Schrijvende acties zitten er bewust niet in.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
import time
from typing import Any

import aiohttp

from .auth import TokenManager
from .const import SITES_MEASUREMENT_TYPES, ChartPeriodScale, ChartTimeUnit, MeasurementType
from .models import (
    AlertPage,
    AnalysisDevice,
    AvailableReport,
    BatteryChart,
    CniSite,
    Curtailment,
    DataAvailability,
    EnergyByInverter,
    EnvironmentalBenefits,
    GenerateChartResponse,
    InverterEquipmentDetails,
    InvertersEnergy,
    InvertersPower,
    LayoutInformation,
    LivePower,
    OptimizersInformation,
    PowerFlow,
    SiteCommunication,
    SiteComponents,
    SiteDetails,
    SiteEnergy,
    SiteEquipment,
    SiteLayout,
    SiteMeasurements,
    SitePower,
    SiteSearchResult,
    SiteUserActions,
    SiteWeather,
    StorageEnergyDistribution,
    SunTimes,
    SupportedMetrics,
    UserProfile,
    parse,
)
from .transport import Transport


def default_site_filter(site_name: str = "", page_num: int = 1, sites_in_page: int = 20) -> dict[str, Any]:
    """Filter-body van de installatielijst (zoals de webapp zonder filters)."""
    return {
        "pageRequest": {
            "sitesInPage": sites_in_page,
            "pageNum": page_num,
            "sortRequest": {"sortColumnType": "maxImpact", "sortOrder": "DESC"},
        },
        "geoBoundingBox": None,
        "locationFilter": {"countries": [], "states": [], "city": "", "address": "", "zip": ""},
        "peakPowerFilter": {"min": 0, "max": 1_000_000},
        "maxImpactFilter": {"min": 0, "max": 9},
        "installationDateFilter": {},
        "statusFilter": [],
        "serialNumber": "",
        "siteNameFilter": site_name,
        "accountNameFilter": [],
        "groupFilter": "",
        "favoriteFilter": False,
        "devicesFilter": {},
        "demoSitesFilter": False,
        "siteMagnitudeFilter": None,
    }


def utc_timestamp(value: datetime) -> str:
    """``2026-11-17T23:00:00.000Z``, het formaat van request-bodies."""
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


class _Sub:
    def __init__(self, transport: Transport) -> None:
        self._t = transport


class UserClient(_Sub):
    """Gebruiker, feature flags en overeenkomsten."""

    async def get_user(self) -> UserProfile:
        """``GET user-management/user``."""
        return parse(UserProfile, await self._t.get("user-management/user"))

    async def get_user_info(self) -> Any:
        """``GET cni/ui-api/user-info``."""
        return await self._t.get("cni/ui-api/user-info")

    async def get_user_feature_flags(self) -> list[str]:
        """``GET cni/ui-api/feature-flags/user`` (namen)."""
        return [f.get("name") for f in await self._t.get("cni/ui-api/feature-flags/user") or []]

    async def get_site_feature_flags(self, site_id: int) -> list[str]:
        """``GET cni/ui-api/feature-flags/site/{id}`` (namen)."""
        return [f.get("name") for f in await self._t.get(f"cni/ui-api/feature-flags/site/{site_id}") or []]

    async def are_feature_flags_enabled(self, names: Iterable[str]) -> dict[str, bool]:
        """``GET installer-infra/is-feature-flags-enabled?feature-flag-names[]=…``."""
        return await self._t.get("installer-infra/is-feature-flags-enabled", [("feature-flag-names[]", n) for n in names]) or {}

    async def are_site_feature_flags_enabled(self, site_id: int, names: Iterable[str]) -> dict[str, bool]:
        """``GET installer-infra/is-feature-flag-enabled/{id}?feature-flag-names[]=…``."""
        return (
            await self._t.get(f"installer-infra/is-feature-flag-enabled/{site_id}", [("feature-flag-names[]", n) for n in names])
            or {}
        )

    async def get_enabled_features(self, application: str = "DigitalTwin") -> list[str]:
        """``GET feature-access/enabled?application=…`` (namen)."""
        return [f.get("name") for f in await self._t.get("feature-access/enabled", [("application", application)]) or []]

    async def get_privacy_policy_status(self) -> Any:
        """``GET installer-infra/dpa/privacy-policy``."""
        return await self._t.get("installer-infra/dpa/privacy-policy")

    async def get_terms_and_conditions_status(self) -> Any:
        """``GET installer-infra/dpa/terms-and-conditions``."""
        return await self._t.get("installer-infra/dpa/terms-and-conditions")

    async def get_accounts_tab_permissions(self) -> Any:
        """``GET account-admin/management/ui-permissions/accounts-tab``."""
        return await self._t.get("account-admin/management/ui-permissions/accounts-tab")


class SitesClient(_Sub):
    """Site-informatie en rechten (CNI-UI-API)."""

    async def get_site(self, site_id: int) -> CniSite:
        """``GET cni/ui-api/sites/{id}``."""
        return parse(CniSite, await self._t.get(f"cni/ui-api/sites/{site_id}"))

    async def get_user_actions(self, site_id: int) -> SiteUserActions:
        """``GET cni/ui-api/sites/{id}/user-actions``."""
        return parse(SiteUserActions, await self._t.get(f"cni/ui-api/sites/{site_id}/user-actions"))

    async def get_pages(self, site_id: int) -> Any:
        """``GET cni/ui-api/sites/{id}/pages``."""
        return await self._t.get(f"cni/ui-api/sites/{site_id}/pages")

    async def get_tabs(self, site_id: int) -> list[str]:
        """``GET installer-infra/sites/{id}/tabs``."""
        return await self._t.get(f"installer-infra/sites/{site_id}/tabs") or []

    async def get_fleet(self, site_id: int) -> Any:
        """``GET cni/ui-api/sites/{id}/fleet`` (leeg bij een eigenaar)."""
        return await self._t.get(f"cni/ui-api/sites/{site_id}/fleet")


class SiteListClient(_Sub):
    """Installatielijst: zoeken, kerncijfers en vlootoverzicht."""

    async def count(self, statuses: Iterable[str] = ("ACTIVE",)) -> int:
        """``GET sitelist/count?status[]=…``."""
        return int(await self._t.get("sitelist/count", [("status[]", s) for s in statuses]) or 0)

    async def get_metadata(self) -> Any:
        """``GET sitelist/metaData``."""
        return await self._t.get("sitelist/metaData")

    async def get_all_attributes(self) -> Any:
        """``GET sitelist/allAtributes`` (sic)."""
        return await self._t.get("sitelist/allAtributes")

    async def get_groups(self) -> list[Any]:
        """``GET sitelist/groups``."""
        return await self._t.get("sitelist/groups") or []

    async def is_asset_owner(self) -> bool:
        """``GET sitelist/isAssetOwner``."""
        return bool(await self._t.get("sitelist/isAssetOwner"))

    async def is_add_site_permitted(self) -> bool:
        """``GET sitelist/isAddSitePermitted``."""
        return bool(await self._t.get("sitelist/isAddSitePermitted"))

    async def search_sites(self, site_filter: dict[str, Any] | None = None) -> SiteSearchResult:
        """``POST sitelist/searchSites?v={timestamp}``."""
        return parse(
            SiteSearchResult,
            await self._t.post("sitelist/searchSites", site_filter or default_site_filter(), [("v", int(time.time() * 1000))]),
        )

    async def get_fleet_data(self, site_filter: dict[str, Any] | None = None) -> Any:
        """``POST sitelist/fleetData``."""
        return await self._t.post("sitelist/fleetData", site_filter or default_site_filter())

    async def get_fleet_energy_data(
        self, site_filter: dict[str, Any] | None = None, fleet_time_unit: str = "YESTERDAY", timezone: str = "Europe/Amsterdam"
    ) -> Any:
        """``POST sitelist/fleetEnergyData`` (kWh)."""
        return await self._t.post(
            "sitelist/fleetEnergyData",
            site_filter or default_site_filter(),
            [("fleet-time-unit", fleet_time_unit), ("timezone", timezone)],
        )

    async def get_sites_measurements(
        self, site_ids: Iterable[int], measurement_types: Iterable[str] = SITES_MEASUREMENT_TYPES
    ) -> list[SiteMeasurements]:
        """``POST sitelist/sitesMeasurements``: energie vandaag/maand/jaar/lifetime (kWh), rendement, PR."""
        body = {"measurementsTypes": list(measurement_types), "sites": list(site_ids)}
        return parse(SiteMeasurements, await self._t.post("sitelist/sitesMeasurements", body)) or []

    async def get_site_measurements(self, site_id: int) -> SiteMeasurements | None:
        """Kerncijfers van één site."""
        return next((m for m in await self.get_sites_measurements([site_id]) if m.solar_field_id == site_id), None)

    async def get_site(self, site_id: int) -> Any:
        """``GET sitelist/{id}``."""
        return await self._t.get(f"sitelist/{site_id}")

    async def get_overview_measurements(self) -> Any:
        """``GET sitelist/overviewMeasurements``."""
        return await self._t.get("sitelist/overviewMeasurements")

    async def get_map_view(self, site_filter: dict[str, Any] | None = None) -> Any:
        """``POST sitelist/mapView``."""
        return await self._t.post("sitelist/mapView", site_filter or default_site_filter())

    async def get_geo_bounds(self, site_filter: dict[str, Any] | None = None) -> Any:
        """``POST sitelist/geoBounds``."""
        return await self._t.post("sitelist/geoBounds", site_filter or default_site_filter())


class DashboardClient(_Sub):
    """Dashboard: sitegegevens, live data, energie, vermogen en batterij."""

    def _site(self, site_id: int, suffix: str) -> str:
        return f"dashboard/site-details/{site_id}/{suffix}"

    async def get_site_details(self, site_id: int) -> SiteDetails:
        """``dashboard/site-details/{id}/details``."""
        return parse(SiteDetails, await self._t.get(self._site(site_id, "details")))

    async def get_components(self, site_id: int) -> SiteComponents:
        """``dashboard/site-details/{id}/components``: welke onderdelen de site heeft."""
        return parse(SiteComponents, await self._t.get(self._site(site_id, "components")))

    async def get_equipment(self, site_id: int) -> SiteEquipment:
        """``dashboard/v2/site-details/{id}/equipment``."""
        return parse(SiteEquipment, await self._t.get(f"dashboard/v2/site-details/{site_id}/equipment"))

    async def get_equipment_inverter(self, site_id: int, inverter_serial: str) -> InverterEquipmentDetails:
        """``dashboard/site-details/{id}/equipment-inverter/{serial}``: firmware en communicatie."""
        return parse(InverterEquipmentDetails, await self._t.get(self._site(site_id, f"equipment-inverter/{inverter_serial}")))

    async def get_communication(self, site_id: int) -> SiteCommunication:
        """``dashboard/site-details/{id}/communication``."""
        return parse(SiteCommunication, await self._t.get(self._site(site_id, "communication")))

    async def get_image(self, site_id: int) -> Any:
        """``dashboard/site-details/{id}/image`` (tijdelijke URL)."""
        return await self._t.get(self._site(site_id, "image"))

    async def get_data_availability(self, site_id: int) -> DataAvailability:
        """``dashboard/data-availability/sites/{id}``."""
        return parse(DataAvailability, await self._t.get(f"dashboard/data-availability/sites/{site_id}"))

    async def get_environmental_benefits(self, site_id: int) -> EnvironmentalBenefits:
        """``dashboard/environmental-benefits/sites/{id}``."""
        return parse(EnvironmentalBenefits, await self._t.get(f"dashboard/environmental-benefits/sites/{site_id}"))

    async def get_alerts(self, site_id: int) -> Any:
        """``dashboard/alerts/sites/{id}``."""
        return await self._t.get(f"dashboard/alerts/sites/{site_id}")

    async def get_live_power(self, site_id: int) -> LivePower:
        """``dashboard/live-power/sites/{id}`` (W)."""
        return parse(LivePower, await self._t.get(f"dashboard/live-power/sites/{site_id}"))

    async def get_power_flow(self, site_id: int) -> PowerFlow:
        """``dashboard/power-flow/v2/sites/{id}`` (kW)."""
        return parse(PowerFlow, await self._t.get(f"dashboard/power-flow/v2/sites/{site_id}"))

    async def get_energy(
        self,
        site_id: int,
        start: date,
        end: date,
        chart_time_unit: str = ChartTimeUnit.DAYS,
        measurement_types: Iterable[str] = MeasurementType.DEFAULT_ENERGY,
    ) -> SiteEnergy:
        """``dashboard/energy/sites/{id}`` (Wh)."""
        query = [("chart-time-unit", chart_time_unit), ("start-date", start), ("end-date", end)]
        query += [("measurement-types", m) for m in measurement_types]
        query.append(("isCniViewer", True))
        return parse(SiteEnergy, await self._t.get(f"dashboard/energy/sites/{site_id}", query))

    async def get_power(
        self,
        site_id: int,
        start: date,
        end: date,
        chart_time_unit: str = ChartTimeUnit.QUARTER_HOURS,
        measurement_types: Iterable[str] = (MeasurementType.PRODUCTION, MeasurementType.STORAGE_CHARGE_LEVEL),
    ) -> SitePower:
        """``dashboard/power/sites/{id}`` (W)."""
        query = [("chart-time-unit", chart_time_unit), ("start-date", start), ("end-date", end)]
        query += [("measurement-types", m) for m in measurement_types]
        return parse(SitePower, await self._t.get(f"dashboard/power/sites/{site_id}", query))

    async def get_inverters_power(
        self,
        site_id: int,
        start: date,
        end: date,
        chart_time_unit: str = ChartTimeUnit.QUARTER_HOURS,
        normalized: bool = False,
        page_number: int = 0,
    ) -> InvertersPower:
        """``dashboard/inverters/power/sites/{id}`` (W per omvormer)."""
        query = [
            ("start-date", start),
            ("end-date", end),
            ("normalized", normalized),
            ("chart-time-unit", chart_time_unit),
            ("page-number", page_number),
        ]
        return parse(InvertersPower, await self._t.get(f"dashboard/inverters/power/sites/{site_id}", query))

    async def get_inverters_energy(
        self,
        site_id: int,
        start: date,
        end: date,
        chart_time_unit: str = ChartTimeUnit.DAYS,
        normalized: bool = False,
        page_number: int = 0,
    ) -> InvertersEnergy:
        """``dashboard/inverters/energy/sites/{id}``."""
        query = [
            ("start-date", start),
            ("end-date", end),
            ("chart-time-unit", chart_time_unit),
            ("page-number", page_number),
            ("normalized", normalized),
        ]
        return parse(InvertersEnergy, await self._t.get(f"dashboard/inverters/energy/sites/{site_id}", query))

    async def get_comparative_energy(self, site_id: int, period: str = "yearly") -> Any:
        """``dashboard/comparative-energy/{id}/{monthly|quarterly|yearly}`` (Wh)."""
        return await self._t.get(f"dashboard/comparative-energy/{site_id}/{period}")

    async def get_curtailment(self, site_id: int, start: date, end: date) -> Curtailment:
        """``dashboard/curtailment/sites/{id}``."""
        return parse(
            Curtailment, await self._t.get(f"dashboard/curtailment/sites/{site_id}", [("start-date", start), ("end-date", end)])
        )

    async def get_battery(self, site_id: int, start: date, end: date) -> BatteryChart:
        """``dashboard/battery/sites/{id}``: laadniveau (%) per kwartier."""
        return parse(
            BatteryChart, await self._t.get(f"dashboard/battery/sites/{site_id}", [("start-date", start), ("end-date", end)])
        )

    async def get_storage_energy_distribution(
        self, site_id: int, start: date, end: date, chart_time_unit: str = ChartTimeUnit.DAYS
    ) -> StorageEnergyDistribution:
        """``dashboard/storage/energy/distribution/sites/{id}``."""
        return parse(
            StorageEnergyDistribution,
            await self._t.get(
                f"dashboard/storage/energy/distribution/sites/{site_id}",
                [("start-date", start), ("end-date", end), ("chart-time-unit", chart_time_unit)],
            ),
        )

    async def get_storage_profile(self, site_id: int) -> Any:
        """``dashboard/power-flow/sites/{id}/storage-profile`` (``NotApplicableError`` zonder batterij)."""
        return await self._t.get(f"dashboard/power-flow/sites/{site_id}/storage-profile")

    async def get_house_backup(self, site_id: int) -> Any:
        """``dashboard/site-details/{id}/houseBackup``."""
        return await self._t.get(self._site(site_id, "houseBackup"))

    async def get_yearly_peak_energy_export(self, site_id: int) -> Any:
        """``dashboard/yearly-peak-energy-export/sites/{id}`` (``NotApplicableError`` zonder EMS)."""
        return await self._t.get(f"dashboard/yearly-peak-energy-export/sites/{site_id}")


class WeatherClient(_Sub):
    """Weer en zonnetijden."""

    async def get_weather(self, site_id: int) -> SiteWeather:
        """``dashboard/weather/sites/{id}/weather``."""
        return parse(SiteWeather, await self._t.get(f"dashboard/weather/sites/{site_id}/weather"))

    async def get_sun_times(self, site_id: int, day: date) -> SunTimes:
        """``dashboard/weather/sites/{id}/sun-times?date=…``."""
        return parse(SunTimes, await self._t.get(f"dashboard/weather/sites/{site_id}/sun-times", [("date", day)]))


class SiteAdminClient(_Sub):
    """Site-eigenschappen uit de site-admin-service."""

    async def is_genesis_site(self, site_id: int) -> bool:
        """``site-admin/site/{id}/is-genesis-site``."""
        return bool((await self._t.get(f"site-admin/site/{site_id}/is-genesis-site") or {}).get("isGenesisSite"))

    async def should_present_ems_performance(self, site_id: int) -> bool:
        """``site-admin/energy-manager/site/{id}/shouldPresentEmsPerformance``."""
        return bool(await self._t.get(f"site-admin/energy-manager/site/{site_id}/shouldPresentEmsPerformance"))


class AnalysisClient(_Sub):
    """Grafieken: metrics ontdekken en meetwaarden ophalen."""

    def _analysis(self, site_id: int) -> str:
        return f"cni/ui-api/pages/site/analysis/site/{site_id}"

    def _custom(self, site_id: int) -> str:
        return f"cni/ui-api/pages/site/analysis/custom/site/{site_id}"

    async def get_supported_charts(self, site_id: int) -> list[Any]:
        """``…/supported-charts``."""
        return await self._t.get(f"{self._analysis(site_id)}/supported-charts") or []

    async def get_site_metrics(self, site_id: int) -> SupportedMetrics:
        """``…/metrics/supported`` (siteniveau)."""
        return parse(SupportedMetrics, await self._t.get(f"{self._analysis(site_id)}/metrics/supported"))

    async def get_device_type_metrics(self, site_id: int, device_type: str) -> SupportedMetrics:
        """``…/device-types/{type}/metrics/supported``: alle metrics met eenheid, soort en datatype."""
        return parse(
            SupportedMetrics, await self._t.get(f"{self._analysis(site_id)}/device-types/{device_type}/metrics/supported")
        )

    async def get_devices(self, site_id: int, active_only: bool = True) -> list[AnalysisDevice]:
        """``…/custom/site/{id}/v2/devices``."""
        return (
            parse(AnalysisDevice, await self._t.get(f"{self._custom(site_id)}/v2/devices", [("fetchActiveOnly", active_only)]))
            or []
        )

    async def get_custom_charts(self, site_id: int) -> Any:
        """``…/custom/site/{id}/charts``."""
        return await self._t.get(f"{self._custom(site_id)}/charts")

    async def render_chart(
        self,
        site_id: int,
        chart_uri: str,
        period_scale: str = ChartPeriodScale.DAY,
        interval_index: int = 0,
        inverters: bool = False,
    ) -> Any:
        """``POST …/execute/site/{id}/render-chart`` (Highcharts-configuratie)."""
        population: dict[str, Any] = {"siteId": site_id, "populationType": "site"}
        if inverters:
            population = {"siteId": site_id, "populationType": "deviceList", "deviceType": "INVERTER", "allDevicesInSite": True}
        body = {
            "chartPeriodScale": period_scale,
            "intervalIndex": interval_index,
            "chartUri": chart_uri,
            "chartPopulation": population,
        }
        return await self._t.post(f"cni/ui-api/pages/site/analysis/execute/site/{site_id}/render-chart", body)

    async def generate_chart(
        self, site_id: int, start: datetime, end: datetime, datasources: list[dict[str, Any]]
    ) -> GenerateChartResponse:
        """``POST …/custom/site/{id}/generate-chart``: ruwe meetwaarden.

        Een datasource: ``{"metrics": [...], "datasourcePopulation": {...}, "alignmentGranularity": "FIVE_MINUTES"}``,
        zie :func:`measurement_datasource`.
        """
        body = {"reportPeriod": {"from": utc_timestamp(start), "to": utc_timestamp(end)}, "datasources": datasources}
        return parse(GenerateChartResponse, await self._t.post(f"{self._custom(site_id)}/generate-chart", body))


def measurement_datasource(
    site_id: int, device_type: str, serials: Iterable[str], metrics: Iterable[tuple[str, str]], granularity: str = "FIVE_MINUTES"
) -> dict[str, Any]:
    """Datasource voor generate-chart met ruwe metingen: ``metrics`` = (categorie, uri)-paren."""
    return {
        "metrics": [{"metricType": "measurement", "metricsCategoryName": c, "uri": u} for c, u in metrics],
        "datasourcePopulation": {
            "populationType": "deviceList",
            "siteId": site_id,
            "deviceType": device_type,
            "deviceSerials": list(serials),
        },
        "alignmentGranularity": granularity,
    }


class LayoutClient(_Sub):
    """Lay-out (digitale tweeling) en data per paneel."""

    async def get_logical_layout(self, site_id: int, include_optimizers: bool = True) -> SiteLayout:
        """``layout/logical/generic/v2/site/{id}``."""
        return parse(
            SiteLayout,
            await self._t.get(f"layout/logical/generic/v2/site/{site_id}", [("include-optimizers", include_optimizers)]),
        )

    async def get_physical_layout(self, site_id: int) -> Any:
        """``layout/physical/site/{id}``."""
        return await self._t.get(f"layout/physical/site/{site_id}")

    async def get_information(self, site_id: int) -> LayoutInformation:
        """``layout/information/site/{id}``."""
        return parse(LayoutInformation, await self._t.get(f"layout/information/site/{site_id}"))

    async def get_energy_by_inverter(
        self,
        site_id: int,
        start: date,
        end: date,
        inverter_serials: Iterable[str],
        include_max_temperature: bool = False,
        include_color: bool = True,
    ) -> EnergyByInverter:
        """``layout/energy/site/{id}/by-inverter``: energie per omvormer, string en paneel."""
        query = [
            ("start-date", start),
            ("end-date", end),
            ("inverter-serials", ",".join(inverter_serials)),
            ("include-max-temperature", include_max_temperature),
            ("include-color", include_color),
        ]
        return parse(EnergyByInverter, await self._t.get(f"layout/energy/site/{site_id}/by-inverter", query))

    async def get_energy_graph(self, site_id: int, start: date, end: date, chart_time_unit: str = ChartTimeUnit.HOURS) -> Any:
        """``layout/energy-graph/site/{id}``."""
        return await self._t.get(
            f"layout/energy-graph/site/{site_id}",
            [("chart-time-unit", chart_time_unit), ("start-date", start), ("end-date", end)],
        )

    async def get_optimizers_information(self, optimizer_serials: Iterable[str]) -> OptimizersInformation:
        """``POST layout/information/optimizers``: paneelgegevens en laatste meting per optimizer."""
        return parse(OptimizersInformation, await self._t.post("layout/information/optimizers", list(optimizer_serials)))


class ReportsClient(_Sub):
    """Rapporten (alleen lezen)."""

    async def get_available_reports(self, site_id: int) -> list[AvailableReport]:
        """``report/site/{id}/available-reports``."""
        return parse(AvailableReport, await self._t.get(f"report/site/{site_id}/available-reports")) or []


class AlertsClient(_Sub):
    """Alerts. De tellingen via POST geven 403 voor een eigenaarsaccount (``NotApplicableError``)."""

    async def get_total_count(self, site_id: int) -> int:
        """``GET alerts/site/{id}/alertTotalCount``."""
        return int((await self._t.get(f"alerts/site/{site_id}/alertTotalCount") or {}).get("totalAlertsCount") or 0)

    async def get_filter_types(self, site_id: int) -> Any:
        """``POST alerts/site/{id}/filters``."""
        return await self._t.post(f"alerts/site/{site_id}/filters")

    async def get_alerts(self, site_id: int, statuses: Iterable[str] = ("OPEN",), page_size: int = 50) -> AlertPage:
        """``POST alerts/partialData/site/{id}``."""
        body = {
            "alertStatusApi": list(statuses),
            "impactFilterApi": {"min": 0, "max": 9},
            "siteAlertsPageRequestApi": {
                "alertsInPage": page_size,
                "pageNum": 1,
                "alertSortRequestApi": {"alertsSortColumnTypeApi": "impact", "sortOrderApi": "DESC"},
            },
        }
        return parse(AlertPage, await self._t.post(f"alerts/partialData/site/{site_id}", body))


class SolarEdgeMonitoringClient:
    """Ingang voor de interne API, met één sub-client per backend-service."""

    def __init__(
        self, session: aiohttp.ClientSession, tokens: TokenManager | None = None, bearer_token: str | None = None
    ) -> None:
        """``tokens`` regelt de login; ``bearer_token`` is een vast token (bijv. uit de browser)."""
        self.transport = Transport(session, tokens, bearer_token)
        self.user = UserClient(self.transport)
        self.sites = SitesClient(self.transport)
        self.site_list = SiteListClient(self.transport)
        self.dashboard = DashboardClient(self.transport)
        self.weather = WeatherClient(self.transport)
        self.site_admin = SiteAdminClient(self.transport)
        self.analysis = AnalysisClient(self.transport)
        self.layout = LayoutClient(self.transport)
        self.reports = ReportsClient(self.transport)
        self.alerts = AlertsClient(self.transport)

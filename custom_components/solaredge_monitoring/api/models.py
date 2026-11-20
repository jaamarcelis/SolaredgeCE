"""Datamodellen van de interne API (Python-versie van de .NET-modellen).

Elk model is een dataclass. Velden in snake_case worden automatisch gekoppeld aan camelCase-JSON
(``peak_power`` ↔ ``peakPower``); afwijkende namen staan in ``field(metadata={"key": ...})``.
Onbekende JSON-velden komen in ``extra`` terecht, zodat nieuwe API-velden niets breken.
Alle meetgetallen zijn optioneel: de API geeft ``null`` als er geen data is (bijvoorbeeld 's nachts).
Eenheden staan in de commentaren.
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, field, fields
from datetime import date, datetime
import re
import types
from typing import Any, ClassVar, Self, Union, get_args, get_origin, get_type_hints

_FRACTION = re.compile(r"(\.\d{6})\d+")


def to_camel(name: str) -> str:
    """``peak_power_pkw`` → ``peakPowerPkw``."""
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def parse_datetime(value: str) -> datetime:
    """ISO-8601 met ``Z`` en meer dan 6 decimalen (de API geeft er soms 9)."""
    text = _FRACTION.sub(r"\1", value.replace("Z", "+00:00"))
    return datetime.fromisoformat(text)


def _convert(value: Any, tp: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        return _convert(value, args[0]) if len(args) == 1 else value
    if origin is list:
        (item,) = get_args(tp) or (Any,)
        return [_convert(v, item) for v in value] if isinstance(value, list) else []
    if origin is dict:
        _, val = get_args(tp) or (str, Any)
        return {k: _convert(v, val) for k, v in value.items()} if isinstance(value, dict) else {}
    if isinstance(tp, type) and issubclass(tp, ApiModel):
        return tp.from_dict(value) if isinstance(value, dict) else None
    if tp is float:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if tp is int:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if tp is bool:
        return bool(value) if isinstance(value, bool) else None
    if tp is str:
        return value if isinstance(value, str) else str(value)
    if tp is datetime:
        return parse_datetime(value) if isinstance(value, str) else None
    if tp is date:
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None
    return value


@dataclass(slots=True)
class ApiModel:
    """Basis voor alle modellen."""

    extra: dict[str, Any] = field(default_factory=dict, repr=False, kw_only=True)

    _hints: ClassVar[dict[type, dict[str, Any]]] = {}

    @classmethod
    def json_keys(cls) -> dict[str, str]:
        """JSON-sleutel per veldnaam (zonder ``extra``)."""
        return {f.name: f.metadata.get("key", to_camel(f.name)) for f in fields(cls) if f.name != "extra"}

    @classmethod
    def field_types(cls) -> dict[str, Any]:
        """Type per veldnaam (gecachet)."""
        if cls not in ApiModel._hints:
            ApiModel._hints[cls] = get_type_hints(cls)
        return ApiModel._hints[cls]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Lees een JSON-object in; ontbrekende velden krijgen hun standaardwaarde."""
        hints = cls.field_types()
        keys = cls.json_keys()
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name == "extra":
                continue
            key = keys[f.name]
            if key in data:
                converted = _convert(data[key], hints[f.name])
                if converted is None and f.default is MISSING and f.default_factory is not MISSING:
                    converted = f.default_factory()
                kwargs[f.name] = converted
        known = set(keys.values())
        kwargs["extra"] = {k: v for k, v in data.items() if k not in known}
        return cls(**kwargs)


def _list(cls: type[ApiModel], data: Any) -> list[Any]:
    return [cls.from_dict(item) for item in data or [] if isinstance(item, dict)]


# --- Gebruiker en site -------------------------------------------------------------------------


@dataclass(slots=True)
class UserProfile(ApiModel):
    """``GET user-management/user``."""

    user_id: int | None = None
    account_id: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    locale: str | None = None
    international_system_units: str | None = None


@dataclass(slots=True)
class CniSiteLocation(ApiModel):
    """Locatie van een site."""

    latitude: float | None = None
    longitude: float | None = None
    country_code: str | None = None
    address: str | None = None


@dataclass(slots=True)
class CniSite(ApiModel):
    """``GET cni/ui-api/sites/{id}``."""

    id: int = 0
    name: str | None = None
    kind: str | None = None
    timezone: str | None = None
    location: CniSiteLocation | None = None
    peak_power_pkw: float | None = None  # W (ondanks de naam)
    last_data_input: datetime | None = None


@dataclass(slots=True)
class SiteUserActions(ApiModel):
    """``GET cni/ui-api/sites/{id}/user-actions``."""

    roles: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SiteListItem(ApiModel):
    """Site in de installatielijst."""

    solar_field_id: int = 0
    name: str | None = None
    status: str | None = None
    peak_power: float | None = None  # kWp
    address: str | None = None
    city: str | None = None
    zip: str | None = None
    country_code: str | None = None
    time_zone: str | None = None
    account_name: str | None = None
    installation_date: datetime | None = None
    max_impact: int | None = None
    alerts_count: int | None = None
    battery_count: int = 0
    inverter_count: int = 0
    optimizer_count: int = 0
    meter_count: int = 0
    ev_charger_count: int = 0


@dataclass(slots=True)
class SiteSearchResult(ApiModel):
    """``POST sitelist/searchSites``."""

    total_sites_in_search: int = 0
    page: list[SiteListItem] = field(default_factory=list)


@dataclass(slots=True)
class SiteMeasurements(ApiModel):
    """Kerncijfers uit ``sitesMeasurements``. Energie in kWh, rendement in kWh/kWp."""

    solar_field_id: int = 0
    energy_today: float | None = None
    energy_yesterday: float | None = None
    energy_monthly: float | None = None
    energy_yearly: float | None = None
    energy_life_time: float | None = None
    weekly_yield: float | None = None
    monthly_yield: float | None = None
    yearly_yield: float | None = None
    pr_today: float | None = None
    pr_monthly: float | None = None
    pr_yearly: float | None = None
    last_reporting_time: datetime | None = None


# --- Dashboard ---------------------------------------------------------------------------------


@dataclass(slots=True)
class SiteDetails(ApiModel):
    """``dashboard/site-details/{id}/details``."""

    site_id: int = 0
    site_name: str | None = None
    account_name: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    installation_date: datetime | None = None
    peak_power: float | None = None  # kWp
    status: str | None = None


@dataclass(slots=True)
class SiteComponents(ApiModel):
    """``dashboard/site-details/{id}/components``: welke onderdelen de site heeft."""

    site_type: str | None = None
    has_storage: bool = False
    has_consumption_and_grid: bool = False
    has_performance_ratio: bool = False
    has_smart_devices: bool = False
    has_e_v_chargers: bool = field(default=False, metadata={"key": "hasEVChargers"})
    has_production: bool = True
    has_generator: bool = False
    has_ac_storage: bool = False
    has_dc_storage: bool = False
    inverter_count: int = 0
    can_normalize_inverters_data: bool = False
    international_system_units: str | None = None


@dataclass(slots=True)
class EquipmentModel(ApiModel):
    """Een model met serienummers of aantallen."""

    model_name: str | None = None
    serial_numbers: list[str] = field(default_factory=list)
    total_count: int | None = None


@dataclass(slots=True)
class EquipmentGroup(ApiModel):
    """Apparaten per model."""

    total_count: int = 0
    details: list[EquipmentModel] = field(default_factory=list)


@dataclass(slots=True)
class StorageDevice(ApiModel):
    """Een batterij. ``capacity`` in kWh."""

    manufacturer: str | None = None
    capacity: float | None = None
    serial_number: str | None = None
    part_number: str | None = None
    software_version: str | None = None


@dataclass(slots=True)
class StorageEquipment(ApiModel):
    """Batterijen."""

    total_count: int = 0
    details: list[StorageDevice] = field(default_factory=list)


@dataclass(slots=True)
class SiteEquipment(ApiModel):
    """``dashboard/v2/site-details/{id}/equipment``."""

    inverters: EquipmentGroup | None = None
    optimizers: EquipmentGroup | None = None
    storage: StorageEquipment | None = None
    meters: Any = None
    gateways: Any = None
    ev_chargers: Any = None
    css_od: Any = None  # SolarEdge Home Battery-cluster (energyModuleCluster.totalCount)
    css_ods: Any = None  # meervoudvariant, live gezien (null zonder Home Battery)
    smart_home: Any = None
    buis: Any = None
    meter_collars: Any = None
    commercial_backup_interfaces: Any = None
    local_controllers: Any = None


@dataclass(slots=True)
class InverterEquipmentDetails(ApiModel):
    """``dashboard/site-details/{id}/equipment-inverter/{serial}``."""

    serial_number: str | None = None
    status: str | None = None
    communication: str | None = None  # WIFI / LAN / CELLULAR
    dsp1: str | None = None
    dsp2: str | None = None
    dsp3: str | None = None
    comm_bord: str | None = None
    pid_data: Any = None
    house_backup: str | None = None
    is_ev_charger_active: bool | None = None
    part_number: str | None = None
    power_model: Any = None


@dataclass(slots=True)
class SiteCommunication(ApiModel):
    """``dashboard/site-details/{id}/communication``."""

    connectivity_status: str | None = None
    active: bool = False


@dataclass(slots=True)
class LivePower(ApiModel):
    """``dashboard/live-power/sites/{id}``. W; ``current_ac_power`` is ``None`` zonder productie."""

    current_ac_power: float | None = None
    max_ac_power: float | None = None


@dataclass(slots=True)
class PowerFlowNode(ApiModel):
    """Knooppunt in de energiestroom. ``current_power`` in **kW**."""

    is_active: bool = False
    current_power: float | None = None
    is_producing: bool | None = None
    is_consuming: bool | None = None
    charge_level: float | None = None  # %
    status: str | None = None  # batterij: charging/discharging/idle; net: import/export/grid-outage/idle
    block_count: int | None = None


@dataclass(slots=True)
class PowerFlow(ApiModel):
    """``dashboard/power-flow/v2/sites/{id}``."""

    update_refresh_rate: int | None = None
    is_real_time: bool = False
    is_communicating: bool = False
    solar_production: PowerFlowNode | None = None
    consumption: PowerFlowNode | None = None
    dc_storage: PowerFlowNode | None = None
    ac_storage: PowerFlowNode | None = None
    grid: PowerFlowNode | None = None
    energy_producers: list[Any] = field(default_factory=list)
    last_update_time: datetime | None = None

    @property
    def storage(self) -> PowerFlowNode | None:
        """De batterij zoals de UI hem toont (DC, anders AC)."""
        return self.dc_storage or self.ac_storage


@dataclass(slots=True)
class Distribution(ApiModel):
    """Verdeling van productie of verbruik (Wh)."""

    production_to_home: float | None = None
    production_to_grid: float | None = None
    production_to_battery: float | None = None
    production_to_ev_charger: float | None = None
    production_unknown: float | None = None
    consumption_from_solar: float | None = None
    consumption_from_grid: float | None = None
    consumption_from_battery: float | None = None
    consumption_from_ev_charger: float | None = None
    consumption_unknown: float | None = None
    estimated: bool | None = None


@dataclass(slots=True)
class EnergySummary(ApiModel):
    """Totalen van ``energy/sites/{id}`` (Wh)."""

    production: float | None = None
    production_distribution: Distribution | None = None
    consumption: float | None = None
    consumption_distribution: Distribution | None = None
    performance_ratio: float | None = None
    average_power_factor: float | None = None
    self_consumption_ratio: float | None = None
    self_sufficiency_ratio: float | None = None
    site_availability: float | None = None
    export: float | None = None
    import_: float | None = field(default=None, metadata={"key": "import"})
    yield_: float | None = field(default=None, metadata={"key": "yield"})  # Wh/Wp
    late_production_start_date: Any = None
    late_production_distribution_start_date: Any = None
    late_consumption_start_date: Any = None
    late_consumption_distribution_start_date: Any = None
    late_performance_ratio_start_date: Any = None
    late_yield_start_date: Any = None


@dataclass(slots=True)
class EnergyMeasurement(ApiModel):
    """Energie in één tijdseenheid (Wh)."""

    measurement_time: datetime | None = None
    production: float | None = None
    production_distribution: Distribution | None = None
    consumption: float | None = None
    consumption_distribution: Distribution | None = None
    weather_description: str | None = None
    export: float | None = None
    import_: float | None = field(default=None, metadata={"key": "import"})


@dataclass(slots=True)
class EnergyChart(ApiModel):
    """Grafiekdeel van ``energy``."""

    measurements: list[EnergyMeasurement] = field(default_factory=list)


@dataclass(slots=True)
class SiteEnergy(ApiModel):
    """``dashboard/energy/sites/{id}``."""

    summary: EnergySummary | None = None
    chart: EnergyChart | None = None


@dataclass(slots=True)
class PowerMeasurement(ApiModel):
    """Vermogen op één moment (W)."""

    measurement_time: datetime | None = None
    production: float | None = None
    production_distribution: Distribution | None = None
    consumption: float | None = None
    consumption_distribution: Distribution | None = None
    storage_level: float | None = None  # %
    weather_description: str | None = None
    export: float | None = None
    import_: float | None = field(default=None, metadata={"key": "import"})


@dataclass(slots=True)
class SitePower(ApiModel):
    """``dashboard/power/sites/{id}``."""

    measurements: list[PowerMeasurement] = field(default_factory=list)


@dataclass(slots=True)
class InverterInfo(ApiModel):
    """Naam en serienummer van een omvormer."""

    inverter_name: str | None = None
    inverter_serial: str | None = None
    inverter_order: int = 0


@dataclass(slots=True)
class InvertersPowerPoint(ApiModel):
    """Vermogen (W) van alle omvormers op één moment, in volgorde van ``inverter_data_array``."""

    measurement_time: datetime | None = None
    inverter_power_array: list[float | None] = field(default_factory=list)


@dataclass(slots=True)
class InvertersPower(ApiModel):
    """``dashboard/inverters/power/sites/{id}``."""

    inverters_dated_power_list: list[InvertersPowerPoint] = field(default_factory=list)
    inverter_data_array: list[InverterInfo] = field(default_factory=list)
    external_production_meter_power: Any = None
    power_unit: str | None = None
    dc_power_enabled: bool | None = None


@dataclass(slots=True)
class InverterEnergyItem(ApiModel):
    """Energie van één omvormer (``energy_unit``, meestal Wh)."""

    inverter_name: str | None = None
    inverter_serial: str | None = None
    inverter_order: int = 0
    inverter_energy: float | None = None


@dataclass(slots=True)
class InvertersEnergy(ApiModel):
    """``dashboard/inverters/energy/sites/{id}``."""

    inverter_energy_list: list[InverterEnergyItem] = field(default_factory=list)
    external_production_meter_energy: float | None = None
    energy_unit: str | None = None


@dataclass(slots=True)
class EnvironmentalBenefits(ApiModel):
    """``dashboard/environmental-benefits/sites/{id}``."""

    lifetime_co2_emission_saved: float | None = field(default=None, metadata={"key": "lifetimeCO2EmissionSaved"})  # kg
    lifetime_equivalent_trees_planted: float | None = None
    lifetime_driven_on_sunshine: float | None = None  # km
    international_system_unit: str | None = None


@dataclass(slots=True)
class Curtailment(ApiModel):
    """``dashboard/curtailment/sites/{id}``."""

    power_limited: bool = False
    limit_reasons: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class BatteryMeasurement(ApiModel):
    """Laadniveau (%) op één moment."""

    measurement_time: datetime | None = None
    state_of_charge: float | None = None


@dataclass(slots=True)
class BatteryChart(ApiModel):
    """``dashboard/battery/sites/{id}``."""

    measurements: list[BatteryMeasurement] = field(default_factory=list)


@dataclass(slots=True)
class StorageFlow(ApiModel):
    """Bestemmingen of bronnen van batterij-energie (Wh en %)."""

    total: float | None = None
    building: float | None = None
    building_percentage: float | None = None
    pv: float | None = None
    pv_percentage: float | None = None
    grid: float | None = None
    grid_percentage: float | None = None
    unknown: float | None = None
    unknown_percentage: float | None = None


@dataclass(slots=True)
class StorageEnergyDistribution(ApiModel):
    """``dashboard/storage/energy/distribution/sites/{id}``."""

    destinations: StorageFlow | None = None
    sources: StorageFlow | None = None


@dataclass(slots=True)
class DataAvailability(ApiModel):
    """``dashboard/data-availability/sites/{id}``."""

    production_data_available_from: date | None = None
    consumption_data_available_from: date | None = None
    last_update_time: datetime | None = None
    production_data_available_until: datetime | None = None


# --- Weer -------------------------------------------------------------------------------------


@dataclass(slots=True)
class LiveWeather(ApiModel):
    """Actueel weer (metrisch: °C, km/u)."""

    weather_date: datetime | None = None
    humidity: float | None = None
    current_temp: float | None = None
    wind_speed: float | None = None
    wind_direction: str | None = None
    feels_like: float | None = None
    irradiance: float | None = None
    current_condition: str | None = None
    international_system_unit: str | None = None


@dataclass(slots=True)
class WeatherForecastDay(ApiModel):
    """Verwachting voor één dag."""

    weather_date: date | None = None
    temp_high: float | None = None
    temp_low: float | None = None
    description: str | None = None


@dataclass(slots=True)
class SunTimes(ApiModel):
    """Zonsopkomst en -ondergang."""

    sunrise: datetime | None = None
    sunset: datetime | None = None


@dataclass(slots=True)
class SiteWeather(ApiModel):
    """``dashboard/weather/sites/{id}/weather``."""

    live_weather: LiveWeather | None = None
    weather_forecast: list[WeatherForecastDay] = field(default_factory=list)
    sun_time: SunTimes | None = None
    international_system_unit: str | None = None


# --- Analysis ---------------------------------------------------------------------------------


@dataclass(slots=True)
class MetricMeta(ApiModel):
    """Beschrijving van een metric (eenheid, soort, datatype)."""

    uri: str = ""
    display_name: str | None = None
    display_name_translation_key: str | None = None
    description: str | None = None
    description_translation_key: str | None = None
    data_type: str | None = None  # INTEGER / DECIMAL / STRING
    kind: str | None = None  # GAUGE / DELTA / CUMULATIVE
    units: str | None = None


@dataclass(slots=True)
class MetricCategory(ApiModel):
    """Categorie metingen, berekeningen, live metrics of gebeurtenissen."""

    uri: str = ""
    display_name: str | None = None
    display_name_translation_key: str | None = None
    description: str | None = None
    description_translation_key: str | None = None
    supported_metrics: list[MetricMeta] = field(default_factory=list)
    supported_metric_calculations: list[MetricMeta] = field(default_factory=list)
    supported_period_types: list[str] = field(default_factory=list)
    metrics: list[MetricMeta] = field(default_factory=list)
    supported_event_properties: list[MetricMeta] = field(default_factory=list)


@dataclass(slots=True)
class SupportedMetrics(ApiModel):
    """``analysis/site/{id}[/device-types/{type}]/metrics/supported``."""

    supported_measurements: list[MetricCategory] = field(default_factory=list)
    supported_calculations: list[MetricCategory] = field(default_factory=list)
    supported_live_metrics_categories: list[MetricCategory] = field(default_factory=list)
    supported_device_events: list[MetricCategory] = field(default_factory=list)


@dataclass(slots=True)
class AnalysisDevice(ApiModel):
    """Apparaat voor aangepaste grafieken."""

    device_type: str | None = None
    device_name: str | None = None
    device_serial: str | None = None
    part_of_serial: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    description: str | None = None
    synergy_unit_position: Any = None
    group_type: str | None = None
    identifier: str | None = None
    plugged_to: str | None = None
    status: str | None = None


@dataclass(slots=True)
class ReportObject(ApiModel):
    """Apparaat of site van een dataset."""

    entity_type: str = ""
    entity_id: str = ""


@dataclass(slots=True)
class DatasetMeta(ApiModel):
    """Beschrijving van één dataset in generate-chart."""

    report_object: ReportObject | None = None
    metrics: list[MetricMeta] = field(default_factory=list)
    dataset_name: str = ""
    type: str | None = None

    @property
    def category(self) -> str:
        """Categorie uit ``dataset_name``, bijv. ``measurements`` uit ``INVERTER:measurements``."""
        parts = self.dataset_name.split(":")
        return parts[1] if len(parts) > 1 else self.dataset_name


@dataclass(slots=True)
class ChartMeta(ApiModel):
    """Metadata van generate-chart."""

    datasets_meta: list[DatasetMeta] = field(default_factory=list)


@dataclass(slots=True)
class DataPoint:
    """Eén punt: tijdstip (UTC) en waarden in de volgorde van ``DatasetMeta.metrics``."""

    timestamp: datetime
    values: list[Any]


@dataclass(slots=True)
class GenerateChartResponse(ApiModel):
    """``analysis/custom/site/{id}/generate-chart``. ``data[i]`` hoort bij ``meta.datasets_meta[i]``."""

    range: dict[str, Any] = field(default_factory=dict)
    meta: ChartMeta | None = None
    data: list[Any] = field(default_factory=list)

    def points(self, index: int) -> list[DataPoint]:
        """De punten van dataset ``index``."""
        if index >= len(self.data):
            return []
        return [
            DataPoint(parse_datetime(raw[0]), list(raw[1:]))
            for raw in self.data[index]
            if isinstance(raw, list) and raw and isinstance(raw[0], str)
        ]

    def latest(self, index: int) -> dict[str, tuple[datetime, Any]]:
        """Per metric-uri de laatste niet-null waarde met tijdstip."""
        if not self.meta or index >= len(self.meta.datasets_meta):
            return {}
        metrics = self.meta.datasets_meta[index].metrics
        result: dict[str, tuple[datetime, Any]] = {}
        for point in self.points(index):
            for i, metric in enumerate(metrics):
                if i < len(point.values) and point.values[i] is not None:
                    result[metric.uri] = (point.timestamp, point.values[i])
        return result


# --- Layout -----------------------------------------------------------------------------------


@dataclass(slots=True)
class LayoutNode(ApiModel):
    """Knoop in de logische opstelling: SITE, FOLDER, INVERTER, STRING, OPTIMIZER of een onbekend type."""

    type: str = "UNKNOWN"
    uuid: str | None = None
    name: str = ""
    identifier: str | None = None  # korte ID voor analysis, bijv. 7307A2C0
    serial: str | None = None  # volledig serienummer, bijv. 7307A2C0-DC
    original_serial: str | None = None
    order: int | None = None
    display_order: str | None = None  # positie, bijv. 1.1.3
    is_contain_children: bool = False
    is_alert_on_child: bool = False
    properties: dict[str, Any] = field(default_factory=dict)
    children: list[LayoutNode] = field(default_factory=list)

    def walk(self) -> list[LayoutNode]:
        """Deze knoop en alle onderliggende (diepte-eerst)."""
        result = [self]
        for child in self.children:
            result.extend(child.walk())
        return result


@dataclass(slots=True)
class SiteLayout(ApiModel):
    """``layout/logical/generic/v2/site/{id}``."""

    site_structure: LayoutNode | None = None

    def nodes(self, node_type: str) -> list[LayoutNode]:
        """Alle knopen van één type."""
        return [n for n in (self.site_structure.walk() if self.site_structure else []) if n.type == node_type]

    def parent_of(self, node: LayoutNode, parent_type: str) -> LayoutNode | None:
        """De dichtstbijzijnde voorouder van ``parent_type``."""

        def search(current: LayoutNode, ancestors: list[LayoutNode]) -> LayoutNode | None:
            if current is node:
                return next((a for a in reversed(ancestors) if a.type == parent_type), None)
            for child in current.children:
                if (found := search(child, [*ancestors, current])) is not None:
                    return found
            return None

        return search(self.site_structure, []) if self.site_structure else None


@dataclass(slots=True)
class EnergyValue(ApiModel):
    """Waarde met eenheid (meestal ``watt-hour``)."""

    value: float | None = None
    unit: str | None = None


@dataclass(slots=True)
class OptimizerTemperature(ApiModel):
    """Temperatuur van een optimizer."""

    temperature: float | None = None
    temperature_unit: str | None = None


@dataclass(slots=True)
class OptimizerLayoutEnergy(ApiModel):
    """Energie van één paneel. ``color`` = relatieve opbrengst (0–1)."""

    serial: str | None = None
    energy: EnergyValue | None = None
    temperature: OptimizerTemperature | None = None
    color: float | None = None


@dataclass(slots=True)
class StringLayoutEnergy(ApiModel):
    """Energie van een string."""

    energy: EnergyValue | None = None
    string_relative_order: int = 0


@dataclass(slots=True)
class InverterLayoutEnergy(ApiModel):
    """Energie van een omvormer met strings en panelen."""

    serial: str | None = None
    energy: EnergyValue | None = None
    strings: list[StringLayoutEnergy] = field(default_factory=list)
    optimizers: list[OptimizerLayoutEnergy] = field(default_factory=list)


@dataclass(slots=True)
class EnergyByInverter(ApiModel):
    """``layout/energy/site/{id}/by-inverter``."""

    site_id: int = 0
    start_date: date | None = None
    end_date: date | None = None
    inverters: list[InverterLayoutEnergy] = field(default_factory=list)


@dataclass(slots=True)
class PanelModule(ApiModel):
    """Zonnepaneel. ``tilt`` en ``azimuth`` in radialen, ``stc_power`` in W."""

    orientation: str | None = None
    tilt: float | None = None
    azimuth: float | None = None
    manufacturer: str | None = None
    model: str | None = None
    stc_power: float | None = None


@dataclass(slots=True)
class OptimizerBasicInformation(ApiModel):
    """Statische informatie van een optimizer."""

    type_name: str | None = field(default=None, metadata={"key": "type_"})
    serial: str | None = None
    model: str | None = None
    bundle_type: str | None = None
    modules: list[PanelModule] = field(default_factory=list)
    multi_optimizers: bool = False


@dataclass(slots=True)
class OptimizerLiveData(ApiModel):
    """Laatste meting van een optimizer."""

    type_name: str | None = field(default=None, metadata={"key": "type_"})
    last_measurement: datetime | None = None
    current: float | None = field(default=None, metadata={"key": "current_A"})  # A
    optimizer_voltage: float | None = field(default=None, metadata={"key": "optimizerVoltage_V"})  # V (uitgang)
    power: float | None = field(default=None, metadata={"key": "power_W"})  # W
    voltage: float | None = field(default=None, metadata={"key": "voltage_V"})  # V (paneel)
    energy: float | None = field(default=None, metadata={"key": "total_last_telemetry_energy_WH"})  # Wh


@dataclass(slots=True)
class OptimizersInformation(ApiModel):
    """``POST layout/information/optimizers``."""

    basic_information_list: list[OptimizerBasicInformation] = field(default_factory=list)
    serial_to_live_data: dict[str, OptimizerLiveData] = field(default_factory=dict)


@dataclass(slots=True)
class LayoutInformation(ApiModel):
    """``layout/information/site/{id}``."""

    peak_power: float | None = None
    installation_date: date | None = None
    address: str | None = None
    site_time: datetime | None = None
    panel_clean_date: date | None = None
    big_image_download_link: str | None = None
    site_time_zone: str | None = None
    life_time_date: date | None = None
    latitude: float | None = None
    longitude: float | None = None


# --- Alerts en rapporten ------------------------------------------------------------------------


@dataclass(slots=True)
class SiteAlert(ApiModel):
    """Eén alert (velden afgeleid uit de webapp-code)."""

    alert_id: Any = None
    category: str | None = None
    component_type: str | None = None
    serial_number: str | None = None
    alert_type: str | None = None
    impact: int | None = None
    creation_date: datetime | None = None
    last_trigger_date: datetime | None = None
    close_date: datetime | None = None
    status: str | None = None


@dataclass(slots=True)
class AlertPage(ApiModel):
    """``POST alerts/partialData/site/{id}``."""

    alerts: list[SiteAlert] = field(default_factory=list)
    number_of_all_returned_alerts: int = 0
    number_of_all_existing_alerts: int = 0
    is_exceeds_alerts_limit: bool = False


@dataclass(slots=True)
class AvailableReport(ApiModel):
    """Beschikbaar rapport."""

    report_type: str = ""
    date_range: dict[str, Any] | None = None


def parse(model: type[ApiModel], data: Any) -> Any:
    """Lees ``data`` in als ``model`` (of lijst daarvan); ``None`` blijft ``None``."""
    if data is None:
        return None
    if isinstance(data, list):
        return _list(model, data)
    return model.from_dict(data) if isinstance(data, dict) else None

"""Constanten van de SolarEdge Monitoring-integratie."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "solaredge_monitoring"
MANUFACTURER: Final = "SolarEdge"

CONF_SITE_ID: Final = "site_id"
CONF_SITE_NAME: Final = "site_name"
CONF_TOKENS: Final = "tokens"

# Opties
CONF_LIVE_INTERVAL: Final = "live_interval"  # seconden
CONF_ENERGY_INTERVAL: Final = "energy_interval"  # minuten
CONF_TELEMETRY_INTERVAL: Final = "telemetry_interval"  # minuten
CONF_PANELS_INTERVAL: Final = "panels_interval"  # minuten
CONF_INFO_INTERVAL: Final = "info_interval"  # minuten
CONF_INCLUDE_OPTIMIZERS: Final = "include_optimizers"
CONF_TELEMETRY_METRICS: Final = "telemetry_metrics"  # lijst "categorie.uri"; leeg = alle ontdekte
CONF_HISTORY_DAYS: Final = "history_days"  # knop Historie importeren: aantal dagen tot en met vandaag
CONF_HISTORY_INTERVAL: Final = "history_interval"  # knop Historie importeren: interval van de rijen

HISTORY_INTERVAL_FIVE_MINUTES: Final = "five_minutes"
HISTORY_INTERVAL_HOUR: Final = "hour"
HISTORY_INTERVAL_DAY: Final = "day"
HISTORY_INTERVALS: Final = (HISTORY_INTERVAL_FIVE_MINUTES, HISTORY_INTERVAL_HOUR, HISTORY_INTERVAL_DAY)
DEFAULT_HISTORY_DAYS: Final = 14
MAX_HISTORY_DAYS: Final = 3650

DEFAULT_LIVE_INTERVAL: Final = 60
DEFAULT_ENERGY_INTERVAL: Final = 5
DEFAULT_TELEMETRY_INTERVAL: Final = 15
DEFAULT_PANELS_INTERVAL: Final = 15
DEFAULT_INFO_INTERVAL: Final = 30
STATIC_INTERVAL: Final = timedelta(hours=24)

# Tijdvenster voor telemetrie: laatste niet-null punt uit de afgelopen periode
TELEMETRY_WINDOW: Final = timedelta(hours=2)

# Omvormermetrics die standaard ingeschakeld zijn; alle andere ontdekte metrics zijn standaard uitgeschakeld
CORE_METRICS: Final = frozenset(
    {
        "measurements.active_power",
        "measurements.ac_voltage",
        "measurements.ac_current",
        "measurements.ac_frequency",
        "measurements.dc_voltage",
        "measurements.temperature",
        "measurements.power_factor",
        "measurements.lifetime_positive_ac_energy",
        "pv_measurements.power",
    }
)

# Meer optimizers per request zou de URL/body onnodig groot maken
OPTIMIZER_BATCH_SIZE: Final = 50

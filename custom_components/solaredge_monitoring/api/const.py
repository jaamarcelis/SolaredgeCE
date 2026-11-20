"""Constanten van de interne SolarEdge Monitoring API."""

from __future__ import annotations

BASE_URL = "https://monitoring.solaredge.com/services/"
ORIGIN = "https://monitoring.solaredge.com"
PORTAL_SITE_URL = "https://monitoring.solaredge.com/one#/residential/dashboard?siteId={site_id}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"

# AWS Cognito van de webclient van monitoring.solaredge.com
COGNITO_REGION = "eu-central-1"
COGNITO_USER_POOL_ID = "eu-central-1_fVUTz39em"
COGNITO_CLIENT_ID = "ugfnsujd3384sshcjehaphlh3"


class ChartTimeUnit:
    """Waarden voor ``chart-time-unit``."""

    QUARTER_HOURS = "quarter-hours"
    HOURS = "hours"
    DAYS = "days"
    WEEKS = "weeks"
    MONTHS = "months"
    YEARS = "years"


class MeasurementType:
    """Waarden voor de herhaalbare parameter ``measurement-types``."""

    PRODUCTION = "production"
    YIELD = "yield"
    AVERAGE_POWER_FACTOR = "average-power-factor"
    PERFORMANCE_RATIO = "performance-ratio"
    SITE_AVAILABILITY = "site-availability"
    CONSUMPTION = "consumption"
    EXPORT = "export"
    IMPORT = "import"
    PRODUCTION_DISTRIBUTION_WITHOUT_STORAGE = "production-distribution-without-storage"
    CONSUMPTION_DISTRIBUTION_WITHOUT_STORAGE = "consumption-distribution-without-storage"
    PRODUCTION_DISTRIBUTION_WITH_STORAGE = "production-distribution-with-storage"
    CONSUMPTION_DISTRIBUTION_WITH_STORAGE = "consumption-distribution-with-storage"
    STORAGE_CHARGE_LEVEL = "storage-charge-level"

    DEFAULT_ENERGY = (PRODUCTION, YIELD, AVERAGE_POWER_FACTOR, PERFORMANCE_RATIO, SITE_AVAILABILITY)

    @classmethod
    def for_site(cls, has_meter: bool, has_storage: bool) -> list[str]:
        """De meetsoorten die de webapp kiest voor een site (zoals ``On0`` in de webapp-code)."""
        types = [cls.PRODUCTION]
        if has_meter:
            types.append(cls.CONSUMPTION)
            if has_storage:
                types += [
                    cls.PRODUCTION_DISTRIBUTION_WITH_STORAGE,
                    cls.CONSUMPTION_DISTRIBUTION_WITH_STORAGE,
                    cls.IMPORT,
                    cls.EXPORT,
                ]
            else:
                types += [
                    cls.PRODUCTION_DISTRIBUTION_WITHOUT_STORAGE,
                    cls.CONSUMPTION_DISTRIBUTION_WITHOUT_STORAGE,
                ]
        return types


class ChartPeriodScale:
    """Waarden voor ``chartPeriodScale`` van render-chart."""

    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    YEAR = "YEAR"


class DeviceType:
    """Apparaattypen van de analysis-endpoints."""

    ONE_PHASE_INVERTER = "ONE_PHASE_INVERTER"
    THREE_PHASE_INVERTER = "THREE_PHASE_INVERTER"
    INVERTER = "INVERTER"
    STRING = "STRING"
    OPTIMIZER = "OPTIMIZER"
    MULTI_OPTIMIZER = "MULTI_OPTIMIZER"


class Granularity:
    """Waarden voor ``alignmentGranularity`` en ``periodType``."""

    FIVE_MINUTES = "FIVE_MINUTES"
    QUARTER_HOUR = "QUARTER_HOUR"
    HOUR = "HOUR"
    DAY = "DAY"


SITES_MEASUREMENT_TYPES = (
    "energyToday",
    "energyYesterday",
    "energyMonthly",
    "energyYearly",
    "weeklyYield",
    "monthlyYield",
    "yearlyYield",
    "prToday",
    "prMonthly",
    "prLastMonth",
    "prYearly",
    "prLastYear",
    "lastReportingTime",
    "energyLifeTime",
)

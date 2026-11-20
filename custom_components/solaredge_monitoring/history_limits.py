"""Limieten van generate-chart per apparaattype, metric en resolutie: bekend uit metingen, aangevuld tijdens gebruik.

Gemeten (zie docs/historie-limieten.md):

- Fijne resolutie (5 min, 15 min, uur): hoogstens **31 dagen** per request. Daarboven geeft SolarEdge zonder
  foutmelding dagwaarden terug. Omvormers: alleen de laatste **730 dagen**.
- Dagresolutie: momentwaarden van omvormers (vermogen, spanning, stroom, frequentie) hoogstens 32 dagen per request;
  energie, tellers en temperatuur zonder grens. Geen leeftijdsgrens.
- Arbeidsfactor en blindvermogen: 7 dagen per request, bij elke resolutie.
- Optimizers: hoogstens 14 dagen per request; in de grafiekweergave alleen de laatste **90 dagen** (91–365 dagen alleen
  via de exporttool, die een CSV mailt; ouder dan 365 dagen niet).

Nieuwe grenzen worden uit de foutmeldingen gelezen (``learn``) en per config entry bewaard in ``.storage``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
FINE = "fine"  # 5 minuten, kwartier, uur
DAY = "day"

DEFAULT_RANGE = {FINE: 31, DAY: 366}

# Momentwaarden van omvormers met een dagresolutie van hoogstens 32 dagen per request
INVERTER_GAUGES_32 = {
    "pv_measurements.power",
    "measurements.ac_voltage",
    "measurements.ac_current",
    "measurements.ac_frequency",
    "measurements.dc_voltage",
    "measurements.ac_current_dc_component",
    "measurements.active_power",
    "measurements.apparent_power",
    "measurements.negative_active_power",
}
INVERTER_GAUGES_7 = {"measurements.power_factor", "measurements.reactive_power"}


def granularity_class(granularity: str) -> str:
    """``fine`` voor 5 minuten, kwartier en uur; anders ``day``."""
    return DAY if granularity in ("DAY", "WEEK", "MONTH", "YEAR") else FINE


@dataclass(slots=True)
class Limit:
    """Grenzen van één (apparaattype, metric, resolutieklasse)."""

    max_range_days: float | None = None  # periode per request
    max_age_days: float | None = None  # hoe ver terug vanaf nu
    learned: bool = False  # uit een foutmelding gelezen (anders bekend uit metingen)
    message: str | None = None  # de foutmelding waaruit hij geleerd is


def known_limit(device_type: str, metric: str, cls: str) -> Limit:
    """Bekende grenzen uit de metingen."""
    limit = Limit(max_range_days=DEFAULT_RANGE[cls])
    if device_type in ("OPTIMIZER", "MULTI_OPTIMIZER"):
        limit.max_range_days = 14 if cls == FINE else limit.max_range_days
        limit.max_age_days = 90
    elif "INVERTER" in device_type:
        if cls == FINE:
            limit.max_age_days = 730
        if metric in INVERTER_GAUGES_7:
            limit.max_range_days = 7
        elif metric in INVERTER_GAUGES_32 and cls == DAY:
            limit.max_range_days = 32
    return limit


_RANGE = re.compile(r"limited to (\d+) days")
_AGE = re.compile(r"only available for the last (\d+) days")
_CHART_AGE = re.compile(r"older than (\d+) days is not available")


class HistoryLimits:
    """Bekende en geleerde grenzen; geleerde worden bewaard."""

    def __init__(self, hass: HomeAssistant | None, entry_id: str | None) -> None:
        """Zonder ``hass`` (tests) alleen in het geheugen."""
        self._store: Store[dict[str, Any]] | None = (
            Store(hass, STORAGE_VERSION, f"{DOMAIN}.history_limits.{entry_id}") if hass and entry_id else None
        )
        self._learned: dict[str, Limit] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Laad de geleerde grenzen (één keer)."""
        if self._loaded or self._store is None:
            self._loaded = True
            return
        data = await self._store.async_load() or {}
        self._learned = {key: Limit(**value) for key, value in data.get("limits", {}).items()}
        self._loaded = True

    async def async_save(self) -> None:
        """Bewaar de geleerde grenzen."""
        if self._store is not None:
            await self._store.async_save({"limits": {key: asdict(value) for key, value in self._learned.items()}})

    @staticmethod
    def _key(device_type: str, metric: str, cls: str) -> str:
        return f"{device_type}|{metric}|{cls}"

    def get(self, device_type: str, metric: str, granularity: str) -> Limit:
        """De grenzen: geleerd waar bekend, anders uit de metingen."""
        cls = granularity_class(granularity)
        known = known_limit(device_type, metric, cls)
        learned = self._learned.get(self._key(device_type, metric, cls))
        if learned is None:
            return known
        return Limit(
            max_range_days=_min(learned.max_range_days, known.max_range_days),
            max_age_days=_min(learned.max_age_days, known.max_age_days),
            learned=True,
            message=learned.message,
        )

    def learn(self, device_type: str, metric: str, granularity: str, message: str) -> bool:
        """Lees een grens uit een foutmelding; ``True`` als er iets nieuws geleerd is."""
        requested = granularity_class(granularity)
        fine_only = "hourly or finer" in message
        classes = [FINE] if fine_only else ([requested] if _RANGE.search(message) else [FINE, DAY])
        changed = False
        for cls in classes:
            key = self._key(device_type, metric, cls)
            current = self._learned.get(key) or Limit()
            if match := _RANGE.search(message):
                value = float(match.group(1))
                if current.max_range_days is None or value < current.max_range_days:
                    current.max_range_days, changed = value, True
            for pattern in (_AGE, _CHART_AGE):
                if match := pattern.search(message):
                    value = float(match.group(1))
                    if current.max_age_days is None or value < current.max_age_days:
                        current.max_age_days, changed = value, True
            current.learned, current.message = True, message[:300]
            self._learned[key] = current
        if changed:
            _LOGGER.debug("Grens geleerd voor %s %s %s: %s", device_type, metric, classes, message)
        return changed

    def learn_range(self, device_type: str, metric: str, granularity: str, days: float, reason: str) -> None:
        """Leg een kleinere periode per request vast (bijv. bij stille terugval naar dagwaarden)."""
        cls = granularity_class(granularity)
        key = self._key(device_type, metric, cls)
        current = self._learned.get(key) or Limit()
        if current.max_range_days is None or days < current.max_range_days:
            current.max_range_days = days
        current.learned, current.message = True, reason
        self._learned[key] = current

    def as_dict(self) -> dict[str, Any]:
        """Voor diagnostics."""
        return {key: asdict(value) for key, value in self._learned.items()}


def _min(a: float | None, b: float | None) -> float | None:
    values = [v for v in (a, b) if v is not None]
    return min(values) if values else None

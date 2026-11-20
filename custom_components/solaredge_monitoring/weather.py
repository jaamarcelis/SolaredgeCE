"""Weer-entity met het actuele weer en de dagverwachting van SolarEdge (op de locatie van de site)."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from homeassistant.components.weather import (
    ATTR_CONDITION_CLOUDY,
    ATTR_CONDITION_FOG,
    ATTR_CONDITION_LIGHTNING_RAINY,
    ATTR_CONDITION_PARTLYCLOUDY,
    ATTR_CONDITION_POURING,
    ATTR_CONDITION_RAINY,
    ATTR_CONDITION_SNOWY,
    ATTR_CONDITION_SNOWY_RAINY,
    ATTR_CONDITION_SUNNY,
    ATTR_CONDITION_WINDY,
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SolarEdgeConfigEntry
from .api.models import SiteWeather
from .coordinator import InfoCoordinator
from .entity import SolarEdgeEntity, site_device

# Volgorde is belangrijk: de eerste treffer in de omschrijving wint
_CONDITIONS = (
    ("thunder", ATTR_CONDITION_LIGHTNING_RAINY),
    ("sleet", ATTR_CONDITION_SNOWY_RAINY),
    ("snow", ATTR_CONDITION_SNOWY),
    ("heavy rain", ATTR_CONDITION_POURING),
    ("rain", ATTR_CONDITION_RAINY),
    ("shower", ATTR_CONDITION_RAINY),
    ("drizzle", ATTR_CONDITION_RAINY),
    ("fog", ATTR_CONDITION_FOG),
    ("mist", ATTR_CONDITION_FOG),
    ("wind", ATTR_CONDITION_WINDY),
    ("partly", ATTR_CONDITION_PARTLYCLOUDY),
    ("mostly cloudy", ATTR_CONDITION_CLOUDY),
    ("overcast", ATTR_CONDITION_CLOUDY),
    ("cloud", ATTR_CONDITION_PARTLYCLOUDY),
    ("clear", ATTR_CONDITION_SUNNY),
    ("sun", ATTR_CONDITION_SUNNY),
    ("fair", ATTR_CONDITION_SUNNY),
)
_BEARINGS = {
    d: i * 22.5
    for i, d in enumerate(["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"])
}


def map_condition(description: str | None) -> str | None:
    """Engelse weersomschrijving van SolarEdge naar een HA-conditie."""
    if not description:
        return None
    text = description.lower()
    return next((condition for word, condition in _CONDITIONS if word in text), None)


async def async_setup_entry(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Eén weer-entity per site."""
    runtime = entry.runtime_data
    async_add_entities([SolarEdgeWeather(runtime.info, runtime.model.timezone)])


class SolarEdgeWeather(SolarEdgeEntity, WeatherEntity):
    """Actueel weer en dagverwachting."""

    _attr_translation_key = "weather"
    _attr_name = None  # de naam van het site-device
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_supported_features = WeatherEntityFeature.FORECAST_DAILY

    def __init__(self, coordinator: InfoCoordinator, timezone: str) -> None:
        """Koppel aan het site-device."""
        super().__init__(coordinator, site_device(coordinator.model), f"{coordinator.model.site_id}_site_weather")
        self._tz = ZoneInfo(timezone)

    @property
    def _weather(self) -> SiteWeather | None:
        return (self.coordinator.data or {}).get("weather")

    @property
    def condition(self) -> str | None:
        """Huidige conditie."""
        weather = self._weather
        return map_condition(weather.live_weather.current_condition) if weather and weather.live_weather else None

    @property
    def native_temperature(self) -> float | None:
        """Temperatuur (°C)."""
        return self._weather.live_weather.current_temp if self._weather and self._weather.live_weather else None

    @property
    def native_apparent_temperature(self) -> float | None:
        """Gevoelstemperatuur (°C)."""
        return self._weather.live_weather.feels_like if self._weather and self._weather.live_weather else None

    @property
    def humidity(self) -> float | None:
        """Luchtvochtigheid (%)."""
        return self._weather.live_weather.humidity if self._weather and self._weather.live_weather else None

    @property
    def native_wind_speed(self) -> float | None:
        """Windsnelheid (km/u)."""
        return self._weather.live_weather.wind_speed if self._weather and self._weather.live_weather else None

    @property
    def wind_bearing(self) -> float | str | None:
        """Windrichting in graden (of de tekst als die onbekend is)."""
        direction = self._weather.live_weather.wind_direction if self._weather and self._weather.live_weather else None
        return _BEARINGS.get(direction or "", direction)

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self.hass.async_create_task(self.async_update_listeners(("daily",)))

    async def async_forecast_daily(self) -> list[Forecast] | None:
        """Dagverwachting."""
        if not self._weather:
            return None
        return [
            Forecast(
                datetime=datetime.combine(day.weather_date, time(12), self._tz).isoformat(),
                condition=map_condition(day.description),
                native_temperature=day.temp_high,
                native_templow=day.temp_low,
            )
            for day in self._weather.weather_forecast
            if day.weather_date
        ]

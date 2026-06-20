"""Open-Meteo weather source (extensibility example).

Open-Meteo is free and key-less. This adapter shows how to add a second source;
the registry can later be configured to ensemble multiple sources. Network
calls are only made in live mode.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import requests

from ..utils.logging import get_logger
from .base import Forecast, WeatherSource
from .stations import Station

logger = get_logger("weather.open_meteo")


class OpenMeteoSource(WeatherSource):
    """Daily forecasts from the Open-Meteo API."""

    name = "open_meteo"
    BASE = "https://api.open-meteo.com/v1/forecast"
    ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

    def __init__(self) -> None:
        self.session = requests.Session()

    def _daily(self, url: str, station: Station, target_date: date) -> Optional[dict]:
        params = {
            "latitude": station.latitude,
            "longitude": station.longitude,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
            "snowfall_sum,wind_speed_10m_max",
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
        }
        try:
            resp = self.session.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json().get("daily")
        except requests.RequestException as exc:
            logger.warning("Open-Meteo request failed for %s: %s", station.id, exc)
            return None

    def _build(self, daily: dict, station: Station, target_date: date, horizon: int) -> Forecast:
        def first(key):
            vals = daily.get(key) or []
            return float(vals[0]) if vals else None

        return Forecast(
            location_id=station.id,
            target_date=target_date,
            source=self.name,
            horizon_days=horizon,
            high_temp_f=first("temperature_2m_max"),
            low_temp_f=first("temperature_2m_min"),
            precip_in=first("precipitation_sum"),
            snow_in=first("snowfall_sum"),
            wind_mph=first("wind_speed_10m_max"),
        )

    def get_forecast(self, station: Station, target_date: date) -> Optional[Forecast]:
        from datetime import datetime, timezone

        daily = self._daily(self.BASE, station, target_date)
        if not daily:
            return None
        horizon = max((target_date - datetime.now(timezone.utc).date()).days, 0)
        return self._build(daily, station, target_date, horizon)

    def get_observation(self, station: Station, target_date: date) -> Optional[Forecast]:
        daily = self._daily(self.ARCHIVE, station, target_date)
        if not daily:
            return None
        return self._build(daily, station, target_date, 0)

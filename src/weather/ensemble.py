"""Multi-model ensemble forecast source + composite weather source.

:class:`EnsembleForecastSource` queries several independent NWP models
(GFS, ECMWF, ICON, GEM, MeteoFrance) via Open-Meteo and returns the consensus
mean plus the cross-model **spread** — a per-day uncertainty signal validated
out-of-sample to improve calibration (scripts/ensemble_validation.py).

:class:`CompositeWeatherSource` routes forecasts to the ensemble source while
keeping NWS for intraday observations and official settlement values (the
settlement-critical station data). This also aligns the live forecast source
with what the model was calibrated on.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timezone
from typing import Optional

import requests

from ..utils.logging import get_logger
from .base import Forecast, WeatherSource
from .stations import Station

logger = get_logger("weather.ensemble")

MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_seamless",
          "meteofrance_seamless"]


class EnsembleForecastSource(WeatherSource):
    """Consensus + cross-model spread from several NWP models."""

    name = "ensemble"
    BASE = "https://api.open-meteo.com/v1/forecast"

    def __init__(self) -> None:
        self.session = requests.Session()

    def _fetch(self, station: Station, target_date: date) -> Optional[Forecast]:
        params = {
            "latitude": station.latitude, "longitude": station.longitude,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
            "timezone": station.tz, "models": ",".join(MODELS),
            "start_date": target_date.isoformat(), "end_date": target_date.isoformat(),
        }
        try:
            resp = self.session.get(self.BASE, params=params, timeout=30)
            resp.raise_for_status()
            daily = resp.json().get("daily", {})
        except requests.RequestException as exc:
            logger.warning("Ensemble forecast failed for %s: %s", station.id, exc)
            return None

        def members(field: str) -> list[float]:
            out = []
            for m in MODELS:
                col = daily.get(f"{field}_{m}")
                if col and col[0] is not None:
                    out.append(float(col[0]))
            return out

        highs = members("temperature_2m_max")
        lows = members("temperature_2m_min")
        precip = members("precipitation_sum")
        if not highs:
            return None

        horizon = max((target_date - datetime.now(timezone.utc).date()).days, 0)
        return Forecast(
            location_id=station.id, target_date=target_date, source=self.name,
            horizon_days=horizon,
            high_temp_f=round(statistics.mean(highs), 2),
            low_temp_f=round(statistics.mean(lows), 2) if lows else None,
            precip_in=round(statistics.mean(precip), 3) if precip else None,
            spread_f=round(statistics.pstdev(highs), 2) if len(highs) >= 3 else None,
        )

    def get_forecast(self, station: Station, target_date: date) -> Optional[Forecast]:
        return self._fetch(station, target_date)

    def get_observation(self, station: Station, target_date: date) -> Optional[Forecast]:
        # Settlement/observation should come from the official station (NWS),
        # not a model — handled by CompositeWeatherSource.
        return None


class CompositeWeatherSource(WeatherSource):
    """Forecasts from one source, observations/intraday from another."""

    name = "composite"

    def __init__(self, forecast_source: WeatherSource, observation_source: WeatherSource) -> None:
        self.forecast_source = forecast_source
        self.observation_source = observation_source

    def get_forecast(self, station: Station, target_date: date) -> Optional[Forecast]:
        fc = self.forecast_source.get_forecast(station, target_date)
        if fc is None:
            # Fall back to the observation source's forecast if the ensemble fails.
            fc = self.observation_source.get_forecast(station, target_date)
        return fc

    def get_observation(self, station: Station, target_date: date) -> Optional[Forecast]:
        return self.observation_source.get_observation(station, target_date)

    def get_intraday(self, station: Station) -> Optional[Forecast]:
        return self.observation_source.get_intraday(station)

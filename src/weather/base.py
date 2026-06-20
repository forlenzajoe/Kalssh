"""Abstract weather-source interface and the :class:`Forecast` model.

New providers (Open-Meteo, Meteostat, commercial APIs) only need to subclass
:class:`WeatherSource` and register themselves in :mod:`src.weather.registry`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .stations import Station


@dataclass
class Forecast:
    """A point forecast for one location and target date.

    All fields are optional because providers differ in coverage. Units are
    imperial to match Kalshi weather contracts (°F, inches, mph).
    """

    location_id: str
    target_date: date
    source: str
    horizon_days: int
    high_temp_f: Optional[float] = None
    low_temp_f: Optional[float] = None
    precip_in: Optional[float] = None
    snow_in: Optional[float] = None
    wind_mph: Optional[float] = None
    precip_prob: Optional[float] = None     # 0..1 if provided by source
    # Cross-model forecast spread (°F) — std of independent NWP models for the
    # day. Drives the adaptive per-day uncertainty (ensemble sigma).
    spread_f: Optional[float] = None
    # Intraday conditioning fields (populated for same-day markets when live
    # observations are available). These power the edge in IntradayTemperatureModel.
    observed_high_so_far: Optional[float] = None   # max temp observed today (°F)
    observed_low_so_far: Optional[float] = None     # min temp observed today (°F)
    local_hour: Optional[float] = None              # local solar hour 0..24
    raw: dict = field(default_factory=dict)

    def value_for(self, variable: str) -> Optional[float]:
        """Return the forecast point value for a model variable name."""
        return {
            "high_temp": self.high_temp_f,
            "low_temp": self.low_temp_f,
            "rainfall": self.precip_in,
            "snowfall": self.snow_in,
            "wind": self.wind_mph,
        }.get(variable)


class WeatherSource(abc.ABC):
    """Base class for all weather providers."""

    name: str = "base"

    @abc.abstractmethod
    def get_forecast(self, station: Station, target_date: date) -> Optional[Forecast]:
        """Return a forecast for ``station`` on ``target_date`` (or None)."""

    @abc.abstractmethod
    def get_observation(self, station: Station, target_date: date) -> Optional[Forecast]:
        """Return *observed* conditions for a past/current date (for backtests)."""

    def get_intraday(self, station: Station) -> Optional[Forecast]:
        """Return today's observations-so-far (max/min temp + local hour).

        Powers the intraday edge model. Default returns ``None`` (no intraday
        data); live sources override this. Optional, so existing sources need
        not implement it.
        """
        return None

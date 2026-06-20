"""NOAA / NWS (api.weather.gov) weather source.

The NWS API is free and key-less but requires a descriptive User-Agent. This
adapter pulls gridpoint forecasts (highs/lows/precip/wind) and, for backtests,
daily observations. In mock mode the registry returns :class:`MockWeatherSource`
instead, so this class is only exercised with live network access.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import requests

from ..utils.logging import get_logger
from .base import Forecast, WeatherSource
from .stations import Station

logger = get_logger("weather.noaa")

_C_TO_F = lambda c: c * 9.0 / 5.0 + 32.0  # noqa: E731
_MM_TO_IN = lambda mm: mm / 25.4          # noqa: E731
_KMH_TO_MPH = lambda k: k * 0.621371      # noqa: E731


class NWSWeatherSource(WeatherSource):
    """Forecasts and observations from api.weather.gov."""

    name = "nws"
    BASE = "https://api.weather.gov"

    def __init__(self, user_agent: str = "kalshi-weather-scanner") -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/geo+json"})
        self._grid_cache: dict[str, dict] = {}

    def _gridpoint(self, station: Station) -> Optional[dict]:
        """Resolve a lat/lon to an NWS gridpoint forecast endpoint."""
        if station.id in self._grid_cache:
            return self._grid_cache[station.id]
        try:
            pt = self.session.get(
                f"{self.BASE}/points/{station.latitude},{station.longitude}", timeout=20
            )
            pt.raise_for_status()
            props = pt.json()["properties"]
            self._grid_cache[station.id] = props
            return props
        except (requests.RequestException, KeyError) as exc:
            logger.warning("NWS gridpoint lookup failed for %s: %s", station.id, exc)
            return None

    def get_forecast(self, station: Station, target_date: date) -> Optional[Forecast]:
        props = self._gridpoint(station)
        if not props:
            return None
        try:
            resp = self.session.get(props["forecastGridData"], timeout=20)
            resp.raise_for_status()
            grid = resp.json()["properties"]
        except (requests.RequestException, KeyError) as exc:
            logger.warning("NWS grid data failed for %s: %s", station.id, exc)
            return None

        high = self._daily_extreme(grid.get("maxTemperature"), target_date, kind="max")
        low = self._daily_extreme(grid.get("minTemperature"), target_date, kind="min")
        precip_mm = self._daily_sum(grid.get("quantitativePrecipitation"), target_date)
        snow_mm = self._daily_sum(grid.get("snowfallAmount"), target_date)
        wind = self._daily_extreme(grid.get("windSpeed"), target_date, kind="max")

        horizon = (target_date - datetime.now(timezone.utc).date()).days
        return Forecast(
            location_id=station.id,
            target_date=target_date,
            source=self.name,
            horizon_days=max(horizon, 0),
            high_temp_f=_C_TO_F(high) if high is not None else None,
            low_temp_f=_C_TO_F(low) if low is not None else None,
            precip_in=_MM_TO_IN(precip_mm) if precip_mm is not None else None,
            snow_in=_MM_TO_IN(snow_mm) if snow_mm is not None else None,
            wind_mph=_KMH_TO_MPH(wind) if wind is not None else None,
            raw={"gridpoint": props.get("gridId")},
        )

    def get_observation(self, station: Station, target_date: date) -> Optional[Forecast]:
        """Official daily high/low from the exact settlement station.

        Kalshi temperature markets settle on the NWS daily climate report for a
        specific station (e.g. NYC = Central Park / KNYC). We take the max/min of
        that station's observations over the **local** calendar day, which tracks
        the official settlement far better than a coordinate-based estimate.
        """
        from zoneinfo import ZoneInfo

        try:
            tz = ZoneInfo(station.tz)
        except Exception:  # pragma: no cover
            tz = timezone.utc
        local_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz)
        start_utc = local_start.astimezone(timezone.utc)
        end_utc = (local_start.replace(hour=23, minute=59, second=59)).astimezone(timezone.utc)

        try:
            url = f"{self.BASE}/stations/{station.nws_station}/observations"
            resp = self.session.get(
                url,
                params={
                    "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                timeout=30,
            )
            resp.raise_for_status()
            features = resp.json().get("features", [])
        except requests.RequestException as exc:
            logger.warning("NWS observation failed for %s: %s", station.id, exc)
            return None

        temps_c = []
        for f in features:
            props = f["properties"]
            val = props.get("temperature", {}).get("value")
            if val is None:
                continue
            try:
                ts = datetime.fromisoformat(props["timestamp"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts.astimezone(tz).date() == target_date:
                temps_c.append(val)
        if not temps_c:
            return None
        return Forecast(
            location_id=station.id,
            target_date=target_date,
            source=self.name,
            horizon_days=0,
            high_temp_f=round(_C_TO_F(max(temps_c)), 1),
            low_temp_f=round(_C_TO_F(min(temps_c)), 1),
        )

    def get_intraday(self, station: Station) -> Optional[Forecast]:
        """Today's max/min temperature observed so far, plus local clock hour.

        This is the live feed behind the intraday edge: once the day's high is
        already in, a market still priced off the morning forecast is mispriced.

        The observation window MUST start at the station's *local* midnight, not
        UTC midnight — otherwise the previous evening's heat (which is "today" in
        UTC for western US stations) leaks in and fabricates a false high.
        """
        from zoneinfo import ZoneInfo

        try:
            tz = ZoneInfo(station.tz)
        except Exception:  # pragma: no cover - bad tz string
            tz = timezone.utc
        local_now = datetime.now(tz)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = local_midnight.astimezone(timezone.utc)
        local_hour = local_now.hour + local_now.minute / 60.0

        try:
            url = f"{self.BASE}/stations/{station.nws_station}/observations"
            resp = self.session.get(
                url,
                params={
                    "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": local_now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                timeout=20,
            )
            resp.raise_for_status()
            features = resp.json().get("features", [])
        except requests.RequestException as exc:
            logger.warning("NWS intraday failed for %s: %s", station.id, exc)
            return None

        # Keep only observations within today's LOCAL day.
        temps_c = []
        for f in features:
            props = f["properties"]
            val = props.get("temperature", {}).get("value")
            if val is None:
                continue
            try:
                ts = datetime.fromisoformat(props["timestamp"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts.astimezone(tz).date() == local_now.date():
                temps_c.append(val)
        if not temps_c:
            return None
        return Forecast(
            location_id=station.id,
            target_date=local_now.date(),
            source=self.name,
            horizon_days=0,
            observed_high_so_far=_C_TO_F(max(temps_c)),
            observed_low_so_far=_C_TO_F(min(temps_c)),
            local_hour=local_hour,
        )

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _iter_day_values(series: Optional[dict], target_date: date):
        for entry in (series or {}).get("values", []):
            valid = entry.get("validTime", "")
            stamp = valid.split("/")[0]
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()
            except ValueError:
                continue
            if when == target_date and entry.get("value") is not None:
                yield float(entry["value"])

    def _daily_extreme(self, series, target_date, kind: str) -> Optional[float]:
        vals = list(self._iter_day_values(series, target_date))
        if not vals:
            return None
        return max(vals) if kind == "max" else min(vals)

    def _daily_sum(self, series, target_date) -> Optional[float]:
        vals = list(self._iter_day_values(series, target_date))
        return sum(vals) if vals else None

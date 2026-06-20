"""Registry that maps source names to :class:`WeatherSource` instances.

This indirection is what makes the weather layer modular: to add Meteostat or a
commercial API, implement a :class:`WeatherSource` and register a factory here.
"""

from __future__ import annotations

from typing import Callable

from ..utils.config import Config
from ..utils.logging import get_logger
from .base import WeatherSource

logger = get_logger("weather.registry")

# name -> zero/one-arg factory
_FACTORIES: dict[str, Callable[[Config], WeatherSource]] = {}


def register(name: str, factory: Callable[[Config], WeatherSource]) -> None:
    """Register a weather-source factory under ``name``."""
    _FACTORIES[name] = factory


def available_sources() -> list[str]:
    """Names of all registered weather sources."""
    return sorted(_FACTORIES)


def get_source(config: Config, name: str | None = None) -> WeatherSource:
    """Return a weather source instance.

    In mock mode this always returns the deterministic mock source so the whole
    pipeline runs offline. Otherwise it builds the configured live source.
    """
    if config.mode == "mock":
        from ..utils.mock_data import MockWeatherSource

        return MockWeatherSource()

    name = name or str(config.get("weather.primary_source", "nws"))
    # The "ensemble" source pairs multi-model forecasts (consensus + spread)
    # with NWS for settlement-grade observations/intraday.
    if name == "ensemble":
        from .ensemble import CompositeWeatherSource, EnsembleForecastSource
        from .noaa import NWSWeatherSource

        ua = config.env_str("NWS_USER_AGENT", "kalshi-weather-scanner")
        return CompositeWeatherSource(EnsembleForecastSource(), NWSWeatherSource(user_agent=ua))

    factory = _FACTORIES.get(name)
    if factory is None:
        logger.warning("Unknown weather source %r; falling back to nws", name)
        factory = _FACTORIES["nws"]
    return factory(config)


# -- built-in registrations ---------------------------------------------------
def _make_nws(config: Config) -> WeatherSource:
    from .noaa import NWSWeatherSource

    ua = config.env_str("NWS_USER_AGENT", "kalshi-weather-scanner")
    return NWSWeatherSource(user_agent=ua)


def _make_open_meteo(config: Config) -> WeatherSource:
    from .open_meteo import OpenMeteoSource

    return OpenMeteoSource()


register("nws", _make_nws)
register("open_meteo", _make_open_meteo)

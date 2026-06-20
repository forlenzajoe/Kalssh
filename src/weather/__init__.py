"""Modular weather data sources and forecast retrieval."""

from .base import Forecast, WeatherSource  # noqa: F401
from .registry import get_source, available_sources  # noqa: F401

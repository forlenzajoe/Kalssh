"""Filter Kalshi markets down to weather-related ones."""

from __future__ import annotations

from ..utils.config import Config
from .models import Market


def is_weather_market(market: Market, prefixes: list[str], keywords: list[str]) -> bool:
    """Return True if a market looks weather-related.

    Matches on (a) series/ticker prefix or (b) keyword in title/category. The
    prefix check is the stronger signal; the keyword check is a fallback.
    """
    ticker = (market.series_ticker or market.ticker).upper()
    if any(ticker.startswith(p.upper()) for p in prefixes):
        return True

    haystack = f"{market.title} {market.category}".lower()
    return any(kw.lower() in haystack for kw in keywords)


def filter_weather_markets(markets: list[Market], config: Config) -> list[Market]:
    """Return only the weather markets from a list, using config rules."""
    prefixes = config.get("kalshi.weather_series_prefixes", []) or []
    keywords = config.get("kalshi.weather_keywords", []) or []
    return [m for m in markets if is_weather_market(m, prefixes, keywords)]

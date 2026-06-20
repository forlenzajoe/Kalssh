"""Deterministic mock data so the whole pipeline runs without API keys.

Provides:
  * :func:`mock_markets` — a realistic set of Kalshi weather markets (plus a
    couple of non-weather and edge-case markets to exercise filters/risk gates).
  * :func:`mock_order_book` — order books consistent with each market's top-of-book.
  * :class:`MockWeatherSource` — fixed seasonal forecasts/observations per city.

The numbers are chosen so the scanner surfaces a mix of Buy YES, Buy NO, Watch
and Avoid recommendations, which makes the dashboard and tests meaningful.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from ..weather.base import Forecast, WeatherSource
from ..weather.stations import STATIONS, Station
from ..kalshi.models import Market, OrderBook, OrderBookLevel


# Fixed June-ish point forecasts per station (high °F, low °F, precip in, snow in, wind mph).
STATION_FORECASTS: dict[str, dict[str, float]] = {
    "nyc": {"high": 82.0, "low": 66.0, "precip": 0.10, "snow": 0.0, "wind": 10.0},
    "lax": {"high": 75.0, "low": 61.0, "precip": 0.00, "snow": 0.0, "wind": 8.0},
    "chi": {"high": 79.0, "low": 60.0, "precip": 0.20, "snow": 0.0, "wind": 12.0},
    "mia": {"high": 89.0, "low": 78.0, "precip": 0.50, "snow": 0.0, "wind": 11.0},
    "den": {"high": 85.0, "low": 55.0, "precip": 0.05, "snow": 0.0, "wind": 14.0},
    "phl": {"high": 84.0, "low": 65.0, "precip": 0.08, "snow": 0.0, "wind": 9.0},
    "aus": {"high": 95.0, "low": 74.0, "precip": 0.02, "snow": 0.0, "wind": 9.0},
    "dca": {"high": 86.0, "low": 68.0, "precip": 0.06, "snow": 0.0, "wind": 8.0},
    "sea": {"high": 70.0, "low": 54.0, "precip": 0.15, "snow": 0.0, "wind": 7.0},
    "bos": {"high": 76.0, "low": 60.0, "precip": 0.12, "snow": 0.0, "wind": 11.0},
}


def _close(days_ahead: int) -> datetime:
    """Close time relative to *now* so 'hours to close' is always positive.

    Same-day (intraday) markets close ~8 hours out; multi-day markets close at
    the end of their target day.
    """
    now = datetime.now(timezone.utc)
    if days_ahead <= 0:
        return now + timedelta(hours=8)
    target = now.date() + timedelta(days=days_ahead)
    return datetime.combine(target, time(23, 59), tzinfo=timezone.utc)


def _date_str(days_ahead: int) -> str:
    # Windows strftime has no %-d; build manually for portability.
    d = date.today() + timedelta(days=days_ahead)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


# (ticker, series, title, category, yes_bid, yes_ask, volume, oi, liq_cents, days_ahead, thin)
_MARKET_SPECS = [
    ("HIGHNY-B80", "HIGHNY",
     "Will the high temperature in NYC be above 80F on {d0}?",
     "Climate", 66, 70, 1200, 3400, 50000, 0, False),
    ("HIGHMIA-B92", "HIGHMIA",
     "Will the high temperature in Miami be above 92F on {d0}?",
     "Climate", 21, 25, 900, 2100, 40000, 0, False),
    ("HIGHLAX-B74", "HIGHLAX",
     "Will the high temperature in Los Angeles be above 74F on {d0}?",
     "Climate", 60, 64, 800, 1500, 30000, 0, False),
    ("LOWAUS-B76", "LOWAUS",
     "Will the low temperature in Austin be below 76F on {d0}?",
     "Climate", 66, 70, 700, 1300, 25000, 0, False),
    ("RAINCHI-010", "RAINCHI",
     "Will it rain more than 0.10 inches in Chicago on {d0}?",
     "Climate", 56, 60, 600, 1100, 22000, 0, False),
    ("SNOWDEN-B1", "SNOWDEN",
     "Will snowfall in Denver exceed 1 inch on {d0}?",
     "Climate", 8, 12, 400, 900, 15000, 0, False),
    # Long horizon (7 days) -> confidence penalty.
    ("HIGHPHL-B85", "HIGHPHL",
     "Will the high temperature in Philadelphia be above 85F on {d7}?",
     "Climate", 48, 52, 300, 700, 12000, 7, False),
    # Wide spread -> should be downgraded to Watch.
    ("HIGHBOS-B78", "HIGHBOS",
     "Will the high temperature in Boston be above 78F on {d0}?",
     "Climate", 50, 70, 200, 500, 8000, 0, False),
    # Thin liquidity -> Watch.
    ("HIGHSEA-B70", "HIGHSEA",
     "Will the high temperature in Seattle be above 70F on {d0}?",
     "Climate", 47, 53, 20, 60, 1500, 0, True),
    # Ambiguous settlement (no resolvable city) -> Avoid.
    ("HIGHXX-B90", "HIGHXX",
     "Will the high temperature be above 90F tomorrow?",
     "Climate", 40, 45, 150, 400, 6000, 1, False),
    # Non-weather markets (should be filtered out).
    ("FED-JUL26", "FED",
     "Will the Fed raise interest rates in July 2026?",
     "Economics", 30, 34, 5000, 12000, 200000, 30, False),
    ("ELECTION-X", "PREZ",
     "Will turnout exceed 60% in the 2026 midterms?",
     "Politics", 45, 49, 8000, 20000, 300000, 120, False),
]


def _build_book(ticker: str, yes_bid: int, yes_ask: int, thin: bool) -> OrderBook:
    """Synthesize a small multi-level book around the given top-of-book."""
    qty = 8 if thin else 120
    yes_bids = [
        OrderBookLevel(yes_bid, qty),
        OrderBookLevel(max(yes_bid - 1, 1), qty // 2),
        OrderBookLevel(max(yes_bid - 2, 1), qty // 3),
    ]
    yes_asks = [
        OrderBookLevel(yes_ask, qty),
        OrderBookLevel(min(yes_ask + 1, 99), qty // 2),
        OrderBookLevel(min(yes_ask + 2, 99), qty // 3),
    ]
    # NO side is the mirror (no_bid = 100 - yes_ask, etc.).
    no_bids = [OrderBookLevel(100 - lvl.price_cents, lvl.quantity) for lvl in yes_asks]
    no_asks = [OrderBookLevel(100 - lvl.price_cents, lvl.quantity) for lvl in yes_bids]
    no_bids.sort(key=lambda l: l.price_cents, reverse=True)
    no_asks.sort(key=lambda l: l.price_cents)
    return OrderBook(ticker, yes_bids, yes_asks, no_bids, no_asks)


_CACHE: dict[str, object] = {}


def _generate() -> tuple[list[Market], dict[str, OrderBook]]:
    if "markets" in _CACHE:
        return _CACHE["markets"], _CACHE["books"]  # type: ignore[return-value]

    markets: list[Market] = []
    books: dict[str, OrderBook] = {}
    subs = {"d0": _date_str(0), "d7": _date_str(7)}

    for (tk, series, title_tpl, cat, yb, ya, vol, oi, liq, days, thin) in _MARKET_SPECS:
        title = title_tpl.format(**subs)
        market = Market(
            ticker=tk,
            title=title,
            category=cat,
            status="active",
            close_time=_close(days),
            yes_bid=yb,
            yes_ask=ya,
            no_bid=100 - ya,
            no_ask=100 - yb,
            last_price=(yb + ya) // 2,
            volume=vol,
            open_interest=oi,
            liquidity_cents=liq,
            rules_primary=(
                "Settles to YES based on the official NWS daily climate report "
                "for the named city's primary station."
            ),
            series_ticker=series,
            event_ticker=tk,
        )
        book = _build_book(tk, yb, ya, thin)
        market.order_book = book
        markets.append(market)
        books[tk] = book

    _CACHE["markets"] = markets
    _CACHE["books"] = books
    return markets, books


def mock_markets() -> list[Market]:
    """Return the mock set of active markets (fresh copies via cache)."""
    markets, _ = _generate()
    return markets


def mock_order_book(ticker: str) -> Optional[OrderBook]:
    """Return the mock order book for a ticker, if present."""
    _, books = _generate()
    return books.get(ticker)


class MockWeatherSource(WeatherSource):
    """Deterministic forecasts/observations for offline runs and tests."""

    name = "mock"

    def _forecast(self, station: Station, target_date: date, horizon: int) -> Optional[Forecast]:
        data = STATION_FORECASTS.get(station.id)
        if data is None:
            return None
        return Forecast(
            location_id=station.id,
            target_date=target_date,
            source=self.name,
            horizon_days=horizon,
            high_temp_f=data["high"],
            low_temp_f=data["low"],
            precip_in=data["precip"],
            snow_in=data["snow"],
            wind_mph=data["wind"],
            precip_prob=0.6 if data["precip"] > 0.05 else 0.1,
        )

    def get_forecast(self, station: Station, target_date: date) -> Optional[Forecast]:
        horizon = max((target_date - date.today()).days, 0)
        return self._forecast(station, target_date, horizon)

    def get_intraday(self, station: Station) -> Optional[Forecast]:
        """Deterministic 'observations so far today' for offline demos/tests.

        Simulates early-afternoon (local hour 13): the high-so-far is a couple
        degrees below the forecast high (still warming).
        """
        data = STATION_FORECASTS.get(station.id)
        if data is None:
            return None
        return Forecast(
            location_id=station.id, target_date=date.today(), source=self.name,
            horizon_days=0,
            observed_high_so_far=data["high"] - 2.0,
            observed_low_so_far=data["low"],
            local_hour=13.0,
        )

    def get_observation(self, station: Station, target_date: date) -> Optional[Forecast]:
        # Observation == forecast point value plus a tiny deterministic nudge so
        # backtests have non-trivial residuals.
        fc = self._forecast(station, target_date, 0)
        if fc is None:
            return None
        nudge = ((hash((station.id, target_date.toordinal())) % 7) - 3) * 1.0
        fc.high_temp_f = (fc.high_temp_f or 0.0) + nudge
        fc.low_temp_f = (fc.low_temp_f or 0.0) + nudge * 0.5
        return fc


# Convenience export for stations (used by tests).
ALL_STATIONS = STATIONS

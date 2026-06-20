"""City / station registry used to map Kalshi markets to settlement sources.

Kalshi temperature markets settle off specific NWS climate stations (often the
primary airport station for a city). This registry maps common location tokens
to a :class:`Station` with the coordinates and station id needed to query NWS.

If a market's location cannot be resolved here, the scanner flags it as having
an AMBIGUOUS settlement source and penalizes/avoids it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Station:
    """A weather station tied to a city for settlement purposes."""

    id: str                # internal id, e.g. "nyc"
    name: str
    nws_station: str       # NWS/GHCN station id (e.g. "KNYC")
    latitude: float
    longitude: float
    tz: str = "America/New_York"   # IANA timezone for correct local-day boundaries
    aliases: tuple[str, ...] = ()


# Primary settlement stations for the cities Kalshi commonly lists.
STATIONS: dict[str, Station] = {
    "nyc": Station("nyc", "New York City (Central Park)", "KNYC", 40.7790, -73.9690,
                   tz="America/New_York", aliases=("new york", "nyc", "ny", "central park")),
    "lax": Station("lax", "Los Angeles (LAX)", "KLAX", 33.9382, -118.3866,
                   tz="America/Los_Angeles", aliases=("los angeles", "la", "lax")),
    "chi": Station("chi", "Chicago (Midway)", "KMDW", 41.7842, -87.7553,
                   tz="America/Chicago", aliases=("chicago", "chi", "midway")),
    "mia": Station("mia", "Miami Intl", "KMIA", 25.7906, -80.3164,
                   tz="America/New_York", aliases=("miami", "mia")),
    "den": Station("den", "Denver Intl", "KDEN", 39.8466, -104.6562,
                   tz="America/Denver", aliases=("denver", "den")),
    "phl": Station("phl", "Philadelphia Intl", "KPHL", 39.8729, -75.2407,
                   tz="America/New_York", aliases=("philadelphia", "philly", "phl")),
    "aus": Station("aus", "Austin-Bergstrom", "KAUS", 30.1975, -97.6664,
                   tz="America/Chicago", aliases=("austin", "aus")),
    "dca": Station("dca", "Washington (Reagan National)", "KDCA", 38.8521, -77.0377,
                   tz="America/New_York", aliases=("washington", "dc", "d.c.", "reagan", "dca")),
    "sea": Station("sea", "Seattle-Tacoma", "KSEA", 47.4502, -122.3088,
                   tz="America/Los_Angeles", aliases=("seattle", "sea", "sea-tac")),
    "bos": Station("bos", "Boston Logan", "KBOS", 42.3656, -71.0096,
                   tz="America/New_York", aliases=("boston", "bos", "logan")),
}


def resolve_station(text: str) -> Optional[Station]:
    """Resolve a free-text location/title to a :class:`Station`.

    Matches against station ids and aliases (longest alias first to avoid
    partial collisions). Returns ``None`` when nothing matches — the caller
    should then treat settlement as ambiguous.
    """
    if not text:
        return None
    haystack = text.lower()
    # Build (alias, station) pairs sorted by alias length descending.
    pairs: list[tuple[str, Station]] = []
    for station in STATIONS.values():
        pairs.append((station.id, station))
        for alias in station.aliases:
            pairs.append((alias, station))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    for alias, station in pairs:
        if alias in haystack:
            return station
    return None

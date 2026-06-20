"""Live edge watcher — hunts for genuine, model-free, post-fee edges.

Two detectors, both independent of the (unproven) forecast model:

1. **Locked markets** — the daily high is monotonic (it only rises), so once the
   *integer* high already crosses a strike, that market is mathematically
   settled. If a settled outcome still trades away from $1.00, that is free money
   (subject to liquidity). Integer-settlement rounding is applied so we don't
   repeat the Miami false-positive (93.2°F settles as 93, i.e. inside "92-93").

2. **Complementary-pair arbitrage** — two markets that are exact logical
   opposites ("high >= K" and "high <= K-1") must have exactly one winner. If
   their YES asks sum to less than $1 net of fees, buying both is risk-free.

Every candidate is checked for executable price, fees, and fillable size. The
honest expectation is that most scans find nothing — the market is efficient.
The point is to be there the moment one appears.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from zoneinfo import ZoneInfo

from .kalshi.client import KalshiClient
from .models.contract_parser import parse_contract
from .utils.config import Config
from .utils.logging import get_logger
from .weather.noaa import NWSWeatherSource
from .weather.stations import STATIONS

logger = get_logger("edge_watcher")

# Kalshi high-temp series -> station id.
SERIES_STATION = {
    "KXHIGHNY": "nyc", "KXHIGHCHI": "chi", "KXHIGHLAX": "lax", "KXHIGHMIA": "mia",
    "KXHIGHAUS": "aus", "KXHIGHDEN": "den", "KXHIGHPHIL": "phl",
}


@dataclass
class EdgeAlert:
    """A detected, actionable edge."""

    kind: str                 # "locked" | "arbitrage"
    timestamp: str
    market: str               # ticker (or "A | B" for an arb pair)
    description: str
    action: str               # e.g. "BUY YES", "BUY NO", "BUY BOTH YES"
    cost_cents: float         # executable cost to enter
    net_edge_cents: float     # profit per contract/pair after fees
    contracts_available: int  # fillable size at the executable price (0 = unknown)
    volume: int
    notes: str = ""

    def key(self) -> str:
        return f"{self.kind}:{self.market}:{self.action}"

    def as_row(self) -> dict:
        return asdict(self)


def _fee_cents(price_cents: float) -> float:
    """Kalshi per-contract fee in cents for a fill at ``price_cents``."""
    p = price_cents / 100.0
    return math.ceil(0.07 * p * (1.0 - p) * 100.0) / 100.0


def _ask_depth(client: KalshiClient, ticker: str, side: str) -> int:
    """Best-effort fillable size (contracts) at the top of book for a side."""
    try:
        book = client.get_order_book(ticker)
    except Exception:  # pragma: no cover - network
        return 0
    if book is None:
        return 0
    levels = book.yes_asks if side == "yes" else book.no_asks
    return levels[0].quantity if levels else 0


def find_locked_edges(
    client: KalshiClient, config: Config, min_net_cents: float = 1.0
) -> list[EdgeAlert]:
    """Find already-settled markets still trading away from $1 (post-fee)."""
    nws = NWSWeatherSource(user_agent=config.env_str("NWS_USER_AGENT", "kalshi-weather-scanner"))
    alerts: list[EdgeAlert] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for series, sid in SERIES_STATION.items():
        station = STATIONS[sid]
        intra = nws.get_intraday(station)
        if intra is None or intra.observed_high_so_far is None:
            continue
        int_high = round(intra.observed_high_so_far)          # integer settlement
        today = datetime.now(ZoneInfo(station.tz)).date()
        tkey = today.strftime("%y%b%d").upper()

        for m in client.get_markets_by_series(series):
            if tkey not in m.ticker.upper():
                continue
            cond = parse_contract(m.title, subtitle=m.subtitle, rules=m.rules_primary)
            if cond.threshold is None:
                continue

            side = price = None
            if cond.operator in ("gte", "gt") and int_high >= cond.threshold:
                side, price = "yes", m.yes_ask                # certain YES
            elif cond.operator in ("lte", "lt") and int_high > cond.threshold:
                side = "no"
                price = (100 - m.yes_bid) if m.yes_bid is not None else None
            elif (cond.operator == "between" and cond.threshold2 is not None
                  and int_high > max(cond.threshold, cond.threshold2)):
                side = "no"
                price = (100 - m.yes_bid) if m.yes_bid is not None else None

            if side is None or price is None or price >= 99:
                continue
            net = (100 - price) - _fee_cents(price)
            if net < min_net_cents:
                continue
            depth = _ask_depth(client, m.ticker, side)
            if depth <= 0:
                continue
            alerts.append(EdgeAlert(
                kind="locked", timestamp=now_iso, market=m.ticker,
                description=f"{cond.describe()} | observed high so far {int_high}°F",
                action=f"BUY {side.upper()}", cost_cents=float(price),
                net_edge_cents=round(net, 2), contracts_available=depth,
                volume=m.volume,
                notes="Outcome already mathematically settled (high only rises).",
            ))
    return alerts


def find_arbitrage_edges(
    client: KalshiClient, config: Config, min_net_cents: float = 0.5
) -> list[EdgeAlert]:
    """Find complementary market pairs whose YES asks sum to < $1 after fees."""
    alerts: list[EdgeAlert] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    ladders: dict[str, list] = {}

    for series in SERIES_STATION:
        for m in client.get_markets_by_series(series):
            cond = parse_contract(m.title, subtitle=m.subtitle, rules=m.rules_primary)
            if cond.threshold is None or m.yes_ask is None:
                continue
            ev = m.ticker.rsplit("-", 1)[0]
            ladders.setdefault(ev, []).append((m, cond))

    for ev, items in ladders.items():
        gte = {int(c.threshold): m for (m, c) in items if c.operator in ("gte", "gt")}
        lte = {int(c.threshold): m for (m, c) in items if c.operator in ("lte", "lt")}
        for k, mg in gte.items():
            ml = lte.get(k - 1)                                # exact complement
            if ml is None or ml.yes_ask is None:
                continue
            cost = mg.yes_ask + ml.yes_ask
            fees = _fee_cents(mg.yes_ask) + _fee_cents(ml.yes_ask)
            net = 100 - cost - fees
            if net < min_net_cents:
                continue
            depth = min(_ask_depth(client, mg.ticker, "yes"),
                        _ask_depth(client, ml.ticker, "yes"))
            if depth <= 0:
                continue
            alerts.append(EdgeAlert(
                kind="arbitrage", timestamp=now_iso,
                market=f"{mg.ticker} | {ml.ticker}",
                description=f"YES(>={k})@{mg.yes_ask}c + YES(<={k-1})@{ml.yes_ask}c",
                action="BUY BOTH YES", cost_cents=float(cost),
                net_edge_cents=round(net, 2), contracts_available=depth,
                volume=min(mg.volume, ml.volume),
                notes="Exact complements: exactly one must settle YES.",
            ))
    return alerts


def scan_for_edges(config: Config, client: Optional[KalshiClient] = None) -> list[EdgeAlert]:
    """Run both detectors once and return all actionable edges."""
    client = client or KalshiClient(config)
    if client.mock:
        logger.info("Edge watcher needs live mode (mock has no real books).")
    alerts = find_locked_edges(client, config) + find_arbitrage_edges(client, config)
    alerts.sort(key=lambda a: a.net_edge_cents, reverse=True)
    return alerts

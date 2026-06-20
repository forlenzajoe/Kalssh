"""Typed data models for Kalshi markets and order books.

Kalshi prices are integer cents in [1, 99]. A YES contract settles to $1 if the
event occurs and $0 otherwise. ``yes_*`` and ``no_*`` prices satisfy the
identity ``no_bid = 100 - yes_ask`` and ``no_ask = 100 - yes_bid``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class OrderBookLevel:
    """A single price level: price in cents and resting quantity (contracts)."""

    price_cents: int
    quantity: int


@dataclass
class OrderBook:
    """Top-of-book and depth for a single market.

    Levels are stored best-first. ``yes`` levels are the YES side; ``no`` levels
    are the NO side. We derive executable top-of-book from these.
    """

    ticker: str
    yes_bids: list[OrderBookLevel] = field(default_factory=list)
    yes_asks: list[OrderBookLevel] = field(default_factory=list)
    no_bids: list[OrderBookLevel] = field(default_factory=list)
    no_asks: list[OrderBookLevel] = field(default_factory=list)

    @property
    def best_yes_bid(self) -> Optional[int]:
        return self.yes_bids[0].price_cents if self.yes_bids else None

    @property
    def best_yes_ask(self) -> Optional[int]:
        return self.yes_asks[0].price_cents if self.yes_asks else None

    @property
    def best_no_bid(self) -> Optional[int]:
        return self.no_bids[0].price_cents if self.no_bids else None

    @property
    def best_no_ask(self) -> Optional[int]:
        return self.no_asks[0].price_cents if self.no_asks else None

    def depth_contracts(self, side: str = "yes", levels: int = 3) -> int:
        """Sum of resting quantity across the top ``levels`` of one side."""
        book = {
            "yes": self.yes_asks,
            "no": self.no_asks,
            "yes_bid": self.yes_bids,
            "no_bid": self.no_bids,
        }.get(side, self.yes_asks)
        return sum(level.quantity for level in book[:levels])


@dataclass
class Market:
    """A Kalshi market with the fields the scanner needs.

    Prices (``yes_bid``, ``yes_ask`` ...) are best executable top-of-book in
    cents. ``None`` means no resting interest on that side.
    """

    ticker: str
    title: str
    category: str
    status: str                       # "active", "closed", "settled", ...
    close_time: Optional[datetime]
    subtitle: str = ""                # canonical YES definition (yes_sub_title)
    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    no_bid: Optional[int] = None
    no_ask: Optional[int] = None
    last_price: Optional[int] = None
    volume: int = 0
    open_interest: int = 0
    liquidity_cents: int = 0          # Kalshi-reported notional liquidity
    rules_primary: str = ""           # settlement rule text
    rules_secondary: str = ""
    series_ticker: str = ""
    event_ticker: str = ""
    order_book: Optional[OrderBook] = None

    # -- convenience ----------------------------------------------------------
    @property
    def spread_cents(self) -> Optional[int]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def mid_cents(self) -> Optional[float]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2.0

    def hours_to_close(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.close_time is None:
            return None
        now = now or datetime.now(timezone.utc)
        if self.close_time.tzinfo is None:
            close = self.close_time.replace(tzinfo=timezone.utc)
        else:
            close = self.close_time
        return (close - now).total_seconds() / 3600.0

    @classmethod
    def from_api(cls, payload: dict) -> "Market":
        """Build a :class:`Market` from a Kalshi REST ``market`` object.

        Handles both the legacy integer-cents schema (``yes_bid``, ``volume``)
        and the current decimal-dollar schema (``yes_bid_dollars``,
        ``volume_fp``, ``liquidity_dollars``). Prices are normalized to integer
        cents internally; a price of 0 is treated as "no quote" (``None``).
        """
        def cents(*keys, zero_is_none: bool = True) -> Optional[int]:
            for k in keys:
                if k in payload and payload[k] not in (None, ""):
                    raw = payload[k]
                    try:
                        # *_dollars are strings like "0.6500"; legacy are ints (cents)
                        val = round(float(raw) * 100) if k.endswith("_dollars") else int(raw)
                    except (TypeError, ValueError):
                        continue
                    if zero_is_none and val == 0:
                        return None
                    return val
            return None

        def number(*keys) -> int:
            for k in keys:
                if k in payload and payload[k] not in (None, ""):
                    try:
                        return int(float(payload[k]))
                    except (TypeError, ValueError):
                        continue
            return 0

        close_raw = (payload.get("close_time") or payload.get("expected_expiration_time")
                     or payload.get("expiration_time"))
        close_dt: Optional[datetime] = None
        if close_raw:
            try:
                close_dt = datetime.fromisoformat(str(close_raw).replace("Z", "+00:00"))
            except ValueError:
                close_dt = None

        return cls(
            ticker=payload.get("ticker", ""),
            title=payload.get("title", "") or payload.get("subtitle", ""),
            category=payload.get("category", "") or "",
            status=payload.get("status", "unknown"),
            close_time=close_dt,
            subtitle=payload.get("yes_sub_title") or payload.get("subtitle", "") or "",
            yes_bid=cents("yes_bid", "yes_bid_dollars"),
            yes_ask=cents("yes_ask", "yes_ask_dollars"),
            no_bid=cents("no_bid", "no_bid_dollars"),
            no_ask=cents("no_ask", "no_ask_dollars"),
            last_price=cents("last_price", "last_price_dollars"),
            volume=number("volume", "volume_fp"),
            open_interest=number("open_interest", "open_interest_fp"),
            liquidity_cents=cents("liquidity", "liquidity_dollars", zero_is_none=False) or 0,
            rules_primary=payload.get("rules_primary", "") or "",
            rules_secondary=payload.get("rules_secondary", "") or "",
            series_ticker=payload.get("series_ticker", "") or "",
            event_ticker=payload.get("event_ticker", "") or "",
        )

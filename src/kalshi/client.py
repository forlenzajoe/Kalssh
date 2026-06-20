"""Kalshi REST client with an automatic mock-data fallback.

In ``mock`` mode (or whenever credentials are missing) the client reads bundled
fixtures from :mod:`src.utils.mock_data` and never touches the network. In
``live`` mode it talks to the Kalshi v2 trade API using RSA request signing.

The live path is intentionally read-only: this project does NOT place orders.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

import requests

from ..utils.config import Config
from ..utils.logging import get_logger
from . import filters
from .models import Market, OrderBook, OrderBookLevel

logger = get_logger("kalshi.client")

_BASES = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}


class KalshiClient:
    """Read-only Kalshi market data client.

    Args:
        config: Loaded :class:`Config`.
        force_mock: Override to force mock mode regardless of config.
    """

    def __init__(self, config: Config, force_mock: bool = False) -> None:
        self.config = config
        self.mock = force_mock or config.mode == "mock"
        self.env = str(config.get("kalshi.env", "demo"))
        self.base_url = config.env_str("KALSHI_API_BASE") or _BASES.get(
            self.env, _BASES["demo"]
        )
        self._session = requests.Session()
        self._private_key = None
        self._key_id = config.env_str("KALSHI_API_KEY_ID")

        if self.mock:
            logger.info("KalshiClient running in MOCK mode (no network).")
        else:
            # Market/orderbook reads are PUBLIC. Only load a signing key if the
            # user supplied one (not needed for this read-only project).
            if self._key_id and self.config.env_str("KALSHI_PRIVATE_KEY_PATH"):
                try:
                    self._load_private_key()
                    logger.info("KalshiClient LIVE (authenticated) against %s", self.base_url)
                except Exception as exc:  # pragma: no cover - optional
                    logger.warning("Could not load Kalshi key (%s); using public reads.", exc)
                    self._key_id = ""
            else:
                self._key_id = ""
                logger.info("KalshiClient LIVE (public read-only) against %s", self.base_url)

    # -- auth -----------------------------------------------------------------
    def _load_private_key(self) -> None:
        """Load the RSA private key used to sign requests (live mode)."""
        path = self.config.env_str("KALSHI_PRIVATE_KEY_PATH")
        if not path:
            raise RuntimeError("KALSHI_PRIVATE_KEY_PATH is required in live mode.")
        try:
            from cryptography.hazmat.primitives import serialization

            with open(path, "rb") as fh:
                self._private_key = serialization.load_pem_private_key(
                    fh.read(), password=None
                )
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "The 'cryptography' package is required for live Kalshi auth. "
                "Install it or run in mock mode."
            ) from exc

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Build Kalshi RSA-PSS signed auth headers for a request."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method.upper()}{path}".encode("utf-8")
        signature = self._private_key.sign(  # type: ignore[union-attr]
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = self.base_url + path
        headers = self._auth_headers("GET", "/trade-api/v2" + path) if self._key_id else {}
        resp = self._session.get(url, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()

    # -- public API -----------------------------------------------------------
    def get_active_markets(self) -> list[Market]:
        """Return all active markets (mock fixtures or live, paginated)."""
        if self.mock:
            from ..utils.mock_data import mock_markets

            return mock_markets()

        markets: list[Market] = []
        cursor: Optional[str] = None
        while True:
            params = {"status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params=params)
            for raw in data.get("markets", []):
                markets.append(Market.from_api(raw))
            cursor = data.get("cursor")
            if not cursor:
                break
        logger.info("Pulled %d active markets from Kalshi", len(markets))
        return markets

    def get_markets_by_series(self, series_ticker: str) -> list[Market]:
        """Fetch all open markets for a single series (live)."""
        markets: list[Market] = []
        cursor: Optional[str] = None
        while True:
            params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params=params)
            for raw in data.get("markets", []):
                market = Market.from_api(raw)
                if not market.series_ticker:
                    market.series_ticker = series_ticker
                markets.append(market)
            cursor = data.get("cursor")
            if not cursor:
                break
        return markets

    def get_weather_markets(self) -> list[Market]:
        """Return only weather-related active markets.

        Live mode queries the configured weather *series* directly (fast and
        targeted). If that fails (e.g. no network), it falls back to mock data so
        the app stays usable, and logs a clear warning.
        """
        if self.mock:
            weather = filters.filter_weather_markets(self.get_active_markets(), self.config)
            logger.info("Filtered %d weather markets (mock)", len(weather))
            return weather

        series_list = self.config.get("kalshi.weather_series", []) or []
        try:
            markets: list[Market] = []
            for series in series_list:
                found = self.get_markets_by_series(series)
                logger.info("Series %s -> %d open markets", series, len(found))
                markets.extend(found)
            if not markets:
                # Series list empty/unknown -> fall back to scanning all markets.
                markets = filters.filter_weather_markets(self.get_active_markets(), self.config)
            logger.info("Pulled %d live weather markets", len(markets))
            return markets
        except requests.RequestException as exc:
            logger.warning("Live Kalshi fetch failed (%s); falling back to MOCK data.", exc)
            self.mock = True
            from ..utils.mock_data import mock_markets

            return filters.filter_weather_markets(mock_markets(), self.config)

    def get_order_book(self, ticker: str, depth: int = 10) -> Optional[OrderBook]:
        """Fetch and normalize an order book for a market."""
        if self.mock:
            from ..utils.mock_data import mock_order_book

            return mock_order_book(ticker)

        data = self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        book = data.get("orderbook", {})
        return self._parse_order_book(ticker, book)

    @staticmethod
    def _parse_order_book(ticker: str, book: dict) -> OrderBook:
        """Convert a Kalshi orderbook payload into an :class:`OrderBook`.

        Kalshi returns ``yes`` and ``no`` arrays of ``[price_cents, quantity]``
        representing resting BIDS on each side. The YES ask is derived from the
        best NO bid: ``yes_ask = 100 - best_no_bid``.
        """
        def levels(rows: Iterable) -> list[OrderBookLevel]:
            out = [OrderBookLevel(int(p), int(q)) for p, q in (rows or [])]
            out.sort(key=lambda level: level.price_cents, reverse=True)
            return out

        yes_bids = levels(book.get("yes"))
        no_bids = levels(book.get("no"))
        yes_asks = [OrderBookLevel(100 - lvl.price_cents, lvl.quantity) for lvl in no_bids]
        yes_asks.sort(key=lambda level: level.price_cents)
        no_asks = [OrderBookLevel(100 - lvl.price_cents, lvl.quantity) for lvl in yes_bids]
        no_asks.sort(key=lambda level: level.price_cents)
        return OrderBook(
            ticker=ticker,
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            no_bids=no_bids,
            no_asks=no_asks,
        )

    def enrich_with_order_books(self, markets: list[Market]) -> list[Market]:
        """Attach order books and refresh top-of-book on each market.

        In live mode we skip markets with no sign of life (no quote, no volume,
        no open interest) to avoid hundreds of pointless requests, and tolerate
        per-market errors so one bad book never sinks the whole scan.
        """
        for market in markets:
            if not self.mock:
                has_life = (
                    market.yes_bid is not None or market.yes_ask is not None
                    or market.volume > 0 or market.open_interest > 0
                )
                if not has_life:
                    continue
            try:
                book = self.get_order_book(market.ticker)
            except requests.RequestException as exc:  # pragma: no cover - network
                logger.debug("Orderbook fetch failed for %s: %s", market.ticker, exc)
                continue
            if book is None:
                continue
            market.order_book = book
            if book.best_yes_bid is not None:
                market.yes_bid = book.best_yes_bid
            if book.best_yes_ask is not None:
                market.yes_ask = book.best_yes_ask
            if book.best_no_bid is not None:
                market.no_bid = book.best_no_bid
            if book.best_no_ask is not None:
                market.no_ask = book.best_no_ask
        return markets


def utcnow() -> datetime:
    """UTC now (timezone-aware) helper used across modules."""
    return datetime.now(timezone.utc)

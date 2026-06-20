"""Shared pytest fixtures and path setup."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.config import Config  # noqa: E402


@pytest.fixture
def config() -> Config:
    """A minimal in-memory mock-mode config for tests."""
    return Config(data={
        "mode": "mock",
        "trading": {"paper_only": True, "bankroll_usd": 1000.0},
        "fees": {"trade_fee_rate": 0.07, "slippage_cents": 1, "assume_taker": True},
        "risk": {
            "min_edge": 0.05, "min_ev_after_fees": 0.02, "min_confidence": 0.50,
            "min_liquidity_score": 0.30, "max_spread_cents": 8,
            "max_position_per_market_usd": 50.0, "max_daily_exposure_usd": 200.0,
            "avoid_ambiguous_settlement": True, "kelly_fraction": 0.25,
        },
        "weather": {"temp_error_sigma_f": {0: 2.5, 1: 3.0, 7: 9.0}},
        "model": {"default": "normal_forecast",
                  "confidence": {"long_horizon_days": 7, "long_horizon_penalty": 0.6,
                                 "ambiguous_settlement_penalty": 0.4}},
        "kalshi": {"weather_series_prefixes": ["HIGH", "LOW", "RAIN", "SNOW"],
                   "weather_keywords": ["temperature", "rain", "snow"]},
        "paper_trading": {"store": "csv", "csv_path": "data/test_trades.csv",
                          "signal_threshold_edge": 0.05},
    }, env={})

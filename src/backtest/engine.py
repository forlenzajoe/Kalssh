"""Backtest engine.

Two entry points:

* :func:`run_synthetic_backtest` — replays the bundled mock markets across a
  range of past days, using forecasts as predictions and (nudged) observations
  as outcomes. Fully offline; good for validating the plumbing and calibration.

* :func:`run_backtest` — loads pre-computed samples from a CSV so you can drop in
  archived Kalshi prices + realized weather outcomes. Expected columns:
  ``fair_prob, entry_price, outcome`` (0..1, 0..1, 0/1). Optional: ``ticker,
  date, side, contracts, edge``.

Both return a :class:`~src.backtest.metrics.BacktestMetrics`.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from ..edge import evaluate_market
from ..models.contract_parser import EventCondition, parse_contract
from ..models.ensemble import build_model
from ..utils.config import Config
from ..utils.logging import get_logger
from ..weather.base import Forecast
from ..weather.stations import resolve_station
from .metrics import (
    BacktestMetrics,
    brier_score,
    calibration_table,
    log_loss,
    max_drawdown,
)

logger = get_logger("backtest")


@dataclass
class BacktestSample:
    """One scored prediction and (optionally) the trade taken on it."""

    fair_prob: float
    entry_price: float
    outcome: int                 # 1 if YES event occurred else 0
    traded: bool = False
    side: Optional[str] = None   # yes | no
    contracts: int = 0
    pnl: float = 0.0
    stake: float = 0.0
    edge: float = 0.0
    ticker: str = ""
    when: str = ""


def _condition_true(condition: EventCondition, value: Optional[float]) -> Optional[bool]:
    """Evaluate whether the YES event occurred given a realized ``value``."""
    if value is None or condition.threshold is None:
        return None
    t = condition.threshold
    op = condition.operator
    if op == "gte":
        return value >= t
    if op == "gt":
        return value > t
    if op == "lte":
        return value <= t
    if op == "lt":
        return value < t
    if op == "between" and condition.threshold2 is not None:
        lo, hi = sorted((t, condition.threshold2))
        return lo <= value <= hi
    return value >= t


def _observed_value(condition: EventCondition, obs: Forecast) -> Optional[float]:
    if condition.variable == "temp_range":
        if obs.high_temp_f is None or obs.low_temp_f is None:
            return None
        return obs.high_temp_f - obs.low_temp_f
    return obs.value_for(condition.variable)


def _aggregate(samples: list[BacktestSample]) -> BacktestMetrics:
    """Turn scored samples into a :class:`BacktestMetrics`."""
    metrics = BacktestMetrics()
    if not samples:
        return metrics

    probs = [s.fair_prob for s in samples]
    outcomes = [s.outcome for s in samples]
    metrics.n_predictions = len(samples)
    metrics.brier = brier_score(probs, outcomes)
    metrics.log_loss = log_loss(probs, outcomes)
    metrics.calibration = calibration_table(probs, outcomes, n_buckets=10)

    trades = [s for s in samples if s.traded]
    metrics.n_trades = len(trades)
    if trades:
        wins = sum(1 for s in trades if s.pnl > 0)
        metrics.win_rate = wins / len(trades)
        metrics.avg_edge = sum(s.edge for s in trades) / len(trades)
        metrics.total_pnl = sum(s.pnl for s in trades)
        metrics.total_stake = sum(s.stake for s in trades)
        metrics.realized_return = (
            metrics.total_pnl / metrics.total_stake if metrics.total_stake else float("nan")
        )
        equity, running = [], 0.0
        for s in trades:
            running += s.pnl
            equity.append(running)
        metrics.equity_curve = equity
        metrics.max_drawdown = max_drawdown(equity)
    return metrics


def run_synthetic_backtest(
    config: Config, days: int = 30, horizon_days: int = 1, fixed_contracts: int = 1
) -> BacktestMetrics:
    """Replay bundled mock markets over the past ``days`` days."""
    from ..utils.mock_data import MockWeatherSource, mock_markets

    model = build_model(config)
    weather = MockWeatherSource()
    threshold = float(config.get("paper_trading.signal_threshold_edge", 0.05))
    fee_rate = float(config.get("fees.trade_fee_rate", 0.07))
    slippage = int(config.get("fees.slippage_cents", 1))

    from .. import kalshi  # noqa: F401  (ensure package import side effects)
    from ..kalshi.filters import filter_weather_markets

    markets = filter_weather_markets(mock_markets(), config)
    samples: list[BacktestSample] = []
    today = date.today()

    for offset in range(1, days + 1):
        d = today - timedelta(days=offset)
        for market in markets:
            condition = parse_contract(market.title, rules=market.rules_primary)
            station = resolve_station(market.title)
            if station is None or condition.threshold is None:
                continue
            forecast = weather.get_forecast(station, d)
            obs = weather.get_observation(station, d)
            if forecast is None or obs is None:
                continue

            model_out = model.estimate(condition, forecast, horizon_days)
            realized = _observed_value(condition, obs)
            truth = _condition_true(condition, realized)
            if truth is None:
                continue
            yes_outcome = 1 if truth else 0

            opp = evaluate_market(
                market, model_out, ambiguous_settlement=condition.ambiguous,
                station_id=station.id, fee_rate=fee_rate, slippage_cents=slippage,
            )

            sample = BacktestSample(
                fair_prob=model_out.fair_yes,
                entry_price=opp.yes_ask_prob or 0.5,
                outcome=yes_outcome,
                ticker=market.ticker,
                when=d.isoformat(),
            )

            # Simulate a trade if there is a positive-EV edge over threshold.
            if (opp.best_side is not None and opp.ev_after_fees > 0
                    and abs(opp.gross_edge) >= threshold):
                side = opp.best_side
                entry = (opp.yes_ask_prob if side == "yes" else opp.no_ask_prob) or 0.5
                won = (yes_outcome == 1) if side == "yes" else (yes_outcome == 0)
                settle_value = 1.0 if won else 0.0
                fee = opp.fee_per_contract * fixed_contracts
                pnl = (settle_value - entry) * fixed_contracts - fee
                sample.traded = True
                sample.side = side
                sample.contracts = fixed_contracts
                sample.entry_price = entry
                sample.edge = opp.gross_edge
                sample.stake = entry * fixed_contracts
                sample.pnl = pnl

            samples.append(sample)

    metrics = _aggregate(samples)
    logger.info("Synthetic backtest: %d predictions, %d trades, PnL %.2f",
                metrics.n_predictions, metrics.n_trades, metrics.total_pnl)
    return metrics


def run_backtest(csv_path: str) -> BacktestMetrics:
    """Run a backtest from a CSV of pre-computed samples."""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Backtest CSV not found: {csv_path}")

    samples: list[BacktestSample] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            fair = float(row["fair_prob"])
            entry = float(row["entry_price"])
            outcome = int(float(row["outcome"]))
            side = row.get("side") or ("yes" if fair >= entry else "no")
            contracts = int(float(row.get("contracts", 1) or 1))
            won = (outcome == 1) if side == "yes" else (outcome == 0)
            settle = 1.0 if won else 0.0
            pnl = (settle - entry) * contracts
            edge = float(row.get("edge", abs(fair - entry)))
            traded = abs(fair - entry) > 0
            samples.append(BacktestSample(
                fair_prob=fair, entry_price=entry, outcome=outcome, traded=traded,
                side=side, contracts=contracts, pnl=pnl, stake=entry * contracts,
                edge=edge, ticker=row.get("ticker", ""), when=row.get("date", ""),
            ))

    metrics = _aggregate(samples)
    logger.info("CSV backtest: %d predictions, %d trades, PnL %.2f",
                metrics.n_predictions, metrics.n_trades, metrics.total_pnl)
    return metrics

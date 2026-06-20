"""Calibration and performance metrics for the probability model and strategy.

All functions take plain sequences so they are trivial to unit-test:
  * ``brier_score`` / ``log_loss`` measure probabilistic accuracy.
  * ``calibration_table`` buckets predictions and compares predicted vs realized.
  * ``max_drawdown`` measures the worst peak-to-trough of an equity curve.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

_EPS = 1e-9


def brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error between predicted probabilities and 0/1 outcomes."""
    if not probs:
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def log_loss(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Binary cross-entropy (clipped for numerical stability)."""
    if not probs:
        return float("nan")
    total = 0.0
    for p, o in zip(probs, outcomes):
        p = min(max(p, _EPS), 1 - _EPS)
        total += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return total / len(probs)


def calibration_table(
    probs: Sequence[float], outcomes: Sequence[int], n_buckets: int = 10
) -> list[dict]:
    """Group predictions into probability buckets and compare to realized rate.

    Returns one dict per non-empty bucket with ``bucket``, ``count``,
    ``avg_predicted`` and ``observed_freq``.
    """
    buckets: list[dict] = []
    for i in range(n_buckets):
        lo = i / n_buckets
        hi = (i + 1) / n_buckets
        ps, os_ = [], []
        for p, o in zip(probs, outcomes):
            if (lo <= p < hi) or (i == n_buckets - 1 and p == 1.0):
                ps.append(p)
                os_.append(o)
        if ps:
            buckets.append({
                "bucket": f"[{lo:.1f},{hi:.1f})",
                "count": len(ps),
                "avg_predicted": round(sum(ps) / len(ps), 4),
                "observed_freq": round(sum(os_) / len(os_), 4),
            })
    return buckets


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Largest peak-to-trough drop of an equity curve (absolute units)."""
    peak = -float("inf")
    mdd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        mdd = min(mdd, value - peak)
    return mdd


@dataclass
class BacktestMetrics:
    """Aggregate results of a backtest run."""

    n_trades: int = 0
    n_predictions: int = 0
    win_rate: float = float("nan")
    avg_edge: float = float("nan")
    realized_return: float = float("nan")     # total PnL / total stake
    total_pnl: float = 0.0
    total_stake: float = 0.0
    max_drawdown: float = 0.0
    brier: float = float("nan")
    log_loss: float = float("nan")
    calibration: list[dict] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    def summary_dict(self) -> dict:
        return {
            "n_predictions": self.n_predictions,
            "n_trades": self.n_trades,
            "win_rate": round(self.win_rate, 4) if self.win_rate == self.win_rate else None,
            "avg_edge": round(self.avg_edge, 4) if self.avg_edge == self.avg_edge else None,
            "realized_return": round(self.realized_return, 4)
            if self.realized_return == self.realized_return else None,
            "total_pnl": round(self.total_pnl, 2),
            "total_stake": round(self.total_stake, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "brier": round(self.brier, 4) if self.brier == self.brier else None,
            "log_loss": round(self.log_loss, 4) if self.log_loss == self.log_loss else None,
        }

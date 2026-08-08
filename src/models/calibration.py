"""Probability calibration wrapper.

The forecast models are systematically overconfident: measured against settled
Kalshi outcomes they predict ~16% where ~36% actually occur, and across settled
paper trades they have claimed ~86% and delivered ~74%. That error propagates
into every downstream number -- the claimed edge, the EV after fees, and which
signals clear the alert filter -- so it is corrected at the source.

Platt scaling in log-odds space::

    p_calibrated = sigmoid(a * logit(p_raw) + b)

``a < 1`` pulls estimates toward 50% (the cure for overconfidence); ``b``
corrects a directional lean. Parameters are fitted out-of-sample by
``scripts/fit_calibration.py`` -- refit them there rather than hand-tuning, and
leave ``enabled`` false if the fit does not beat the raw model on held-out data.

Expect calibration to REDUCE the number of signals. That is the point: it
removes edge that was an artifact of overstated confidence, not real.
"""

from __future__ import annotations

import math
from typing import Optional

from ..weather.base import Forecast
from .base import ModelOutput, ProbabilityModel
from .contract_parser import EventCondition

EPS = 1e-6


def _logit(p: float) -> float:
    p = min(max(p, EPS), 1.0 - EPS)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def calibrate(p: float, a: float, b: float) -> float:
    """Map a raw probability through the fitted Platt curve."""
    return _sigmoid(a * _logit(p) + b)


class CalibratedModel(ProbabilityModel):
    """Wrap any model and calibrate its fair YES probability."""

    def __init__(self, inner: ProbabilityModel, a: float, b: float) -> None:
        self.inner = inner
        self.a = float(a)
        self.b = float(b)
        self.name = f"calibrated({getattr(inner, 'name', 'model')})"

    def estimate(
        self,
        condition: EventCondition,
        forecast: Optional[Forecast],
        horizon_days: int,
    ) -> ModelOutput:
        out = self.inner.estimate(condition, forecast, horizon_days)
        # A zero-confidence estimate carries no information to calibrate.
        if out.confidence <= 0.0:
            return out
        raw = out.fair_yes
        adj = calibrate(raw, self.a, self.b)
        notes = list(out.notes)
        notes.append(f"calibrated {raw:.3f} -> {adj:.3f} "
                     f"(a={self.a:.2f}, b={self.b:+.2f})")
        return ModelOutput(
            fair_yes=adj,
            confidence=out.confidence,
            model=self.name,
            notes=notes,
            point_forecast=out.point_forecast,
            sigma=out.sigma,
        )

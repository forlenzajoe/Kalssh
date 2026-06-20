"""Transparent normal-distribution fair-probability model (the baseline).

Methodology (intentionally simple and auditable):

1. **Point forecast** ``mu`` for the contract variable (high temp, low temp,
   precip, snow, wind, or temp range) from a :class:`Forecast`.
2. **Forecast uncertainty** ``sigma`` from a horizon-indexed error table — i.e.
   the historical 1-sigma forecast error at that lead time. Longer horizons get
   wider distributions.
3. **Probability curve around the threshold**: model the realized value as
   ``Normal(mu, sigma)`` and integrate the relevant tail/interval to get the
   fair YES probability. This yields a smooth probability that respects how
   close the forecast sits to the strike and how uncertain it is.

Limitations (see README): precipitation/snow are non-negative and skewed, so the
normal approximation is crude — confidence is reduced accordingly and these are
prime candidates for the pluggable ML layer.
"""

from __future__ import annotations

import math
from typing import Optional

from ..weather.base import Forecast
from .base import ModelOutput, ProbabilityModel
from .contract_parser import EventCondition

try:
    from scipy.stats import norm

    def _cdf(z: float) -> float:
        return float(norm.cdf(z))
except Exception:  # pragma: no cover - scipy optional fallback
    def _cdf(z: float) -> float:
        """Standard-normal CDF via erf (no scipy)."""
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# Default horizon -> 1-sigma temp error (°F), overridable via config.
_DEFAULT_TEMP_SIGMA = {0: 2.5, 1: 3.0, 2: 4.0, 3: 5.0, 4: 6.0, 5: 7.0, 6: 8.0, 7: 9.0}


class NormalForecastModel(ProbabilityModel):
    """Normal-distribution model around a point forecast."""

    name = "normal_forecast"

    def __init__(
        self,
        temp_sigma_by_horizon: Optional[dict[int, float]] = None,
        long_horizon_days: int = 7,
        long_horizon_penalty: float = 0.6,
        ambiguous_penalty: float = 0.4,
        bias_correction_f: float = 0.0,
        ensemble_sigma_a: Optional[float] = None,
        ensemble_sigma_b: Optional[float] = None,
    ) -> None:
        self.temp_sigma = {int(k): float(v) for k, v in
                           (temp_sigma_by_horizon or _DEFAULT_TEMP_SIGMA).items()}
        self.long_horizon_days = long_horizon_days
        self.long_horizon_penalty = long_horizon_penalty
        self.ambiguous_penalty = ambiguous_penalty
        # Additive correction (°F) applied to temperature point forecasts. Fit
        # empirically against settled outcomes (see scripts/recalibrate_sigma.py).
        self.bias_correction_f = float(bias_correction_f)
        # Adaptive per-day sigma from cross-model spread: sigma = a + b*spread.
        # Fit out-of-sample (scripts/ensemble_validation.py). None -> use static.
        self.ensemble_sigma_a = ensemble_sigma_a
        self.ensemble_sigma_b = ensemble_sigma_b

    # -- uncertainty ----------------------------------------------------------
    def _temp_sigma_for(self, horizon: int) -> float:
        if not self.temp_sigma:
            return 5.0
        horizon = max(0, horizon)
        if horizon in self.temp_sigma:
            return self.temp_sigma[horizon]
        max_h = max(self.temp_sigma)
        return self.temp_sigma[max_h] if horizon > max_h else self.temp_sigma[min(self.temp_sigma)]

    def _sigma_for(self, condition: EventCondition, point: float, horizon: int) -> float:
        """Return the 1-sigma uncertainty for the contract variable."""
        var = condition.variable
        if var in {"high_temp", "low_temp"}:
            return self._temp_sigma_for(horizon)
        if var == "temp_range":
            # Difference of two roughly independent temps.
            return self._temp_sigma_for(horizon) * math.sqrt(2.0)
        if var in {"rainfall", "snowfall"}:
            # Crude: scale with magnitude, floor to avoid zero-width.
            return max(0.35 * abs(point), 0.1)
        if var == "wind":
            return max(0.25 * abs(point), 2.0)
        return max(0.2 * abs(point), 1.0)

    # -- main -----------------------------------------------------------------
    def _point_value(self, condition: EventCondition, forecast: Forecast) -> Optional[float]:
        if condition.variable == "temp_range":
            if forecast.high_temp_f is None or forecast.low_temp_f is None:
                return None
            return forecast.high_temp_f - forecast.low_temp_f
        return forecast.value_for(condition.variable)

    def estimate(
        self,
        condition: EventCondition,
        forecast: Optional[Forecast],
        horizon_days: int,
    ) -> ModelOutput:
        notes: list[str] = []

        if forecast is None or condition.threshold is None:
            reason = "no forecast available" if forecast is None else "no threshold parsed"
            return ModelOutput(
                fair_yes=0.5, confidence=0.0, model=self.name,
                notes=[f"Cannot estimate ({reason}); confidence 0."],
            )

        point = self._point_value(condition, forecast)
        if point is None:
            return ModelOutput(
                fair_yes=0.5, confidence=0.0, model=self.name,
                notes=[f"Forecast missing variable {condition.variable!r}; confidence 0."],
            )
        # Apply empirical bias correction to temperature forecasts.
        if self.bias_correction_f and condition.variable in {"high_temp", "low_temp"}:
            point = point + self.bias_correction_f

        sigma = self._sigma_for(condition, point, horizon_days)
        # Adaptive per-day uncertainty from cross-model spread, when available.
        if (self.ensemble_sigma_a is not None and self.ensemble_sigma_b is not None
                and forecast.spread_f is not None
                and condition.variable in {"high_temp", "low_temp"}):
            sigma = self.ensemble_sigma_a + self.ensemble_sigma_b * forecast.spread_f
            notes.append(f"ensemble sigma={sigma:.2f} (spread {forecast.spread_f:.2f}).")
        sigma = max(sigma, 1e-6)
        thr = condition.threshold

        # z for P(X <= threshold)
        z = (thr - point) / sigma
        p_below = _cdf(z)
        p_at_or_above = 1.0 - p_below

        if condition.operator in {"gte", "gt"}:
            fair_yes = p_at_or_above
        elif condition.operator in {"lte", "lt"}:
            fair_yes = p_below
        elif condition.operator == "between" and condition.threshold2 is not None:
            lo, hi = sorted((thr, condition.threshold2))
            fair_yes = _cdf((hi - point) / sigma) - _cdf((lo - point) / sigma)
        else:
            fair_yes = p_at_or_above
            notes.append("Unrecognized operator; assumed >=.")

        notes.append(
            f"point={point:.2f}{condition.unit}, sigma={sigma:.2f}, "
            f"threshold={thr}{condition.unit}, op={condition.operator}"
        )

        # -- confidence -------------------------------------------------------
        confidence = 1.0
        if horizon_days > self.long_horizon_days:
            confidence *= self.long_horizon_penalty
            notes.append(f"Long horizon ({horizon_days}d) penalty applied.")
        if condition.ambiguous:
            confidence *= self.ambiguous_penalty
            notes.append("Ambiguous parse penalty applied.")
        if condition.variable in {"rainfall", "snowfall"}:
            confidence *= 0.7
            notes.append("Skewed precip variable: normal approximation is crude.")

        return ModelOutput(
            fair_yes=fair_yes,
            confidence=confidence,
            model=self.name,
            notes=notes,
            point_forecast=point,
            sigma=sigma,
        )

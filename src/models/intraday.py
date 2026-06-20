"""Intraday-conditioned temperature model — the project's real edge candidate.

Standard models price a daily high/low temp contract off a *morning point
forecast*. But the daily high is realized in the mid-afternoon and the daily low
near dawn, so as the clock advances the outcome becomes partly (then fully)
determined:

* The final daily high can never be **below** the maximum temperature already
  observed today. Once you have observed 81°F, "high ≥ 80°F" is settled YES.
* After the afternoon peak, temperatures fall, so the final high is essentially
  the max-so-far — remaining uncertainty collapses to near zero.

Slow market participants keep pricing off the stale morning forecast. By
conditioning on *observations-so-far* we get a sharper, better-calibrated
probability than the crowd. That difference — not a better physics model — is
where any genuine edge lives.

This model still falls back to the plain normal forecast model when intraday
observations are unavailable (e.g. multi-day horizons) or for non-temperature
variables, so it is always safe to select.
"""

from __future__ import annotations

import math
from typing import Optional

from ..weather.base import Forecast
from .base import ModelOutput, ProbabilityModel
from .contract_parser import EventCondition
from .forecast_model import NormalForecastModel, _cdf

# Typical local solar hours for the diurnal extremes.
PEAK_HOUR = 15.0      # daily high ~3pm local
SUNRISE_HOUR = 6.0    # heating begins ~6am; low occurs near here
TROUGH_HOUR = 5.5     # daily low ~dawn


class IntradayTemperatureModel(ProbabilityModel):
    """Bayesian-style update of the daily high/low using observations-so-far."""

    name = "intraday_temp"

    def __init__(self, baseline: Optional[NormalForecastModel] = None) -> None:
        self.baseline = baseline or NormalForecastModel()

    # -- high temperature -----------------------------------------------------
    def _high_distribution(
        self, forecast: Forecast, base_sigma: float
    ) -> tuple[float, float, bool]:
        """Return (mu, sigma, locked) for the *final* daily high.

        ``locked`` is True when the high is essentially determined (past peak),
        which we surface as high confidence.
        """
        observed = forecast.observed_high_so_far
        hour = forecast.local_hour
        point = forecast.high_temp_f if forecast.high_temp_f is not None else observed

        if observed is None or hour is None:
            # No intraday info: behave like the point-forecast model.
            return (point if point is not None else 0.0), base_sigma, False

        if hour >= PEAK_HOUR:
            # Past the afternoon peak: future temps are falling, so the final
            # high is the max already seen. Tiny residual for late anomalies.
            mu = observed
            sigma = max(0.5, base_sigma * 0.12)
            return mu, sigma, True

        # Before the peak: warming is still ahead. The expected peak is at least
        # what we've seen, and at least the forecast high. Remaining uncertainty
        # shrinks as we approach the peak hour.
        span = max(PEAK_HOUR - SUNRISE_HOUR, 1e-6)
        frac_remaining = min(max((PEAK_HOUR - hour) / span, 0.0), 1.0)
        sigma = base_sigma * (0.25 + 0.75 * frac_remaining)
        mu = max(observed, point if point is not None else observed)
        return mu, sigma, False

    # -- low temperature ------------------------------------------------------
    def _low_distribution(
        self, forecast: Forecast, base_sigma: float
    ) -> tuple[float, float, bool]:
        observed = forecast.observed_low_so_far
        hour = forecast.local_hour
        point = forecast.low_temp_f if forecast.low_temp_f is not None else observed

        if observed is None or hour is None:
            return (point if point is not None else 0.0), base_sigma, False

        # The daily low occurs near dawn. Past mid-morning, the low is set: the
        # final low is the min already seen (it only rises afterward).
        if hour >= SUNRISE_HOUR + 1.0:
            mu = observed
            sigma = max(0.5, base_sigma * 0.12)
            return mu, sigma, True

        # Overnight, before dawn: more cooling may still occur.
        span = max(TROUGH_HOUR + 24.0 - 18.0, 1e-6)  # evening->dawn window
        # crude: shrink uncertainty as we approach dawn
        frac_remaining = 0.6
        sigma = base_sigma * (0.3 + 0.7 * frac_remaining)
        mu = min(observed, point if point is not None else observed)
        return mu, sigma, False

    # -- main -----------------------------------------------------------------
    def estimate(
        self,
        condition: EventCondition,
        forecast: Optional[Forecast],
        horizon_days: int,
    ) -> ModelOutput:
        # Only same-day temperature markets benefit; otherwise delegate.
        if (forecast is None or condition.threshold is None
                or condition.variable not in {"high_temp", "low_temp"}
                or horizon_days > 0
                or forecast.local_hour is None):
            out = self.baseline.estimate(condition, forecast, horizon_days)
            if forecast is not None and forecast.local_hour is None:
                out.notes.append("intraday: no observations available; used baseline.")
            return out

        base_sigma = self.baseline._sigma_for(condition, forecast.high_temp_f or 0.0, 0)

        if condition.variable == "high_temp":
            observed = forecast.observed_high_so_far
            mu, sigma, locked = self._high_distribution(forecast, base_sigma)
            # The final high can't fall below what's already observed.
            already = observed is not None and observed >= condition.threshold
        else:
            observed = forecast.observed_low_so_far
            mu, sigma, locked = self._low_distribution(forecast, base_sigma)
            already = False  # handled via direction below

        sigma = max(sigma, 1e-6)
        thr = condition.threshold
        z = (thr - mu) / sigma
        p_below = _cdf(z)
        p_at_or_above = 1.0 - p_below

        if condition.operator in {"gte", "gt"}:
            fair_yes = p_at_or_above
            if condition.variable == "high_temp" and already:
                fair_yes = max(fair_yes, 0.995)   # already crossed; can't undo
        elif condition.operator in {"lte", "lt"}:
            fair_yes = p_below
            if condition.variable == "high_temp" and already:
                fair_yes = min(fair_yes, 0.005)   # high already above the cap
        elif condition.operator == "between" and condition.threshold2 is not None:
            lo, hi = sorted((thr, condition.threshold2))
            fair_yes = _cdf((hi - mu) / sigma) - _cdf((lo - mu) / sigma)
        else:
            fair_yes = p_at_or_above

        # Confidence: conditioning on observations makes us sharper, and a locked
        # outcome is near-certain. This is exactly where we beat a stale quote.
        if locked:
            confidence = 0.95
        else:
            span = max(PEAK_HOUR - SUNRISE_HOUR, 1e-6)
            hour = forecast.local_hour
            progressed = min(max((hour - SUNRISE_HOUR) / span, 0.0), 1.0)
            confidence = min(0.9, 0.55 + 0.4 * progressed)

        notes = [
            f"intraday: observed_so_far={observed}, local_hour={forecast.local_hour}, "
            f"mu={mu:.2f}, sigma={sigma:.2f}, locked={locked}",
        ]
        if condition.ambiguous:
            confidence *= self.baseline.ambiguous_penalty
            notes.append("Ambiguous parse penalty applied.")

        return ModelOutput(
            fair_yes=fair_yes, confidence=confidence, model=self.name,
            notes=notes, point_forecast=mu, sigma=sigma,
        )

"""Tests for the normal-forecast probability model and metrics."""

import math
from datetime import date

from src.backtest.metrics import brier_score, calibration_table, log_loss, max_drawdown
from src.models.contract_parser import parse_contract
from src.models.forecast_model import NormalForecastModel
from src.weather.base import Forecast


def _forecast(high=82.0, low=66.0, precip=0.2, snow=0.0):
    return Forecast(location_id="nyc", target_date=date(2026, 6, 18), source="mock",
                    horizon_days=0, high_temp_f=high, low_temp_f=low,
                    precip_in=precip, snow_in=snow)


def test_high_temp_probability_above_point_is_high():
    model = NormalForecastModel()
    cond = parse_contract("high temperature above 80F")
    out = model.estimate(cond, _forecast(high=82), horizon_days=0)
    # Forecast 82 vs threshold 80 -> P(>=80) should exceed 0.5.
    assert out.fair_yes > 0.5
    assert 0.0 <= out.fair_yes <= 1.0
    assert out.confidence > 0


def test_symmetry_at_threshold():
    model = NormalForecastModel()
    cond = parse_contract("high temperature above 80F")
    out = model.estimate(cond, _forecast(high=80), horizon_days=0)
    assert math.isclose(out.fair_yes, 0.5, abs_tol=1e-6)


def test_below_operator_is_complement():
    model = NormalForecastModel()
    above = model.estimate(parse_contract("high temperature above 80F"),
                           _forecast(high=82), 0)
    below = model.estimate(parse_contract("high temperature below 80F"),
                           _forecast(high=82), 0)
    assert math.isclose(above.fair_yes + below.fair_yes, 1.0, abs_tol=1e-6)


def test_longer_horizon_widens_and_pulls_to_half():
    model = NormalForecastModel(temp_sigma_by_horizon={0: 2.5, 7: 9.0})
    cond = parse_contract("high temperature above 80F")
    near = model.estimate(cond, _forecast(high=84), 0).fair_yes
    far = model.estimate(cond, _forecast(high=84), 7).fair_yes
    # Both above 0.5 but the far one is closer to 0.5 (more uncertainty).
    assert near > far > 0.5


def test_missing_forecast_gives_zero_confidence():
    model = NormalForecastModel()
    out = model.estimate(parse_contract("high temperature above 80F"), None, 0)
    assert out.confidence == 0.0


def test_precip_variable_has_reduced_confidence():
    model = NormalForecastModel()
    cond = parse_contract("rain more than 0.1 inches")
    out = model.estimate(cond, _forecast(precip=0.3), 0)
    assert out.confidence < 1.0


def test_metrics_brier_and_logloss():
    probs = [0.9, 0.1, 0.8, 0.2]
    outs = [1, 0, 1, 0]
    assert brier_score(probs, outs) < 0.05
    assert log_loss(probs, outs) > 0


def test_calibration_table_buckets():
    probs = [0.05, 0.15, 0.95]
    outs = [0, 0, 1]
    table = calibration_table(probs, outs, n_buckets=10)
    assert sum(b["count"] for b in table) == 3


def test_max_drawdown():
    assert max_drawdown([0, 5, 3, 8, 2]) == -6

"""Tests for the intraday-conditioned temperature model (the edge candidate)."""

from datetime import date

from src.models.contract_parser import parse_contract
from src.models.intraday import IntradayTemperatureModel
from src.weather.base import Forecast


def _fc(high=82.0, low=66.0, observed_high=None, observed_low=None, hour=None):
    return Forecast(
        location_id="nyc", target_date=date(2026, 6, 18), source="mock", horizon_days=0,
        high_temp_f=high, low_temp_f=low,
        observed_high_so_far=observed_high, observed_low_so_far=observed_low,
        local_hour=hour,
    )


def test_already_crossed_is_near_certain():
    # 4pm and we've already hit 81F -> "high >= 80" is essentially settled YES.
    m = IntradayTemperatureModel()
    cond = parse_contract("high temperature above 80F")
    out = m.estimate(cond, _fc(high=82, observed_high=81.0, hour=16.0), 0)
    assert out.fair_yes > 0.98
    assert out.confidence >= 0.9          # locked outcome -> high confidence


def test_past_peak_not_yet_reached_is_unlikely():
    # 5pm, only reached 78F, threshold 80 -> high already locked below; unlikely.
    m = IntradayTemperatureModel()
    cond = parse_contract("high temperature above 80F")
    out = m.estimate(cond, _fc(high=79, observed_high=78.0, hour=17.0), 0)
    assert out.fair_yes < 0.2
    assert out.confidence >= 0.9


def test_morning_is_less_certain_than_afternoon():
    m = IntradayTemperatureModel()
    cond = parse_contract("high temperature above 80F")
    morning = m.estimate(cond, _fc(high=82, observed_high=70.0, hour=8.0), 0)
    afternoon = m.estimate(cond, _fc(high=82, observed_high=81.0, hour=16.0), 0)
    # Confidence rises through the day as the outcome gets determined.
    assert afternoon.confidence > morning.confidence


def test_falls_back_to_baseline_without_observations():
    m = IntradayTemperatureModel()
    cond = parse_contract("high temperature above 80F")
    out = m.estimate(cond, _fc(high=82, observed_high=None, hour=None), 0)
    # Should match the plain forecast model's estimate (no intraday edge).
    base = m.baseline.estimate(cond, _fc(high=82), 0)
    assert abs(out.fair_yes - base.fair_yes) < 1e-9


def test_multiday_horizon_uses_baseline():
    m = IntradayTemperatureModel()
    cond = parse_contract("high temperature above 80F")
    out = m.estimate(cond, _fc(high=82, observed_high=70, hour=10), horizon_days=3)
    assert out.model == "normal_forecast"   # delegated


def test_non_temperature_delegates():
    m = IntradayTemperatureModel()
    cond = parse_contract("rain more than 0.1 inches")
    fc = _fc()
    fc.precip_in = 0.3
    out = m.estimate(cond, fc, 0)
    assert out.model == "normal_forecast"

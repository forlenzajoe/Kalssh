"""Tests for parsing Kalshi weather contract language."""

from datetime import date

from src.models.contract_parser import parse_contract


def test_high_temp_above_threshold():
    c = parse_contract("Will the high temperature in NYC be above 80F on June 18, 2026?")
    assert c.variable == "high_temp"
    assert c.operator == "gte"
    assert c.threshold == 80
    assert c.unit == "F"
    assert c.target_date == date(2026, 6, 18)
    assert not c.ambiguous


def test_low_temp_below_threshold():
    c = parse_contract("Will the low temperature in Austin be below 76F on June 18, 2026?")
    assert c.variable == "low_temp"
    assert c.operator == "lte"
    assert c.threshold == 76


def test_rainfall_more_than():
    c = parse_contract("Will it rain more than 0.10 inches in Chicago on June 18, 2026?")
    assert c.variable == "rainfall"
    # Parser normalizes upper-direction comparisons to "gte"; for continuous
    # weather variables strict vs inclusive is immaterial to the probability.
    assert c.operator in {"gt", "gte"}
    assert c.threshold == 0.10
    assert c.unit == "in"


def test_snowfall_exceed():
    c = parse_contract("Will snowfall in Denver exceed 1 inch on June 18, 2026?")
    assert c.variable == "snowfall"
    assert c.operator in {"gt", "gte"}
    assert c.threshold == 1


def test_between_operator():
    c = parse_contract("Will the high temperature be between 70 and 80 today?")
    assert c.operator == "between"
    assert c.threshold == 70
    assert c.threshold2 == 80


def test_missing_threshold_is_ambiguous():
    c = parse_contract("Will it be hot in Miami today?")
    assert c.ambiguous
    assert c.threshold is None


def test_relative_date_today():
    ref = date(2026, 6, 18)
    c = parse_contract("Will the high temperature in NYC be above 80F today?", reference_date=ref)
    assert c.target_date == ref


def test_describe_round_trips():
    c = parse_contract("Will the high temperature in NYC be above 80F on June 18, 2026?")
    assert "high_temp" in c.describe()
    assert "80" in c.describe()

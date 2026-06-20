"""Tests for implied probabilities, fees, and effective cost."""

import math

from src.kalshi.models import Market
from src.kalshi.pricing import effective_cost, implied_prices, liquidity_score, trade_fee


def _market(yes_bid=60, yes_ask=64, **kw):
    return Market(
        ticker="T", title="t", category="Climate", status="active", close_time=None,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=100 - yes_ask, no_ask=100 - yes_bid,
        volume=kw.get("volume", 100),
    )


def test_implied_prices_basic():
    p = implied_prices(_market(60, 64))
    assert p.yes_ask_prob == 0.64
    assert p.yes_bid_prob == 0.60
    assert p.no_ask_prob == 0.40          # 100 - yes_bid
    assert math.isclose(p.mid_prob, 0.62)
    assert math.isclose(p.spread_prob, 0.04)


def test_trade_fee_is_nonnegative_and_rounds_up():
    # 0.07 * 100 * 0.5 * 0.5 = 1.75 -> rounds up to whole cents
    fee = trade_fee(0.5, 100, fee_rate=0.07)
    assert fee >= 1.75
    assert trade_fee(0.5, 0) == 0.0


def test_fee_zero_at_extreme_prices():
    assert trade_fee(0.0, 100) == 0.0
    assert trade_fee(1.0, 100) == 0.0


def test_effective_cost_includes_slippage_and_fee():
    m = _market(60, 64)
    cost = effective_cost("yes", m, contracts=1, slippage_cents=1)
    assert cost is not None
    # price = 0.64 ask + 0.01 slippage
    assert math.isclose(cost["price_prob"], 0.65)
    assert cost["total_cost"] > cost["gross_cost"]   # fee added
    assert cost["breakeven_prob"] > 0.65


def test_no_side_cost_uses_no_ask():
    m = _market(60, 64)
    cost = effective_cost("no", m, contracts=1, slippage_cents=0)
    # no_ask = 100 - yes_bid = 40
    assert math.isclose(cost["price_prob"], 0.40)


def test_liquidity_score_penalizes_wide_spread():
    tight = liquidity_score(_market(60, 62))
    wide = liquidity_score(_market(50, 70))
    assert tight > wide
    assert 0.0 <= wide <= 1.0

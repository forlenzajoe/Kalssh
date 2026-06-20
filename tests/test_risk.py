"""Tests for risk gating and position sizing."""

from src.edge import evaluate_market
from src.kalshi.models import Market, OrderBook, OrderBookLevel
from src.models.base import ModelOutput
from src.risk import RiskParams, apply_risk, enforce_daily_exposure


def _market(yes_bid=60, yes_ask=64, depth=120, volume=500):
    book = OrderBook(
        "T",
        yes_bids=[OrderBookLevel(yes_bid, depth)],
        yes_asks=[OrderBookLevel(yes_ask, depth)],
        no_bids=[OrderBookLevel(100 - yes_ask, depth)],
        no_asks=[OrderBookLevel(100 - yes_bid, depth)],
    )
    return Market(
        ticker="T", title="t", category="Climate", status="active", close_time=None,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=100 - yes_ask, no_ask=100 - yes_bid,
        volume=volume, order_book=book,
    )


def _opp(fair=0.80, conf=0.9, ambiguous=False, market=None):
    market = market or _market()
    out = ModelOutput(fair_yes=fair, confidence=conf, model="t")
    return evaluate_market(market, out, ambiguous_settlement=ambiguous, station_id="nyc")


def test_strong_signal_becomes_buy():
    params = RiskParams()
    opp = apply_risk(_opp(fair=0.80), params, bankroll=1000.0)
    assert opp.action == "Buy YES"
    assert opp.suggested_contracts > 0
    assert opp.max_position_usd <= params.max_position_per_market_usd


def test_ambiguous_settlement_is_avoided():
    params = RiskParams()
    opp = apply_risk(_opp(fair=0.80, ambiguous=True), params, bankroll=1000.0)
    assert opp.action == "Avoid"


def test_low_confidence_downgraded_to_watch():
    params = RiskParams()
    opp = apply_risk(_opp(fair=0.80, conf=0.2), params, bankroll=1000.0)
    assert opp.action == "Watch"


def test_wide_spread_downgraded_to_watch():
    params = RiskParams()
    wide = _market(50, 70)               # 20c spread > 8c max
    opp = apply_risk(_opp(fair=0.85, market=wide), params, bankroll=1000.0)
    assert opp.action == "Watch"


def test_negative_ev_is_avoided():
    params = RiskParams()
    # Fair == mid (0.62): YES ask (0.65) and NO ask (0.40 vs fair_no 0.38) are
    # both negative-EV after fees+slippage, so the market should be avoided.
    opp = apply_risk(_opp(fair=0.62), params, bankroll=1000.0)
    assert opp.action == "Avoid"


def test_daily_exposure_cap_downgrades_extras():
    params = RiskParams(max_daily_exposure_usd=10.0, max_position_per_market_usd=8.0)
    opps = [apply_risk(_opp(fair=0.85), params, 1000.0) for _ in range(5)]
    for i, o in enumerate(opps):
        o.ticker = f"T{i}"
    enforce_daily_exposure(opps, params)
    buys = [o for o in opps if o.action.startswith("Buy")]
    assert sum(o.max_position_usd for o in buys) <= params.max_daily_exposure_usd

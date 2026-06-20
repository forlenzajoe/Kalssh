"""Tests for edge / expected-value calculation."""

from src.edge import evaluate_market
from src.kalshi.models import Market, OrderBook, OrderBookLevel
from src.models.base import ModelOutput


def _market(yes_bid=60, yes_ask=64, depth=120):
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
        volume=500, order_book=book,
    )


def test_positive_edge_buy_yes():
    # Fair 0.80 vs YES ask 0.64 -> strong YES edge.
    m = _market(60, 64)
    out = ModelOutput(fair_yes=0.80, confidence=0.9, model="t")
    opp = evaluate_market(m, out, ambiguous_settlement=False, station_id="nyc")
    assert opp.best_side == "yes"
    assert opp.gross_edge > 0
    assert opp.ev_after_fees > 0


def test_positive_edge_buy_no():
    # Fair 0.20 -> NO is the value side.
    m = _market(60, 64)
    out = ModelOutput(fair_yes=0.20, confidence=0.9, model="t")
    opp = evaluate_market(m, out, ambiguous_settlement=False, station_id="mia")
    assert opp.best_side == "no"
    assert opp.ev_after_fees > 0


def test_no_edge_when_fair_equals_price():
    m = _market(60, 64)
    out = ModelOutput(fair_yes=0.62, confidence=0.9, model="t")
    opp = evaluate_market(m, out, ambiguous_settlement=False, station_id="nyc")
    # Around mid; neither side should be strongly +EV after fees+slippage.
    assert opp.ev_after_fees <= 0.05


def test_fee_makes_marginal_edge_negative():
    # Fair only marginally above ask; fees+slippage should erode EV.
    m = _market(60, 64)
    out = ModelOutput(fair_yes=0.655, confidence=0.9, model="t")
    opp = evaluate_market(m, out, ambiguous_settlement=False, station_id="nyc")
    assert opp.ev_after_fees < opp.gross_edge

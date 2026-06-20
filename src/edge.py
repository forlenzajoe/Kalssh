"""Edge and expected-value calculation for a single market.

Combines executable Kalshi prices, model fair probability, fees, slippage, and
liquidity into an :class:`Opportunity`. Risk gating and position sizing are
applied separately in :mod:`src.risk` so the economics stay pure and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .kalshi.models import Market
from .kalshi.pricing import ImpliedPrices, effective_cost, implied_prices, liquidity_score
from .models.base import ModelOutput


@dataclass
class Opportunity:
    """A fully-evaluated market opportunity (pre risk-gate)."""

    ticker: str
    title: str
    category: str

    # Executable Kalshi prices (cents) and implied probabilities.
    yes_bid: Optional[int]
    yes_ask: Optional[int]
    yes_ask_prob: Optional[float]
    yes_bid_prob: Optional[float]
    no_ask_prob: Optional[float]
    implied_prob_mid: Optional[float]
    spread_cents: Optional[int]
    spread_cost_prob: Optional[float]

    # Model.
    fair_yes: float
    fair_no: float
    confidence: float
    model_notes: list[str]

    # Economics (best actionable side).
    best_side: Optional[str]            # "yes" | "no" | None
    gross_edge: float                   # fair - executable implied on best side
    ev_after_fees: float                # $ per contract after fees+slippage
    fee_per_contract: float
    breakeven_prob: Optional[float]

    # Microstructure / risk inputs.
    liquidity: float
    volume: int
    hours_to_close: Optional[float]
    ambiguous_settlement: bool
    station_id: Optional[str]
    settlement_notes: list[str] = field(default_factory=list)

    # Filled in by risk layer.
    action: str = "Watch"               # Buy YES | Buy NO | Watch | Avoid
    max_position_usd: float = 0.0
    suggested_contracts: int = 0
    risk_notes: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        """Flatten to a dict suitable for a DataFrame / dashboard table."""
        return {
            "ticker": self.ticker,
            "title": self.title,
            "category": self.category,
            "yes_bid": self.yes_bid,
            "yes_ask": self.yes_ask,
            "implied_yes_ask": round(self.yes_ask_prob, 4) if self.yes_ask_prob is not None else None,
            "implied_mid": round(self.implied_prob_mid, 4) if self.implied_prob_mid is not None else None,
            "fair_yes": round(self.fair_yes, 4),
            "fair_no": round(self.fair_no, 4),
            "confidence": round(self.confidence, 3),
            "best_side": self.best_side,
            "gross_edge": round(self.gross_edge, 4),
            "ev_after_fees": round(self.ev_after_fees, 4),
            "spread_cents": self.spread_cents,
            "liquidity": self.liquidity,
            "hours_to_close": round(self.hours_to_close, 1) if self.hours_to_close is not None else None,
            "ambiguous": self.ambiguous_settlement,
            "action": self.action,
            "max_position_usd": self.max_position_usd,
            "suggested_contracts": self.suggested_contracts,
        }


def _side_economics(side: str, market: Market, fair_prob: float, *, fee_rate: float,
                    slippage_cents: int, assume_taker: bool) -> Optional[dict]:
    """Edge + EV per contract for taking ``side`` at the executable price."""
    cost = effective_cost(
        side, market, contracts=1, fee_rate=fee_rate,
        slippage_cents=slippage_cents, assume_taker=assume_taker,
    )
    if cost is None:
        return None
    # Expected value per contract: win prob * $1 payout - total cost (incl fee).
    ev = fair_prob - cost["total_cost"]
    gross_edge = fair_prob - cost["price_prob"]
    return {
        "side": side,
        "ev": ev,
        "gross_edge": gross_edge,
        "fee": cost["fee"],
        "price_prob": cost["price_prob"],
        "breakeven_prob": cost["breakeven_prob"],
    }


def evaluate_market(
    market: Market,
    model_out: ModelOutput,
    *,
    ambiguous_settlement: bool,
    station_id: Optional[str],
    settlement_notes: Optional[list[str]] = None,
    fee_rate: float = 0.07,
    slippage_cents: int = 1,
    assume_taker: bool = True,
    max_spread_cents: int = 20,
) -> Opportunity:
    """Build an :class:`Opportunity` from a market and a model estimate."""
    prices: ImpliedPrices = implied_prices(market)
    liq = liquidity_score(market, max_spread_cents=max_spread_cents)

    yes_econ = _side_economics(
        "yes", market, model_out.fair_yes, fee_rate=fee_rate,
        slippage_cents=slippage_cents, assume_taker=assume_taker,
    )
    no_econ = _side_economics(
        "no", market, model_out.fair_no, fee_rate=fee_rate,
        slippage_cents=slippage_cents, assume_taker=assume_taker,
    )

    candidates = [c for c in (yes_econ, no_econ) if c is not None]
    if candidates:
        best = max(candidates, key=lambda c: c["ev"])
        best_side = best["side"]
        gross_edge = best["gross_edge"]
        ev = best["ev"]
        fee = best["fee"]
        breakeven = best["breakeven_prob"]
    else:
        best_side, gross_edge, ev, fee, breakeven = None, 0.0, 0.0, 0.0, None

    return Opportunity(
        ticker=market.ticker,
        title=market.title,
        category=market.category,
        yes_bid=market.yes_bid,
        yes_ask=market.yes_ask,
        yes_ask_prob=prices.yes_ask_prob,
        yes_bid_prob=prices.yes_bid_prob,
        no_ask_prob=prices.no_ask_prob,
        implied_prob_mid=prices.mid_prob,
        spread_cents=market.spread_cents,
        spread_cost_prob=prices.spread_prob,
        fair_yes=model_out.fair_yes,
        fair_no=model_out.fair_no,
        confidence=model_out.confidence,
        model_notes=list(model_out.notes),
        best_side=best_side,
        gross_edge=gross_edge,
        ev_after_fees=ev,
        fee_per_contract=fee,
        breakeven_prob=breakeven,
        liquidity=liq,
        volume=market.volume,
        hours_to_close=market.hours_to_close(),
        ambiguous_settlement=ambiguous_settlement,
        station_id=station_id,
        settlement_notes=settlement_notes or [],
    )

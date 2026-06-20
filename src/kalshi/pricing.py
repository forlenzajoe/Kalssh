"""Implied probabilities, fees, and slippage for Kalshi markets.

All probabilities are expressed in dollars (0..1). Kalshi quotes in cents, so a
``yes_ask`` of 60 means it costs $0.60 to buy a YES contract that pays $1.00.

Key idea: use *executable* prices. To go long YES you pay the YES ask; to go
long NO you pay the NO ask (= 100 - yes_bid). The mid is informational only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .models import Market


@dataclass(frozen=True)
class ImpliedPrices:
    """Executable implied probabilities and microstructure for a market."""

    yes_ask_prob: Optional[float]     # cost to buy YES (implied P(YES))
    yes_bid_prob: Optional[float]     # proceeds to sell YES
    no_ask_prob: Optional[float]      # cost to buy NO (implied P(NO))
    mid_prob: Optional[float]         # (yes_bid + yes_ask) / 2
    spread_prob: Optional[float]      # ask - bid, in probability units


def implied_prices(market: Market) -> ImpliedPrices:
    """Compute implied probabilities from executable top-of-book prices."""
    yes_ask = market.yes_ask
    yes_bid = market.yes_bid
    no_ask = market.no_ask
    if no_ask is None and yes_bid is not None:
        no_ask = 100 - yes_bid

    yes_ask_prob = yes_ask / 100.0 if yes_ask is not None else None
    yes_bid_prob = yes_bid / 100.0 if yes_bid is not None else None
    no_ask_prob = no_ask / 100.0 if no_ask is not None else None

    mid_prob: Optional[float] = None
    spread_prob: Optional[float] = None
    if yes_ask is not None and yes_bid is not None:
        mid_prob = (yes_ask + yes_bid) / 200.0
        spread_prob = (yes_ask - yes_bid) / 100.0

    return ImpliedPrices(
        yes_ask_prob=yes_ask_prob,
        yes_bid_prob=yes_bid_prob,
        no_ask_prob=no_ask_prob,
        mid_prob=mid_prob,
        spread_prob=spread_prob,
    )


def trade_fee(price_prob: float, contracts: int, fee_rate: float = 0.07) -> float:
    """Kalshi trading fee in dollars.

    Kalshi's published fee model is ``fee = ceil(fee_rate * C * P * (1 - P))``
    where ``C`` is the number of contracts and ``P`` is price in dollars. The
    ``ceil`` is applied to the dollar amount (fees round up to the next cent at
    minimum); we round up to whole cents to stay conservative.

    Args:
        price_prob: Fill price in dollars (0..1).
        contracts: Number of contracts traded.
        fee_rate: Fee coefficient (default 0.07).

    Returns:
        Fee in dollars (>= 0).
    """
    if contracts <= 0:
        return 0.0
    p = min(max(price_prob, 0.0), 1.0)
    raw = fee_rate * contracts * p * (1.0 - p)
    # round up to the next cent
    return math.ceil(raw * 100.0) / 100.0


def effective_cost(
    side: str,
    market: Market,
    contracts: int = 1,
    *,
    fee_rate: float = 0.07,
    slippage_cents: int = 1,
    assume_taker: bool = True,
) -> Optional[dict]:
    """Total executable cost of taking ``contracts`` on a side, after fees.

    Args:
        side: ``"yes"`` or ``"no"``.
        market: The market to price.
        contracts: Number of contracts.
        fee_rate: Kalshi fee coefficient.
        slippage_cents: Adverse-fill cushion added to the crossing price.
        assume_taker: If True, we cross the spread (pay the ask).

    Returns:
        Dict with ``price_prob``, ``gross_cost``, ``fee``, ``total_cost`` and
        ``breakeven_prob`` (the true probability needed to be EV-neutral), or
        ``None`` if the side is not executable.
    """
    prices = implied_prices(market)
    if side == "yes":
        base = prices.yes_ask_prob
    elif side == "no":
        base = prices.no_ask_prob
    else:
        raise ValueError(f"side must be 'yes' or 'no', got {side!r}")

    if base is None:
        return None

    slip = slippage_cents / 100.0 if assume_taker else 0.0
    price_prob = min(base + slip, 1.0)
    gross_cost = price_prob * contracts
    fee = trade_fee(price_prob, contracts, fee_rate)
    total_cost = gross_cost + fee

    # Breakeven true probability for this side: payout is $1 per winning
    # contract, so we need P(win) * contracts >= total_cost.
    breakeven_prob = total_cost / contracts if contracts else price_prob

    return {
        "side": side,
        "price_prob": price_prob,
        "gross_cost": gross_cost,
        "fee": fee,
        "total_cost": total_cost,
        "breakeven_prob": breakeven_prob,
    }


def liquidity_score(market: Market, *, max_spread_cents: int = 20) -> float:
    """Heuristic 0..1 liquidity score combining spread, depth, and volume.

    Higher is better. A wide spread, thin book, or zero volume all drag the
    score toward 0. This is deliberately conservative.
    """
    score = 1.0

    # Spread component (linear penalty up to max_spread_cents).
    spread = market.spread_cents
    if spread is None:
        return 0.0
    score *= max(0.0, 1.0 - spread / max(max_spread_cents, 1))

    # Depth component (top-3 contracts on the YES ask side).
    depth = 0
    if market.order_book is not None:
        depth = market.order_book.depth_contracts("yes", levels=3)
    depth_factor = min(1.0, depth / 200.0) if depth else 0.25
    score *= 0.5 + 0.5 * depth_factor

    # Volume component (soft).
    vol_factor = min(1.0, market.volume / 500.0) if market.volume else 0.5
    score *= 0.5 + 0.5 * vol_factor

    return round(min(max(score, 0.0), 1.0), 4)

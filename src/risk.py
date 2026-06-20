"""Risk controls: gating, recommended action, and position sizing.

Every gate is conservative by design. A market only earns a Buy recommendation
when it clears *all* configured thresholds; otherwise it is downgraded to Watch
(promising but failed a gate) or Avoid (disqualifying problem such as ambiguous
settlement or negative EV).
"""

from __future__ import annotations

from dataclasses import dataclass

from .edge import Opportunity
from .utils.config import Config


@dataclass
class RiskParams:
    """Resolved risk thresholds (from config)."""

    min_edge: float = 0.05
    min_ev_after_fees: float = 0.02
    min_confidence: float = 0.50
    min_liquidity_score: float = 0.30
    max_spread_cents: int = 8
    max_position_per_market_usd: float = 50.0
    max_daily_exposure_usd: float = 200.0
    avoid_ambiguous_settlement: bool = True
    kelly_fraction: float = 0.25
    # "Trust the market" sanity gate: if a market is liquid (>= this volume) and
    # the model disagrees by more than this edge, the model is almost certainly
    # wrong, not the market — so refuse to act.
    implausible_edge: float = 0.20
    liquid_volume: int = 500

    @classmethod
    def from_config(cls, config: Config) -> "RiskParams":
        r = config.get("risk", {}) or {}
        return cls(
            min_edge=float(r.get("min_edge", 0.05)),
            min_ev_after_fees=float(r.get("min_ev_after_fees", 0.02)),
            min_confidence=float(r.get("min_confidence", 0.50)),
            min_liquidity_score=float(r.get("min_liquidity_score", 0.30)),
            max_spread_cents=int(r.get("max_spread_cents", 8)),
            max_position_per_market_usd=float(r.get("max_position_per_market_usd", 50.0)),
            max_daily_exposure_usd=float(r.get("max_daily_exposure_usd", 200.0)),
            avoid_ambiguous_settlement=bool(r.get("avoid_ambiguous_settlement", True)),
            kelly_fraction=float(r.get("kelly_fraction", 0.25)),
            implausible_edge=float(r.get("implausible_edge", 0.20)),
            liquid_volume=int(r.get("liquid_volume", 500)),
        )


def _kelly_contracts(
    win_prob: float,
    price_prob: float,
    bankroll: float,
    kelly_fraction: float,
    max_position_usd: float,
) -> tuple[int, float]:
    """Fractional-Kelly position size.

    For a binary contract bought at cost ``c`` (price in dollars) that pays $1
    on a win with probability ``q``, the full-Kelly fraction of bankroll to risk
    is ``(q - c) / (1 - c)``. We scale by ``kelly_fraction`` and cap by the
    per-market dollar limit.

    Returns:
        ``(contracts, dollars_at_risk)``.
    """
    if price_prob <= 0 or price_prob >= 1 or win_prob <= price_prob:
        return 0, 0.0
    full_kelly = (win_prob - price_prob) / (1.0 - price_prob)
    frac = max(0.0, full_kelly) * kelly_fraction
    dollars = min(frac * bankroll, max_position_usd)
    contracts = int(dollars // price_prob)
    return max(contracts, 0), round(contracts * price_prob, 2)


def apply_risk(opp: Opportunity, params: RiskParams, bankroll: float) -> Opportunity:
    """Set ``action``, ``suggested_contracts`` and ``max_position_usd`` on ``opp``.

    Mutates and returns the same :class:`Opportunity`.
    """
    notes: list[str] = []

    # Hard disqualifiers -> Avoid.
    if opp.best_side is None or opp.yes_ask is None:
        opp.action = "Avoid"
        opp.risk_notes = ["No executable price."]
        return opp
    if params.avoid_ambiguous_settlement and opp.ambiguous_settlement:
        opp.action = "Avoid"
        opp.risk_notes = ["Ambiguous settlement source."]
        return opp
    if opp.ev_after_fees <= 0:
        opp.action = "Avoid"
        opp.risk_notes = [f"Non-positive EV after fees ({opp.ev_after_fees:+.3f})."]
        return opp
    # "Trust the market" sanity gate: a deep, liquid market is very unlikely to
    # be mispriced by a huge margin. If the model claims a giant edge against
    # real volume, the model is almost certainly wrong — refuse to act.
    if (opp.volume >= params.liquid_volume
            and abs(opp.gross_edge) > params.implausible_edge):
        opp.action = "Avoid"
        opp.risk_notes = [
            f"Edge {opp.gross_edge:+.2f} implausibly large vs a liquid market "
            f"(volume {opp.volume}); model likely wrong — trusting the market."
        ]
        return opp

    # Soft gates -> Watch if any fails.
    passed = True
    if abs(opp.gross_edge) < params.min_edge:
        passed = False
        notes.append(f"Edge {opp.gross_edge:.3f} < min {params.min_edge}.")
    if opp.ev_after_fees < params.min_ev_after_fees:
        passed = False
        notes.append(f"EV {opp.ev_after_fees:.3f} < min {params.min_ev_after_fees}.")
    if opp.confidence < params.min_confidence:
        passed = False
        notes.append(f"Confidence {opp.confidence:.2f} < min {params.min_confidence}.")
    if opp.liquidity < params.min_liquidity_score:
        passed = False
        notes.append(f"Liquidity {opp.liquidity:.2f} < min {params.min_liquidity_score}.")
    if opp.spread_cents is not None and opp.spread_cents > params.max_spread_cents:
        passed = False
        notes.append(f"Spread {opp.spread_cents}c > max {params.max_spread_cents}c.")

    if not passed:
        opp.action = "Watch"
        opp.risk_notes = notes
        return opp

    # Passed all gates: size it and recommend the side.
    win_prob = opp.fair_yes if opp.best_side == "yes" else opp.fair_no
    price_prob = opp.yes_ask_prob if opp.best_side == "yes" else opp.no_ask_prob
    contracts, dollars = _kelly_contracts(
        win_prob, price_prob or 1.0, bankroll, params.kelly_fraction,
        params.max_position_per_market_usd,
    )
    opp.suggested_contracts = contracts
    opp.max_position_usd = dollars
    opp.action = "Buy YES" if opp.best_side == "yes" else "Buy NO"
    if contracts == 0:
        opp.action = "Watch"
        notes.append("Kelly size rounded to 0 contracts.")
    opp.risk_notes = notes or ["Cleared all risk gates."]
    return opp


def enforce_daily_exposure(opps: list[Opportunity], params: RiskParams) -> list[Opportunity]:
    """Cap cumulative recommended exposure at the daily limit.

    Buys are processed in descending EV order; once the daily budget is spent,
    further buys are downgraded to Watch.
    """
    budget = params.max_daily_exposure_usd
    spent = 0.0
    for opp in sorted(opps, key=lambda o: o.ev_after_fees, reverse=True):
        if not opp.action.startswith("Buy"):
            continue
        if spent + opp.max_position_usd > budget:
            opp.action = "Watch"
            opp.risk_notes.append(
                f"Daily exposure cap reached (${budget:.0f}); downgraded to Watch."
            )
            opp.suggested_contracts = 0
            opp.max_position_usd = 0.0
        else:
            spent += opp.max_position_usd
    return opps

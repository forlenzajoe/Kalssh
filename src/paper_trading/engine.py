"""Paper-trading engine.

When the scanner produces a signal above the configured edge threshold, the
engine logs a *hypothetical* trade with entry price, fair value, timestamp and
notes. Outcomes can be settled later (manually or from observed weather), which
computes realized P&L. No live orders are ever placed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..edge import Opportunity
from ..utils.config import Config
from ..utils.logging import get_logger
from .store import make_store

logger = get_logger("paper_trading")


@dataclass
class PaperTrade:
    """A single hypothetical trade and its lifecycle."""

    id: str
    timestamp: str
    ticker: str
    title: str
    side: str                  # yes | no
    action: str                # Buy YES | Buy NO
    entry_price: float         # price paid in dollars (incl. slippage assumption)
    fair_value: float          # model fair prob for the chosen side
    edge: float
    confidence: float
    contracts: int
    stake_usd: float
    settlement_outcome: Optional[str] = None   # "yes" | "no" | None (open)
    settlement_value: Optional[float] = None   # realized value per contract (0 or 1)
    realized_pnl: Optional[float] = None
    status: str = "open"       # open | settled
    notes: str = ""

    @classmethod
    def from_opportunity(cls, opp: Opportunity) -> "PaperTrade":
        side = "yes" if opp.best_side == "yes" else "no"
        entry = opp.yes_ask_prob if side == "yes" else opp.no_ask_prob
        fair = opp.fair_yes if side == "yes" else opp.fair_no
        return cls(
            id=uuid.uuid4().hex[:12],
            timestamp=datetime.now(timezone.utc).isoformat(),
            ticker=opp.ticker,
            title=opp.title,
            side=side,
            action=opp.action,
            entry_price=round(float(entry or 0.0), 4),
            fair_value=round(float(fair), 4),
            edge=round(float(opp.gross_edge), 4),
            confidence=round(float(opp.confidence), 3),
            contracts=int(opp.suggested_contracts),
            stake_usd=round(float(opp.max_position_usd), 2),
            notes="; ".join(opp.risk_notes),
        )


class PaperTradingEngine:
    """Logs and settles paper trades against a configured store."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.threshold = float(config.get("paper_trading.signal_threshold_edge", 0.05))
        self.fee_rate = float(config.get("fees.trade_fee_rate", 0.07))
        kind = str(config.get("paper_trading.store", "sqlite"))
        self.store = make_store(
            kind,
            str(config.get("paper_trading.sqlite_path", "data/paper_trades.sqlite")),
            str(config.get("paper_trading.csv_path", "data/paper_trades.csv")),
        )

    def record_signals(self, opportunities: list[Opportunity]) -> list[PaperTrade]:
        """Log a paper trade for each Buy signal clearing the edge threshold."""
        if self.config.paper_only is False:
            logger.warning("paper_only is False but live trading is NOT implemented; "
                           "continuing in paper mode.")
        logged: list[PaperTrade] = []
        # A market/side pair is logged at most once across its lifetime, so
        # repeated scans (e.g. from the watcher loop) don't duplicate trades.
        already = {(r["ticker"], r["side"]) for r in self.store.all()}
        for opp in opportunities:
            if not opp.action.startswith("Buy"):
                continue
            if abs(opp.gross_edge) < self.threshold or opp.suggested_contracts <= 0:
                continue
            side = "yes" if opp.best_side == "yes" else "no"
            if (opp.ticker, side) in already:
                continue
            trade = PaperTrade.from_opportunity(opp)
            self.store.insert(trade)
            logged.append(trade)
            already.add((opp.ticker, side))
            logger.info("Logged paper trade %s %s x%d @ %.2f (edge %.3f)",
                        trade.action, trade.ticker, trade.contracts,
                        trade.entry_price, trade.edge)
        return logged

    def settle(self, trade_id: str, outcome: str) -> Optional[PaperTrade]:
        """Settle an open trade given the realized outcome ("yes" or "no")."""
        rows = {r["id"]: r for r in self.store.all()}
        row = rows.get(trade_id)
        if row is None:
            logger.warning("Trade %s not found", trade_id)
            return None
        trade = _row_to_trade(row)
        won = (outcome == trade.side)
        settle_value = 1.0 if won else 0.0
        gross = (settle_value - trade.entry_price) * trade.contracts
        # Entry fee already approximated; settlement on Kalshi is fee-free.
        from ..kalshi.pricing import trade_fee
        fee = trade_fee(trade.entry_price, trade.contracts, self.fee_rate)
        trade.settlement_outcome = outcome
        trade.settlement_value = settle_value
        trade.realized_pnl = round(gross - fee, 2)
        trade.status = "settled"
        self.store.update(trade)
        logger.info("Settled %s: outcome=%s pnl=%.2f", trade_id, outcome, trade.realized_pnl)
        return trade

    def history(self) -> list[dict]:
        """Return all stored trades as dict rows (most recent first)."""
        return self.store.all()


def _row_to_trade(row: dict) -> PaperTrade:
    def num(v, cast):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    return PaperTrade(
        id=row["id"],
        timestamp=row["timestamp"],
        ticker=row["ticker"],
        title=row["title"],
        side=row["side"],
        action=row["action"],
        entry_price=num(row.get("entry_price"), float) or 0.0,
        fair_value=num(row.get("fair_value"), float) or 0.0,
        edge=num(row.get("edge"), float) or 0.0,
        confidence=num(row.get("confidence"), float) or 0.0,
        contracts=num(row.get("contracts"), int) or 0,
        stake_usd=num(row.get("stake_usd"), float) or 0.0,
        settlement_outcome=row.get("settlement_outcome") or None,
        settlement_value=num(row.get("settlement_value"), float),
        realized_pnl=num(row.get("realized_pnl"), float),
        status=row.get("status", "open") or "open",
        notes=row.get("notes", "") or "",
    )

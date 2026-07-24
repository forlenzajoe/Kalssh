"""Command-line interface for the Kalshi weather scanner.

Subcommands:
  * ``scan``      — run the scanner and print ranked opportunities; optionally
                    log paper trades for signals above threshold.
  * ``backtest``  — run the synthetic backtest or a CSV backtest and report.
  * ``settle``    — settle an open paper trade by id.
  * ``history``   — print the paper-trade log.

Everything defaults to MOCK mode, so it runs with no credentials.
"""

from __future__ import annotations

import argparse
import sys

import csv
import time
from datetime import datetime, timezone
from pathlib import Path

from .backtest.engine import run_backtest, run_synthetic_backtest
from .backtest.report import format_summary, write_html_report
from .edge_watcher import scan_for_edges
from .paper_trading.engine import PaperTradingEngine
from .scanner import scan
from .utils.config import load_config
from .utils.logging import setup_logging


def _print_opportunities(opps, top_n: int) -> None:
    header = (
        f"{'TICKER':<14}{'ACTION':<9}{'FAIR':>6}{'IMPL':>6}{'EDGE':>7}"
        f"{'EV$':>7}{'CONF':>6}{'LIQ':>6}{'SPR':>5}{'HRS':>7}  TITLE"
    )
    print(header)
    print("-" * len(header))
    for opp in opps[:top_n]:
        impl = opp.yes_ask_prob if opp.yes_ask_prob is not None else float("nan")
        hrs = opp.hours_to_close if opp.hours_to_close is not None else float("nan")
        spr = opp.spread_cents if opp.spread_cents is not None else 0
        title = (opp.title[:48] + "…") if len(opp.title) > 49 else opp.title
        print(
            f"{opp.ticker:<14}{opp.action:<9}{opp.fair_yes:>6.2f}{impl:>6.2f}"
            f"{opp.gross_edge:>+7.3f}{opp.ev_after_fees:>+7.3f}{opp.confidence:>6.2f}"
            f"{opp.liquidity:>6.2f}{spr:>5d}{hrs:>7.1f}  {title}"
        )


def cmd_scan(args, config) -> int:
    opps = scan(config)
    top_n = args.top or int(config.get("scanner.top_n", 25))
    print(f"\nMode: {config.mode.upper()} | Markets evaluated: {len(opps)}\n")
    _print_opportunities(opps, top_n)

    buys = [o for o in opps if o.action.startswith("Buy")]
    print(f"\n{len(buys)} actionable Buy signal(s).")

    has_prices = any(o.yes_ask_prob is not None for o in opps)
    if config.mode == "live" and not has_prices and not config.has_kalshi_credentials:
        print(
            "\nNOTE: Live markets loaded but NO prices are visible. Kalshi gates live\n"
            "prices/order books behind authentication. Add your Kalshi API key\n"
            "(KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH in .env) to see executable\n"
            "prices. Until then every market reads 'Avoid' (no price to bet against)."
        )
    if args.paper:
        engine = PaperTradingEngine(config)
        logged = engine.record_signals(opps)
        print(f"Logged {len(logged)} paper trade(s) to the trade store.")
    return 0


def cmd_backtest(args, config) -> int:
    if args.csv:
        metrics = run_backtest(args.csv)
    else:
        metrics = run_synthetic_backtest(config, days=args.days)
    print(format_summary(metrics))
    if args.html:
        path = write_html_report(metrics, args.html)
        print(f"\nHTML report written to {path}")
    return 0


def cmd_settle(args, config) -> int:
    engine = PaperTradingEngine(config)
    trade = engine.settle(args.trade_id, args.outcome)
    if trade is None:
        print(f"No trade found with id {args.trade_id}")
        return 1
    print(f"Settled {trade.ticker}: outcome={trade.settlement_outcome} "
          f"PnL=${trade.realized_pnl:.2f}")
    return 0


def cmd_autosettle(args, config) -> int:
    """Settle every open paper trade whose market has a real Kalshi result."""
    import requests

    engine = PaperTradingEngine(config)
    api = "https://api.elections.kalshi.com/trade-api/v2/markets/"
    session = requests.Session()
    n = wins = 0
    pnl = 0.0
    for row in engine.store.all():
        if row.get("status") != "open":
            continue
        try:
            market = session.get(api + row["ticker"], timeout=15).json()["market"]
        except Exception as exc:
            print(f"  {row['ticker']}: fetch failed ({exc}); left open")
            continue
        result = (market.get("result") or "").lower()
        if result not in ("yes", "no"):
            continue
        trade = engine.settle(row["id"], result)
        if trade is not None:
            n += 1
            wins += trade.settlement_outcome == trade.side
            pnl += trade.realized_pnl or 0.0
            print(f"  settled {trade.ticker}: outcome={result} "
                  f"PnL=${trade.realized_pnl:+.2f}")
    print(f"Auto-settled {n} trade(s) | wins {wins}/{n} | realized ${pnl:+.2f}")
    return 0


def cmd_history(args, config) -> int:
    engine = PaperTradingEngine(config)
    rows = engine.history()
    if not rows:
        print("No paper trades logged yet.")
        return 0
    print(f"{'ID':<14}{'TICKER':<14}{'SIDE':<5}{'ENTRY':>7}{'FAIR':>7}"
          f"{'CONTR':>7}{'STATUS':>9}{'PNL':>9}")
    for r in rows:
        pnl = r.get("realized_pnl")
        pnl_s = f"{float(pnl):+.2f}" if pnl not in (None, "", "None") else "—"
        print(f"{r['id']:<14}{r['ticker']:<14}{r['side']:<5}"
              f"{float(r['entry_price']):>7.2f}{float(r['fair_value']):>7.2f}"
              f"{int(float(r['contracts'])):>7}{r['status']:>9}{pnl_s:>9}")
    return 0


def _log_alerts(alerts, path: str) -> None:
    cols = ["kind", "timestamp", "market", "description", "action", "cost_cents",
            "net_edge_cents", "contracts_available", "volume", "notes"]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        if new:
            w.writeheader()
        for a in alerts:
            w.writerow(a.as_row())


def _print_alerts(alerts) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    if not alerts:
        print(f"[{stamp}] no edge — books efficient (this is the normal result).")
        return
    print(f"[{stamp}] 🚨 {len(alerts)} EDGE(S) DETECTED:")
    for a in alerts:
        print(f"  [{a.kind}] {a.action} {a.market}")
        print(f"     {a.description}")
        print(f"     cost {a.cost_cents:.0f}c | net +{a.net_edge_cents:.1f}c/contract "
              f"| fillable {a.contracts_available} | vol {a.volume}")


def cmd_watch(args, config) -> int:
    if config.mode != "live":
        print("Edge watcher requires live mode (set 'mode: live' in config.yaml).")
        return 1
    log_path = "data/edge_alerts.csv"
    print(f"Edge watcher running ({'once' if args.once else f'every {args.interval}s'}). "
          f"Alerts logged to {log_path}. Ctrl+C to stop.\n")
    seen: set[str] = set()
    try:
        while True:
            alerts = scan_for_edges(config)
            fresh = [a for a in alerts if a.key() not in seen]
            _print_alerts(fresh)
            if fresh:
                _log_alerts(fresh, log_path)
                from .notify import notify_edges
                notify_edges(fresh, config)
                seen.update(a.key() for a in fresh)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nEdge watcher stopped.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kalshi-scan", description="Kalshi weather mispricing scanner (paper only)."
    )
    p.add_argument("--config", default=None, help="Path to config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="Run the scanner.")
    s.add_argument("--top", type=int, default=None, help="Show top N opportunities.")
    s.add_argument("--paper", action="store_true", help="Log paper trades for Buy signals.")
    s.set_defaults(func=cmd_scan)

    b = sub.add_parser("backtest", help="Run a backtest.")
    b.add_argument("--csv", default=None, help="CSV of precomputed samples (optional).")
    b.add_argument("--days", type=int, default=30, help="Synthetic backtest lookback days.")
    b.add_argument("--html", default=None, help="Write an HTML report to this path.")
    b.set_defaults(func=cmd_backtest)

    se = sub.add_parser("settle", help="Settle an open paper trade.")
    se.add_argument("trade_id")
    se.add_argument("outcome", choices=["yes", "no"])
    se.set_defaults(func=cmd_settle)

    h = sub.add_parser("history", help="Show paper-trade history.")
    h.set_defaults(func=cmd_history)

    a = sub.add_parser("autosettle",
                       help="Settle open paper trades from real Kalshi results.")
    a.set_defaults(func=cmd_autosettle)

    w = sub.add_parser("watch", help="Continuously watch for genuine model-free edges.")
    w.add_argument("--interval", type=int, default=60, help="Seconds between scans.")
    w.add_argument("--once", action="store_true", help="Run a single scan and exit.")
    w.set_defaults(func=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        logfile=str(config.get("logging.file", "data/scanner.log")),
    )
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())

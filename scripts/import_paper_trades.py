"""Import paper trades from a CSV export into this machine's trade store.

Used to move history between machines (e.g. desktop -> server) so the track
record stays continuous when the notifier moves. Rows whose ``id`` already
exists are skipped, so re-running is safe.

Run: python scripts/import_paper_trades.py data/paper_trades_export.csv
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.config import load_config  # noqa: E402

COLUMNS = [
    "id", "timestamp", "ticker", "title", "side", "action", "entry_price",
    "fair_value", "edge", "confidence", "contracts", "stake_usd",
    "settlement_outcome", "settlement_value", "realized_pnl", "status", "notes",
]


def main(csv_path: str) -> int:
    config = load_config()
    db = ROOT / str(config.get("paper_trading.sqlite_path", "data/paper_trades.sqlite"))
    if not Path(csv_path).exists():
        print(f"No such CSV: {csv_path}")
        return 1

    con = sqlite3.connect(db)
    # Creating the store via the engine guarantees the table exists first.
    from src.paper_trading.engine import PaperTradingEngine  # noqa: E402
    PaperTradingEngine(config)

    existing = {r[0] for r in con.execute("select id from paper_trades")}
    added = skipped = 0
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("id") in existing:
                skipped += 1
                continue
            values = [row.get(c) or None for c in COLUMNS]
            con.execute(
                f"insert into paper_trades ({','.join(COLUMNS)}) "
                f"values ({','.join('?' * len(COLUMNS))})", values)
            added += 1
    con.commit()

    total = con.execute("select count(*) from paper_trades").fetchone()[0]
    settled = con.execute(
        "select count(*) from paper_trades where status='settled'").fetchone()[0]
    print(f"Imported {added}, skipped {skipped} already present.")
    print(f"Store now holds {total} trade(s), {settled} settled -> {db}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    raise SystemExit(main(sys.argv[1]))

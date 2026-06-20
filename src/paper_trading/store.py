"""Persistence backends for paper trades: SQLite and CSV."""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid circular import at runtime
    from .engine import PaperTrade

_COLUMNS = [
    "id", "timestamp", "ticker", "title", "side", "action",
    "entry_price", "fair_value", "edge", "confidence", "contracts",
    "stake_usd", "settlement_outcome", "settlement_value", "realized_pnl",
    "status", "notes",
]


class SQLiteStore:
    """SQLite-backed trade log."""

    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    ticker TEXT,
                    title TEXT,
                    side TEXT,
                    action TEXT,
                    entry_price REAL,
                    fair_value REAL,
                    edge REAL,
                    confidence REAL,
                    contracts INTEGER,
                    stake_usd REAL,
                    settlement_outcome TEXT,
                    settlement_value REAL,
                    realized_pnl REAL,
                    status TEXT,
                    notes TEXT
                )
                """
            )

    def insert(self, trade: "PaperTrade") -> None:
        row = asdict(trade)
        with self._conn() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO paper_trades ({','.join(_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in _COLUMNS)})",
                [row[c] for c in _COLUMNS],
            )

    def update(self, trade: "PaperTrade") -> None:
        self.insert(trade)

    def all(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM paper_trades ORDER BY timestamp DESC"
            )]


class CSVStore:
    """CSV-backed trade log (append/rewrite)."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=_COLUMNS).writeheader()

    def _read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def _write_all(self, rows: list[dict]) -> None:
        with self.path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in _COLUMNS})

    def insert(self, trade: "PaperTrade") -> None:
        rows = [r for r in self._read_all() if r.get("id") != trade.id]
        rows.append(asdict(trade))
        self._write_all(rows)

    def update(self, trade: "PaperTrade") -> None:
        self.insert(trade)

    def all(self) -> list[dict]:
        return self._read_all()


def make_store(kind: str, sqlite_path: str, csv_path: str):
    """Factory: return a SQLite or CSV store based on config."""
    if kind == "csv":
        return CSVStore(csv_path)
    return SQLiteStore(sqlite_path)

"""Liveness reporting — prove the pipeline is alive, or say loudly that it isn't.

The failure mode this exists to kill: a dead scanner and a quiet market look
identical from your phone. Both are silence. So the system reports in on a
schedule whether or not it found anything, and shouts if scans have stalled.

Three layers, because no single one is sufficient:

1. ``systemd Restart=always`` restarts a crashed process (already configured).
2. This module's daily heartbeat proves the whole chain -- scanner, network,
   ntfy, phone -- is working, and escalates to a high-priority alert when scans
   have stopped even though the host is still up.
3. An optional external dead-man's-switch (``heartbeat.deadman_url``, e.g. a
   healthchecks.io ping URL) covers what layers 1-2 structurally cannot: a
   server that is fully dead or offline can't report its own death. The switch
   alerts when the expected ping *stops arriving*.
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .notify import push_notify
from .utils.config import Config
from .utils.logging import get_logger

logger = get_logger("health")

MARKER = "data/last_scan.txt"


def record_scan(root: Path) -> None:
    """Stamp a successful scan. Cheap, and the basis of stall detection."""
    try:
        path = root / MARKER
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except OSError as exc:  # never let bookkeeping break a scan
        logger.warning("could not write scan marker: %s", exc)


def _last_scan(root: Path):
    try:
        raw = (root / MARKER).read_text(encoding="utf-8").strip()
        stamp = datetime.fromisoformat(raw)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (OSError, ValueError):
        return None


def _service_state(unit: str) -> str:
    """systemd state for a unit, or "unknown" off-Linux / on error."""
    try:
        out = subprocess.run(["systemctl", "is-active", unit], capture_output=True,
                             text=True, timeout=10)
        return (out.stdout or out.stderr).strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def gather_health(config: Config, root: Path) -> dict:
    """Collect the facts a heartbeat should report."""
    db = root / str(config.get("paper_trading.sqlite_path", "data/paper_trades.sqlite"))
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    total = settled = recent = 0
    pnl = 0.0
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        total = con.execute("select count(*) from paper_trades").fetchone()[0]
        settled = con.execute(
            "select count(*) from paper_trades where status='settled'").fetchone()[0]
        recent = con.execute(
            "select count(*) from paper_trades where timestamp >= ?", (since,)).fetchone()[0]
        pnl = con.execute(
            "select coalesce(sum(realized_pnl), 0) from paper_trades "
            "where status='settled'").fetchone()[0] or 0.0
    except sqlite3.Error as exc:
        logger.warning("could not read trade store: %s", exc)

    last = _last_scan(root)
    age_h = None if last is None else (
        datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    stale_after = float(config.get("notifications.heartbeat.stale_after_hours", 6))

    return {
        "last_scan": last,
        "scan_age_hours": age_h,
        "stalled": age_h is None or age_h > stale_after,
        "stale_after_hours": stale_after,
        "watcher": _service_state("kalshi-watcher"),
        "timer": _service_state("kalshi-signals.timer"),
        "trades_24h": recent,
        "trades_total": total,
        "trades_settled": settled,
        "realized_pnl": float(pnl),
    }


def format_health(h: dict) -> tuple[str, str, str]:
    """Return (title, body, priority) for a health report."""
    if h["stalled"]:
        age = ("no scan on record" if h["scan_age_hours"] is None
               else f"last scan {h['scan_age_hours']:.1f}h ago")
        title = "Kalshi scanner STALLED"
        body = (f"{age.capitalize()} - expected one every "
                f"{h['stale_after_hours']:.0f}h.\n"
                f"watcher={h['watcher']} timer={h['timer']}\n\n"
                "Alerts may be silently missing. Check the server.")
        return title, body, "urgent"

    title = "Kalshi scanner OK"
    body = (f"Alive. Last scan {h['scan_age_hours']:.1f}h ago.\n"
            f"watcher={h['watcher']} timer={h['timer']}\n"
            f"Signals logged in 24h: {h['trades_24h']}\n"
            f"Track record: {h['trades_settled']} settled of {h['trades_total']}, "
            f"paper P&L ${h['realized_pnl']:+.2f}\n\n"
            "No alert today means no qualifying edge - not a broken system.")
    return title, body, "low"


def report_health(config: Config, root: Path) -> bool:
    """Push a heartbeat and ping the dead-man's-switch. True if the push sent."""
    cfg = config.get("notifications", {}) or {}
    hb = cfg.get("heartbeat", {}) or {}
    if not hb.get("enabled", False):
        logger.info("heartbeat disabled; skipping")
        return False

    h = gather_health(config, root)
    title, body, priority = format_health(h)

    ok = push_notify(str(cfg.get("ntfy_topic", "") or ""), title, body,
                     server=str(cfg.get("ntfy_server", "https://ntfy.sh")),
                     priority=priority)
    logger.info("heartbeat push %s (%s)", "sent" if ok else "FAILED", title)

    # Only ping the switch when healthy: a ping during a stall would tell the
    # external monitor everything is fine, defeating the point.
    url = str(hb.get("deadman_url", "") or "")
    if url and not h["stalled"]:
        try:
            requests.get(url, timeout=10).raise_for_status()
            logger.info("dead-man's-switch pinged")
        except requests.RequestException as exc:
            logger.warning("dead-man's-switch ping failed: %s", exc)
    return ok

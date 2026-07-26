"""Notifications for the edge watcher: phone push (ntfy) + desktop toast.

Phone push uses **ntfy.sh** — free, no account. You install the ntfy app
(iOS/Android), subscribe to a private topic name, and this module POSTs to that
topic when an edge is found, delivering a push to your phone. Set the topic in
config.yaml under ``notifications.ntfy_topic`` (pick something unguessable).

Desktop toast uses a stock-Windows PowerShell balloon — no dependencies.

No SMS: real texting requires a paid gateway tied to your account; ntfy push is
the free, legitimate equivalent.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import requests

from .utils.config import Config
from .utils.logging import get_logger

if TYPE_CHECKING:
    from .edge_watcher import EdgeAlert

logger = get_logger("notify")


def push_notify(topic: str, title: str, message: str,
                server: str = "https://ntfy.sh", priority: str = "high") -> bool:
    """Send a phone push via ntfy. Returns True on success."""
    if not topic:
        return False
    # HTTP headers must be Latin-1; strip non-encodable chars (e.g. emoji) from
    # the title. The message body is sent as UTF-8 and may contain anything.
    safe_title = title.encode("ascii", "ignore").decode().strip() or "Kalshi edge"
    try:
        resp = requests.post(
            f"{server.rstrip('/')}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": safe_title, "Priority": priority, "Tags": "money_with_wings",
                     # Tapping the notification opens Kalshi to act on the edge.
                     "Click": "https://kalshi.com/markets"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("ntfy push failed: %s", exc)
        return False


def desktop_notify(title: str, message: str) -> bool:
    """Show a Windows desktop balloon notification (best-effort, no deps)."""
    ps = (
        "[reflection.assembly]::LoadWithPartialName('System.Windows.Forms')|Out-Null;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
        f"$n.ShowBalloonTip(10000,'{title}','{message}',"
        "[System.Windows.Forms.ToolTipIcon]::Info);Start-Sleep -Seconds 8;$n.Dispose()"
    )
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps])
        return True
    except OSError as exc:  # pragma: no cover - platform dependent
        logger.warning("desktop notify failed: %s", exc)
        return False


def notify_signals(trades: list, config: Config) -> int:
    """Push model-based Buy signals that clear the high-conviction quality bar.

    These are *model* signals, not the model-free locked/arb edges that
    ``notify_edges`` handles, so they are filtered harder. The thresholds are
    fit on settled paper trades (see ``notifications.signal_alerts`` in
    config.yaml) and exist to drop the "too good to be true" signals, which the
    settled record shows are where the model is wrong rather than the market:
    cheap entries and huge claimed edges lost money, moderate ones won.

    Returns the number of signals pushed.
    """
    cfg = config.get("notifications", {}) or {}
    sig = cfg.get("signal_alerts", {}) or {}
    if not sig.get("enabled", False) or not trades:
        return 0

    min_entry = float(sig.get("min_entry_price", 0.60))
    max_edge = float(sig.get("max_edge", 0.25))
    min_fair = float(sig.get("min_fair_value", 0.85))

    keep = [t for t in trades
            if t.entry_price >= min_entry
            and abs(t.edge) <= max_edge
            and t.fair_value >= min_fair]
    if not keep:
        logger.info("No signals cleared the alert quality bar (%d candidate(s)).",
                    len(trades))
        return 0

    lines = []
    for t in keep:
        lines.append(
            f"{t.action} {t.ticker} @ up to {t.entry_price * 100:.0f}c "
            f"x{t.contracts} (~${t.stake_usd:.0f}) | model {t.fair_value:.0%} "
            f"vs market {t.entry_price:.0%}"
        )
    body = "\n".join(lines) + "\n\nPaper signal - you decide and place it yourself."
    title = f"Kalshi signal x{len(keep)}"

    topic = str(cfg.get("ntfy_topic", "") or "")
    if topic:
        ok = push_notify(topic, title, body,
                         server=str(cfg.get("ntfy_server", "https://ntfy.sh")))
        logger.info("Signal push %s for %d signal(s).", "sent" if ok else "FAILED", len(keep))
    if cfg.get("desktop", True):
        desktop_notify(title, lines[0])
    return len(keep)


def notify_edges(alerts: list["EdgeAlert"], config: Config) -> None:
    """Fire phone + desktop notifications for a batch of fresh edge alerts."""
    if not alerts:
        return
    cfg = config.get("notifications", {}) or {}
    topic = str(cfg.get("ntfy_topic", "") or "")
    server = str(cfg.get("ntfy_server", "https://ntfy.sh"))

    # Each line carries everything needed to place the bet by hand:
    # side, market, max entry price, net edge after fees, and fillable size.
    lines = [f"{a.action} {a.market} @ up to {a.cost_cents:.0f}c "
             f"(net +{a.net_edge_cents:.1f}c/ct, {a.contracts_available} fillable)"
             for a in alerts]
    body = "\n".join(lines)
    title = f"Kalshi edge x{len(alerts)}"

    if topic:
        ok = push_notify(topic, title, body, server=server)
        logger.info("Phone push %s for %d edge(s).", "sent" if ok else "FAILED", len(alerts))
    else:
        logger.info("No ntfy_topic configured; skipping phone push.")

    if cfg.get("desktop", True):
        desktop_notify(title, lines[0] if lines else body)

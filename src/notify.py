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
            headers={"Title": safe_title, "Priority": priority, "Tags": "money_with_wings"},
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


def notify_edges(alerts: list["EdgeAlert"], config: Config) -> None:
    """Fire phone + desktop notifications for a batch of fresh edge alerts."""
    if not alerts:
        return
    cfg = config.get("notifications", {}) or {}
    topic = str(cfg.get("ntfy_topic", "") or "")
    server = str(cfg.get("ntfy_server", "https://ntfy.sh"))

    lines = [f"{a.action} {a.market}  (+{a.net_edge_cents:.1f}c/contract, "
             f"{a.contracts_available} fillable)" for a in alerts]
    body = "\n".join(lines)
    title = f"Kalshi edge x{len(alerts)}"

    if topic:
        ok = push_notify(topic, title, body, server=server)
        logger.info("Phone push %s for %d edge(s).", "sent" if ok else "FAILED", len(alerts))
    else:
        logger.info("No ntfy_topic configured; skipping phone push.")

    if cfg.get("desktop", True):
        desktop_notify(title, lines[0] if lines else body)

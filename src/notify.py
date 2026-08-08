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
from typing import TYPE_CHECKING, Optional

import requests

from .utils.config import Config
from .utils.logging import get_logger

if TYPE_CHECKING:
    from .edge_watcher import EdgeAlert

logger = get_logger("notify")


def market_url(ticker: str) -> str:
    """Web URL for the Kalshi page where a market ticker can be traded.

    A market ticker is ``SERIES-EVENT_DATE-STRIKE`` (e.g.
    ``KXHIGHPHIL-26JUL26-B86.5``). Kalshi's tradable page is the *event* page,
    which lists every strike for that day, at
    ``kalshi.com/markets/<series>/<series>-<date>`` (all lowercase). Individual
    strikes are not separately addressable, so this lands on the day's ladder
    with the target strike visible. Falls back to the series page, then the
    market index, if the ticker has an unexpected shape.
    """
    base = "https://kalshi.com/markets"
    # Arb alerts use "TICKER_A | TICKER_B"; link the first leg.
    first = ticker.split("|")[0].strip()
    parts = first.split("-")
    if len(parts) >= 2:
        series, date = parts[0].lower(), parts[1].lower()
        return f"{base}/{series}/{series}-{date}"
    if parts and parts[0]:
        return f"{base}/{parts[0].lower()}"
    return base


def push_notify(topic: str, title: str, message: str,
                server: str = "https://ntfy.sh", priority: str = "high",
                click: Optional[str] = None,
                actions: Optional[list[tuple[str, str]]] = None) -> bool:
    """Send a phone push via ntfy. Returns True on success.

    ``click`` is the URL opened by tapping the notification; ``actions`` adds up
    to three tappable buttons as ``(label, url)`` pairs.
    """
    if not topic:
        return False
    # HTTP headers must be Latin-1; strip non-encodable chars (e.g. emoji) from
    # the title. The message body is sent as UTF-8 and may contain anything.
    safe_title = title.encode("ascii", "ignore").decode().strip() or "Kalshi edge"
    headers = {"Title": safe_title, "Priority": priority, "Tags": "money_with_wings",
               # Tapping the notification opens the market to act on the edge.
               "Click": click or "https://kalshi.com/markets"}
    if actions:
        # ntfy caps this at 3; commas/semicolons are separators so strip them.
        specs = []
        for label, url in actions[:3]:
            clean = label.replace(",", " ").replace(";", " ").strip()
            specs.append(f"view, {clean}, {url}, clear=true")
        headers["Actions"] = "; ".join(specs).encode("ascii", "ignore").decode()
    try:
        resp = requests.post(
            f"{server.rstrip('/')}/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
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

    blocks, actions = [], []
    for t in keep:
        url = market_url(t.ticker)
        side = t.side.upper()
        # Market titles arrive like "Will the **high temp in Philadelphia** be
        # 86-87° on Jul 26, 2026?" -- strip markdown for the phone.
        question = (t.title or t.ticker).replace("**", "").strip()
        win_pct = t.fair_value * 100
        cost = t.entry_price * 100
        blocks.append(
            f"[BET {side}] {question}\n"
            f"1. Open the market (tap this alert)\n"
            f"2. Choose {side}\n"
            f"3. Pay up to {cost:.0f} cents per contract - no more\n"
            f"4. Size guide: {t.contracts} contracts = about ${t.stake_usd:.0f}\n"
            f"Model gives {side} a {win_pct:.0f}% chance; the market is "
            f"charging {cost:.0f}%.\n{url}"
        )
        actions.append((t.ticker.split("-")[0].replace("KXHIGH", "") or "market", url))

    body = "\n\n".join(blocks) + (
        "\n\nThis is a model signal, NOT a guaranteed winner. It loses "
        f"sometimes. Only bet money you can afford to lose."
    )
    if len(keep) == 1:
        t = keep[0]
        city = t.ticker.split("-")[0].replace("KXHIGH", "") or "market"
        title = f"BET {t.side.upper()}: {city} @ up to {t.entry_price * 100:.0f}c"
    else:
        title = f"Kalshi: {len(keep)} bets found - see details"

    topic = str(cfg.get("ntfy_topic", "") or "")
    if topic:
        ok = push_notify(topic, title, body,
                         server=str(cfg.get("ntfy_server", "https://ntfy.sh")),
                         click=market_url(keep[0].ticker), actions=actions)
        logger.info("Signal push %s for %d signal(s).", "sent" if ok else "FAILED", len(keep))
    if cfg.get("desktop", True):
        desktop_notify(title, blocks[0].splitlines()[0])
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
             f"\n{market_url(a.market)}"
             for a in alerts]
    body = "\n\n".join(lines)
    title = f"Kalshi edge x{len(alerts)}"

    if topic:
        actions = [(a.market.split("-")[0].replace("KXHIGH", "") or "market",
                    market_url(a.market)) for a in alerts]
        ok = push_notify(topic, title, body, server=server,
                         click=market_url(alerts[0].market), actions=actions)
        logger.info("Phone push %s for %d edge(s).", "sent" if ok else "FAILED", len(alerts))
    else:
        logger.info("No ntfy_topic configured; skipping phone push.")

    if cfg.get("desktop", True):
        desktop_notify(title, lines[0] if lines else body)

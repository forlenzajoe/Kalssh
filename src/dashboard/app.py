"""Local Streamlit dashboard for the Kalshi weather scanner.

Run with::

    streamlit run src/dashboard/app.py

Shows ranked opportunities, paper-trade history, model/backtest performance, and
clear spread/liquidity/settlement warnings. Read-only and paper-only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# Ensure the project root is importable when launched via `streamlit run`.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.engine import run_synthetic_backtest  # noqa: E402
from src.paper_trading.engine import PaperTradingEngine  # noqa: E402
from src.scanner import scan  # noqa: E402
from src.utils.config import load_config  # noqa: E402
from src.utils.logging import setup_logging  # noqa: E402


@st.cache_data(show_spinner=False, ttl=90)
def _load_opportunities(_nonce: float = 0.0):
    config = load_config()
    setup_logging(level="WARNING")
    opps = scan(config)
    rows = [o.as_row() for o in opps]
    details = {o.ticker: o for o in opps}
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return pd.DataFrame(rows), details, config, stamp


def _conf_label(conf: float) -> str:
    if conf >= 0.75:
        return "🟢 High"
    if conf >= 0.5:
        return "🟡 Medium"
    return "🔴 Low"


def _render_play(opp) -> None:
    """Render one recommended bet in plain English: what to buy, risk, and win."""
    side_word = "YES" if opp.action == "Buy YES" else "NO"
    happens = "WILL happen" if side_word == "YES" else "will NOT happen"
    entry = (opp.yes_ask_prob if side_word == "YES" else opp.no_ask_prob) or 0.0
    fair = opp.fair_yes if side_word == "YES" else opp.fair_no
    contracts = max(opp.suggested_contracts, 0)
    stake = round(entry * contracts, 2)
    to_win = round((1.0 - entry) * contracts, 2)

    with st.container(border=True):
        st.markdown(f"### ✅ BET **{side_word}**  ·  {opp.title}")
        st.markdown(
            f"**Do this:** Buy **{side_word}** at up to **{round(entry * 100)}¢** "
            f"per contract — betting it **{happens}**."
        )
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Suggested size", f"{contracts} contracts")
        c2.metric("Risk (you stake)", f"${stake:,.2f}")
        c3.metric("Win if right", f"+${to_win:,.2f}")
        c4.metric("Confidence", _conf_label(opp.confidence))

        c1, c2, c3 = st.columns(3)
        c1.metric("Our fair value", f"{fair:.0%}")
        c2.metric("Market price", f"{entry:.0%}")
        c3.metric("Edge", f"{opp.gross_edge:+.0%}")

        why = opp.model_notes[-1] if opp.model_notes else ""
        st.caption(f"Why this is a bet: model says fair ≈ {fair:.0%} but the market "
                   f"lets you in at {entry:.0%}. {why}")
        warn = _warning_badges(opp)
        if "no warnings" not in warn:
            st.warning(warn)


def _warning_badges(opp) -> str:
    badges = []
    if opp.ambiguous_settlement:
        badges.append("⚠️ ambiguous settlement")
    if opp.spread_cents is not None and opp.spread_cents > 8:
        badges.append(f"⚠️ wide spread {opp.spread_cents}c")
    if opp.liquidity < 0.3:
        badges.append(f"⚠️ thin liquidity {opp.liquidity:.2f}")
    if opp.confidence < 0.5:
        badges.append(f"⚠️ low confidence {opp.confidence:.2f}")
    return "  ".join(badges) if badges else "✅ no warnings"


def main() -> None:
    st.set_page_config(page_title="Kalshi Weather Scanner", layout="wide")
    st.title("🌤️ Kalshi Weather Mispricing Scanner")
    st.caption("Research / paper-trading only — no live orders are placed.")

    if "refresh_nonce" not in st.session_state:
        st.session_state.refresh_nonce = 0.0

    top = st.columns([1, 1, 2])
    if top[0].button("🔄 Refresh now", use_container_width=True):
        import time as _t
        st.session_state.refresh_nonce = _t.time()
        st.rerun()
    auto = top[1].selectbox("Auto-refresh", ["Off", "30s", "60s", "5 min"], index=0)
    interval_ms = {"Off": 0, "30s": 30000, "60s": 60000, "5 min": 300000}[auto]
    if interval_ms:
        import streamlit.components.v1 as components
        components.html(
            f"<script>setTimeout(function(){{window.parent.location.reload();}}, {interval_ms});</script>",
            height=0,
        )

    df, details, config, stamp = _load_opportunities(st.session_state.refresh_nonce)
    mode = config.mode.upper()
    live_note = "🟢 LIVE Kalshi + NWS data" if mode == "LIVE" else "🟡 MOCK demo data"
    top[2].caption(f"{live_note} · Last refreshed: **{stamp}**"
                   + (f" · auto every {auto}" if interval_ms else ""))
    st.info(f"Run mode: **{mode}**  |  Markets evaluated: **{len(df)}**  |  "
            f"Paper-only: **{config.paper_only}**")

    # Detect the auth-gated "metadata but no prices" situation and explain it.
    has_prices = "implied_yes_ask" in df.columns and df["implied_yes_ask"].notna().any()
    if mode == "LIVE" and not has_prices and not config.has_kalshi_credentials:
        st.error(
            "**Live markets loaded, but no prices are visible.** Kalshi gates live "
            "prices/order books behind authentication. Add your Kalshi API key "
            "(`KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` in `.env`) to see "
            "executable prices and get real bet recommendations. Until then the "
            "model still shows live fair values, but every market reads as 'Avoid' "
            "because there is no price to bet against."
        )

    tab_opps, tab_detail, tab_paper, tab_perf = st.tabs(
        ["Top Opportunities", "Market Detail", "Paper Trades", "Model Performance"]
    )

    # --- Top opportunities ---------------------------------------------------
    with tab_opps:
        # Plain-English "what to bet" panel. This panel used to show every Buy
        # signal, including the ones the settled record shows lose money (cheap
        # entries / outsized claimed edges). It now applies the SAME quality bar
        # as the phone alerts, and demotes the rest, so the dashboard and your
        # phone can never disagree about what is worth acting on.
        sig = (config.get("notifications.signal_alerts", {}) or {})
        min_entry = float(sig.get("min_entry_price", 0.60))
        max_edge = float(sig.get("max_edge", 0.25))
        min_fair = float(sig.get("min_fair_value", 0.85))

        def _entry_cost(o):
            return (o.yes_ask_prob if o.action == "Buy YES" else o.no_ask_prob) or 0.0

        def _fair_for_side(o):
            return o.fair_yes if o.action == "Buy YES" else o.fair_no

        def _clears_bar(o):
            return (_entry_cost(o) >= min_entry
                    and abs(o.gross_edge) <= max_edge
                    and _fair_for_side(o) >= min_fair)

        all_buys = [details[t] for t in df[df["action"].str.startswith("Buy")]["ticker"]]
        plays = sorted([o for o in all_buys if _clears_bar(o)],
                       key=lambda o: o.ev_after_fees, reverse=True)
        demoted = sorted([o for o in all_buys if not _clears_bar(o)],
                         key=lambda o: o.ev_after_fees, reverse=True)

        st.header(f"🎯 What to bet right now ({len(plays)})")
        if not plays:
            st.success("Nothing clears the quality bar right now. Sitting out IS the play — "
                       "check back after a refresh.")
        else:
            total_risk = sum((o.yes_ask_prob if o.action == "Buy YES" else o.no_ask_prob or 0)
                             * o.suggested_contracts for o in plays)
            total_win = sum((1 - (o.yes_ask_prob if o.action == "Buy YES"
                                  else o.no_ask_prob or 0)) * o.suggested_contracts
                            for o in plays)
            cap = st.columns(3)
            cap[0].metric("Recommended bets", len(plays))
            cap[1].metric("Total to stake", f"${total_risk:,.2f}")
            cap[2].metric("Total if all win", f"+${total_win:,.2f}")
            st.caption("Ranked best-first by expected value after fees. These cleared "
                       "every risk rule AND the quality bar fit on settled results. "
                       "They are paper bets and they still lose sometimes.")
            for opp in plays:
                _render_play(opp)

        if demoted:
            with st.expander(f"⚠️ {len(demoted)} signal(s) that FAILED the quality bar "
                             "— shown for research, not for betting"):
                st.warning(
                    "On the settled record these were the money-losers: signals with "
                    "cheap entries or outsized claimed edges went 12/20 (60%) for a "
                    "+0.3% paper ROI, which is a loss once real fills and fees are "
                    "counted. The ones above went 14/15 (+21.9%). Big apparent edge "
                    "usually means the model is wrong, not that the market is."
                )
                st.caption(f"Bar: entry ≥ {min_entry:.0%}, |edge| ≤ {max_edge:.0%}, "
                           f"model conviction ≥ {min_fair:.0%}.")
                for opp in demoted:
                    reasons = []
                    if _entry_cost(opp) < min_entry:
                        reasons.append(f"entry {_entry_cost(opp):.0%} too cheap")
                    if abs(opp.gross_edge) > max_edge:
                        reasons.append(f"claimed edge {abs(opp.gross_edge):.0%} too big")
                    if _fair_for_side(opp) < min_fair:
                        reasons.append(f"conviction {_fair_for_side(opp):.0%} too low")
                    st.markdown(f"**{opp.ticker}** — {', '.join(reasons)}")

        st.divider()
        st.subheader("📋 Full market table (all markets, including Watch/Avoid)")
        col1, col2, col3 = st.columns(3)
        action_filter = col1.multiselect(
            "Action", ["Buy YES", "Buy NO", "Watch", "Avoid"],
            default=["Buy YES", "Buy NO"],
        )
        min_edge = col2.slider("Min |edge|", 0.0, 0.30, 0.05, 0.01)
        min_conf = col3.slider("Min confidence", 0.0, 1.0, 0.0, 0.05)

        view = df.copy()
        if action_filter:
            view = view[view["action"].isin(action_filter)]
        view = view[view["gross_edge"].abs() >= min_edge]
        view = view[view["confidence"] >= min_conf]
        view = view.sort_values("ev_after_fees", ascending=False)

        st.subheader(f"Ranked opportunities ({len(view)})")
        st.dataframe(
            view[[
                "ticker", "title", "action", "fair_yes", "implied_yes_ask",
                "gross_edge", "ev_after_fees", "confidence", "liquidity",
                "spread_cents", "hours_to_close", "ambiguous",
                "suggested_contracts", "max_position_usd",
            ]],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "fair_yes = model P(YES) · implied_yes_ask = cost to buy YES · "
            "gross_edge = fair − executable implied · ev_after_fees = $/contract."
        )

    # --- Market detail -------------------------------------------------------
    with tab_detail:
        ticker = st.selectbox("Select a market", df["ticker"].tolist())
        opp = details[ticker]
        st.subheader(opp.title)
        st.write(_warning_badges(opp))
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Action", opp.action)
        c2.metric("Fair YES", f"{opp.fair_yes:.2%}")
        c3.metric("Implied (YES ask)",
                  f"{opp.yes_ask_prob:.2%}" if opp.yes_ask_prob is not None else "—")
        c4.metric("Edge", f"{opp.gross_edge:+.2%}")
        c1.metric("EV after fees", f"${opp.ev_after_fees:+.3f}")
        c2.metric("Confidence", f"{opp.confidence:.2f}")
        c3.metric("Liquidity", f"{opp.liquidity:.2f}")
        c4.metric("Hrs to close",
                  f"{opp.hours_to_close:.1f}" if opp.hours_to_close is not None else "—")

        st.markdown("**Settlement notes**")
        st.write("\n".join(f"- {n}" for n in opp.settlement_notes) or "—")
        st.markdown("**Model notes**")
        st.write("\n".join(f"- {n}" for n in opp.model_notes) or "—")
        st.markdown("**Risk notes**")
        st.write("\n".join(f"- {n}" for n in opp.risk_notes) or "—")

    # --- Paper trades --------------------------------------------------------
    with tab_paper:
        engine = PaperTradingEngine(config)
        if st.button("Log paper trades for current Buy signals"):
            opps = [details[t] for t in df["ticker"]]
            logged = engine.record_signals(opps)
            st.success(f"Logged {len(logged)} paper trade(s).")
        rows = engine.history()
        if rows:
            hist = pd.DataFrame(rows)
            st.dataframe(hist, use_container_width=True, hide_index=True)
            settled = hist[hist["status"] == "settled"]
            if not settled.empty:
                pnl = pd.to_numeric(settled["realized_pnl"], errors="coerce").sum()
                st.metric("Realized PnL (settled)", f"${pnl:.2f}")
        else:
            st.write("No paper trades logged yet.")

    # --- Model performance ---------------------------------------------------
    with tab_perf:
        days = st.slider("Synthetic backtest lookback (days)", 7, 90, 30, 1)
        if st.button("Run backtest"):
            metrics = run_synthetic_backtest(config, days=days)
            s = metrics.summary_dict()
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Win rate", f"{(s['win_rate'] or 0) * 100:.1f}%")
            c2.metric("Realized return", f"{(s['realized_return'] or 0) * 100:.1f}%")
            c3.metric("Brier", s["brier"])
            c4.metric("Log loss", s["log_loss"])
            if metrics.calibration:
                cal = pd.DataFrame(metrics.calibration).set_index("bucket")
                st.subheader("Calibration")
                st.line_chart(cal[["avg_predicted", "observed_freq"]])
            if metrics.equity_curve:
                st.subheader("Cumulative PnL")
                st.line_chart(pd.Series(metrics.equity_curve, name="PnL"))


main()

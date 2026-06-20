"""Render backtest results to console text and an HTML report with charts."""

from __future__ import annotations

from pathlib import Path

from .metrics import BacktestMetrics


def format_summary(metrics: BacktestMetrics) -> str:
    """Return a human-readable multi-line summary of a backtest."""
    s = metrics.summary_dict()
    lines = [
        "=" * 56,
        "BACKTEST SUMMARY",
        "=" * 56,
        f"Predictions scored : {s['n_predictions']}",
        f"Trades simulated   : {s['n_trades']}",
        f"Win rate           : {_pct(s['win_rate'])}",
        f"Average edge        : {_pct(s['avg_edge'])}",
        f"Total stake         : ${s['total_stake']:.2f}",
        f"Total PnL           : ${s['total_pnl']:.2f}",
        f"Realized return     : {_pct(s['realized_return'])}",
        f"Max drawdown        : ${s['max_drawdown']:.2f}",
        f"Brier score         : {s['brier']}  (lower is better, 0=perfect)",
        f"Log loss            : {s['log_loss']}  (lower is better)",
        "-" * 56,
        "CALIBRATION (predicted vs observed by probability bucket)",
        "-" * 56,
        f"{'bucket':>12} {'count':>7} {'predicted':>10} {'observed':>10}",
    ]
    for b in metrics.calibration:
        lines.append(
            f"{b['bucket']:>12} {b['count']:>7} "
            f"{b['avg_predicted']:>10.3f} {b['observed_freq']:>10.3f}"
        )
    lines.append("=" * 56)
    return "\n".join(lines)


def _pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%"


def write_html_report(metrics: BacktestMetrics, out_path: str = "reports/backtest.html") -> str:
    """Write an HTML report with calibration + equity charts (plotly if present).

    Falls back to a plain HTML table if plotly is not installed. Returns the
    path written.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    s = metrics.summary_dict()

    charts_html = ""
    try:
        import plotly.graph_objects as go

        # Calibration chart.
        cal = metrics.calibration
        pred = [b["avg_predicted"] for b in cal]
        obs = [b["observed_freq"] for b in cal]
        fig_cal = go.Figure()
        fig_cal.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines",
                                     name="perfect", line=dict(dash="dash")))
        fig_cal.add_trace(go.Scatter(x=pred, y=obs, mode="markers+lines", name="model"))
        fig_cal.update_layout(title="Calibration", xaxis_title="Predicted",
                              yaxis_title="Observed", height=400)
        charts_html += fig_cal.to_html(full_html=False, include_plotlyjs="cdn")

        if metrics.equity_curve:
            fig_eq = go.Figure()
            fig_eq.add_trace(go.Scatter(y=metrics.equity_curve, mode="lines", name="equity"))
            fig_eq.update_layout(title="Cumulative PnL", xaxis_title="Trade #",
                                 yaxis_title="PnL ($)", height=400)
            charts_html += fig_eq.to_html(full_html=False, include_plotlyjs=False)
    except Exception:  # pragma: no cover - plotly optional
        charts_html = "<p><em>Install plotly for charts.</em></p>"

    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in s.items())
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Backtest Report</title>
<style>body{{font-family:system-ui,Arial,sans-serif;margin:2rem;}}
table{{border-collapse:collapse}}td{{border:1px solid #ccc;padding:4px 10px}}</style>
</head><body>
<h1>Kalshi Weather Scanner — Backtest Report</h1>
<h2>Summary</h2><table>{rows}</table>
<h2>Charts</h2>{charts_html}
</body></html>"""
    out.write_text(html, encoding="utf-8")
    return str(out)

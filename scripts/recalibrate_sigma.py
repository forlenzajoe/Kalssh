"""Fit the forecast-uncertainty sigma from REAL settled outcomes.

The model's overconfidence comes from a guessed sigma. This script grid-searches
the day-ahead sigma that minimizes log loss against real settled Kalshi markets,
using the day-of forecast as the mean. It prints the old vs fitted calibration
and the recommended config sigma table.

Run: python scripts/recalibrate_sigma.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.metrics import brier_score, calibration_table, log_loss  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402
from src.models.contract_parser import parse_contract  # noqa: E402
from src.models.forecast_model import NormalForecastModel  # noqa: E402
from src.utils.config import load_config  # noqa: E402
from src.weather.base import Forecast  # noqa: E402
from src.weather.stations import STATIONS  # noqa: E402

SERIES_STATION = {
    "KXHIGHNY": "nyc", "KXHIGHCHI": "chi", "KXHIGHLAX": "lax", "KXHIGHMIA": "mia",
    "KXHIGHAUS": "aus", "KXHIGHDEN": "den", "KXHIGHPHIL": "phl",
}
_session = requests.Session()
_cache: dict = {}


def _hist_forecast(station, start: date, end: date) -> dict:
    key = (station.id, start, end)
    if key in _cache:
        return _cache[key]
    try:
        r = _session.get(
            "https://historical-forecast-api.open-meteo.com/v1/forecast",
            params={"latitude": station.latitude, "longitude": station.longitude,
                    "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
                    "timezone": station.tz, "start_date": start.isoformat(),
                    "end_date": end.isoformat()},
            timeout=30,
        )
        r.raise_for_status()
        d = r.json().get("daily", {})
        out = dict(zip(d.get("time", []), d.get("temperature_2m_max", [])))
    except requests.RequestException:
        out = {}
    _cache[key] = out
    return out


def _score(records, sigma, bias=0.0):
    """Return (brier, logloss, probs, outcomes) for a given sigma and mean bias.

    ``bias`` is added to the forecast mean (corrects a forecast that runs
    systematically high/low relative to the official settlement).
    """
    model = NormalForecastModel(temp_sigma_by_horizon={1: sigma})
    probs, outs = [], []
    for cond, mu, outcome in records:
        fc = Forecast(cond.location_text, cond.target_date, "f", 1,
                      high_temp_f=mu + bias, low_temp_f=mu + bias)
        probs.append(model.estimate(cond, fc, horizon_days=1).fair_yes)
        outs.append(outcome)
    return brier_score(probs, outs), log_loss(probs, outs), probs, outs


def main(days_back: int = 12) -> None:
    cfg = load_config()
    client = KalshiClient(cfg)
    if client.mock:
        print("Live Kalshi access required. Aborting.")
        return

    records = []          # (condition, mu_forecast, outcome)
    needed = defaultdict(list)
    raw = []
    for series, sid in SERIES_STATION.items():
        try:
            data = client._get("/markets", params={"series_ticker": series,
                                                   "status": "settled", "limit": 200})
        except requests.RequestException:
            continue
        for m in data.get("markets", []):
            res = (m.get("result") or "").lower()
            if res not in ("yes", "no"):
                continue
            cond = parse_contract(m.get("title", ""), subtitle=m.get("yes_sub_title", ""),
                                  rules=m.get("rules_primary", ""))
            if cond.target_date is None or cond.threshold is None:
                continue
            if (date.today() - cond.target_date).days > days_back:
                continue
            raw.append((sid, cond, 1 if res == "yes" else 0))
            needed[sid].append(cond.target_date)

    fcst = {}
    for sid, dts in needed.items():
        fcst[sid] = _hist_forecast(STATIONS[sid], min(dts), max(dts))

    for sid, cond, outcome in raw:
        mu = fcst.get(sid, {}).get(cond.target_date.isoformat())
        if mu is not None:
            records.append((cond, mu, outcome))

    if not records:
        print("No data.")
        return

    # Measure raw forecast bias vs actual (informational).
    # Joint grid-search over (bias, sigma) minimizing log loss.
    sigmas = [round(x * 0.5, 1) for x in range(4, 25)]    # 2.0 .. 12.0
    biases = [round(x * 0.5, 1) for x in range(-8, 9)]    # -4.0 .. +4.0
    best = None
    for b in biases:
        for s in sigmas:
            br, ll, _, _ = _score(records, s, b)
            if best is None or ll < best[3]:
                best = (b, s, br, ll)
    best_bias, best_sigma, best_brier, best_ll = best

    old_brier, old_ll, _, _ = _score(records, 2.5, 0.0)
    base = sum(o for _, _, o in records) / len(records)
    naive_brier = base * (1 - base)

    print("=" * 60)
    print("RECALIBRATION (bias + sigma) vs REAL SETTLED OUTCOMES")
    print("=" * 60)
    print(f"settled markets fit : {len(records)}  (last {days_back} days)")
    print(f"base rate (YES)     : {base:.1%}  -> naive Brier {naive_brier:.4f}")
    print("-" * 60)
    print(f"OLD  bias=0.0 sigma=2.5 : Brier {old_brier:.4f} | logloss {old_ll:.4f}")
    print(f"FIT  bias={best_bias:+.1f} sigma={best_sigma:<4}: Brier {best_brier:.4f} | logloss {best_ll:.4f}")
    print("-" * 60)
    _, _, probs, outs = _score(records, best_sigma, best_bias)
    print(f"Calibration at fitted bias={best_bias:+.1f}, sigma={best_sigma}:")
    print(f"   {'bucket':>12} {'count':>6} {'predicted':>10} {'observed':>9}")
    for b in calibration_table(probs, outs, 10):
        print(f"   {b['bucket']:>12} {b['count']:>6} {b['avg_predicted']:>10.3f} {b['observed_freq']:>9.3f}")
    print("-" * 60)
    # Recommended config table: scale fitted day-ahead sigma across horizons.
    table = {0: round(best_sigma * 0.85, 1), 1: best_sigma}
    for h in range(2, 8):
        table[h] = round(best_sigma * (1 + 0.18 * (h - 1)), 1)
    print("Recommended config weather.temp_error_sigma_f:")
    print("   " + ", ".join(f"{h}: {v}" for h, v in table.items()))
    print("=" * 60)


if __name__ == "__main__":
    main()

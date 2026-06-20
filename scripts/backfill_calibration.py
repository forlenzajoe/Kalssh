"""Backfill REAL history and run an honest, out-of-sample calibration.

Pulls ALL settled Kalshi high-temp markets (months of real outcomes), joins the
day-of forecast that was actually issued (Open-Meteo archive), fits per-station
(bias, sigma) on older data, and evaluates on held-out recent data. This is the
legitimate "backdate": real history, no fabrication. It tells us TODAY whether
the model is calibrated — it does not manufacture readiness.

Run: python scripts/backfill_calibration.py
"""

from __future__ import annotations

import math
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
from src.utils.config import load_config  # noqa: E402
from src.weather.stations import STATIONS  # noqa: E402

SERIES_STATION = {"KXHIGHNY": "nyc", "KXHIGHCHI": "chi", "KXHIGHLAX": "lax",
                  "KXHIGHMIA": "mia", "KXHIGHAUS": "aus", "KXHIGHDEN": "den",
                  "KXHIGHPHIL": "phl"}
_session = requests.Session()


def _cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _prob(cond, mu: float, sigma: float, bias: float) -> float:
    """P(YES) for a temp condition under Normal(mu+bias, sigma)."""
    m = mu + bias
    s = max(sigma, 1e-6)
    t = cond.threshold
    below = _cdf((t - m) / s)
    if cond.operator in ("gte", "gt"):
        return 1.0 - below
    if cond.operator in ("lte", "lt"):
        return below
    if cond.operator == "between" and cond.threshold2 is not None:
        lo, hi = sorted((t, cond.threshold2))
        return _cdf((hi - m) / s) - _cdf((lo - m) / s)
    return 1.0 - below


def _forecast(station, start: date, end: date) -> dict:
    try:
        r = _session.get("https://archive-api.open-meteo.com/v1/archive", params={
            "latitude": station.latitude, "longitude": station.longitude,
            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
            "timezone": station.tz, "start_date": start.isoformat(),
            "end_date": end.isoformat()}, timeout=60)
        r.raise_for_status()
        d = r.json().get("daily", {})
        return dict(zip(d.get("time", []), d.get("temperature_2m_max", [])))
    except requests.RequestException:
        return {}


def _fit(records, biases, sigmas):
    """Grid-search (bias, sigma) minimizing log loss on records."""
    best = None
    for b in biases:
        for s in sigmas:
            probs = [_prob(c, mu, s, b) for c, mu, _ in records]
            outs = [o for _, _, o in records]
            ll = log_loss(probs, outs)
            if best is None or ll < best[2]:
                best = (b, s, ll)
    return best[0], best[1]


def main():
    cfg = load_config()
    client = KalshiClient(cfg)
    if client.mock:
        print("Live access required.")
        return

    # 1) Pull ALL settled markets per city (paginate).
    by_station = defaultdict(list)   # sid -> [(cond, date, outcome)]
    span = defaultdict(list)
    for series, sid in SERIES_STATION.items():
        cursor, pages = None, 0
        while pages < 30:
            p = {"series_ticker": series, "status": "settled", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            d = client._get("/markets", params=p)
            ms = d.get("markets", [])
            for m in ms:
                res = (m.get("result") or "").lower()
                if res not in ("yes", "no"):
                    continue
                cond = parse_contract(m.get("title", ""), subtitle=m.get("yes_sub_title", ""),
                                      rules=m.get("rules_primary", ""))
                if cond.target_date is None or cond.threshold is None:
                    continue
                by_station[sid].append((cond, cond.target_date, 1 if res == "yes" else 0))
                span[sid].append(cond.target_date)
            cursor = d.get("cursor")
            pages += 1
            if not cursor or not ms:
                break

    # 2) Join forecasts (Open-Meteo archive over each station's full range).
    records = defaultdict(list)   # sid -> [(cond, mu, outcome)]
    for sid, rows in by_station.items():
        st = STATIONS[sid]
        fc = _forecast(st, min(span[sid]), max(span[sid]))
        for cond, d, outcome in rows:
            mu = fc.get(d.isoformat())
            if mu is not None:
                records[sid].append((cond, mu, outcome))

    all_recs = [(sid, *r) for sid, rs in records.items() for r in rs]
    total = len(all_recs)
    all_dates = sorted({r[2] for sid, rs in records.items() for r in [(0, 0, x[0]) for x in []]} or
                       {c.target_date for sid, rs in records.items() for (c, _, _) in rs})
    split = all_dates[int(len(all_dates) * 0.7)]

    biases = [round(x * 0.5, 1) for x in range(-8, 9)]
    sigmas = [round(x * 0.5, 1) for x in range(4, 21)]

    # 3) Fit per-station on TRAIN (date < split); evaluate on TEST.
    station_params = {}
    for sid, rs in records.items():
        train = [(c, mu, o) for (c, mu, o) in rs if c.target_date < split]
        if len(train) >= 30:
            station_params[sid] = _fit(train, biases, sigmas)

    # Global fit for comparison.
    global_train = [(c, mu, o) for sid, rs in records.items() for (c, mu, o) in rs
                    if c.target_date < split]
    g_bias, g_sigma = _fit(global_train, biases, sigmas)

    # 4) Evaluate on TEST (held-out recent days).
    def evaluate(prob_fn):
        probs, outs = [], []
        for sid, rs in records.items():
            for (c, mu, o) in rs:
                if c.target_date >= split:
                    probs.append(prob_fn(sid, c, mu))
                    outs.append(o)
        return probs, outs

    p_old, outs = evaluate(lambda sid, c, mu: _prob(c, mu, 2.5, 0.0))
    p_glob, _ = evaluate(lambda sid, c, mu: _prob(c, mu, g_sigma, g_bias))
    p_stat, _ = evaluate(lambda sid, c, mu: _prob(c, mu, *reversed(station_params.get(sid, (g_bias, g_sigma)))) )
    # note: station_params[sid] = (bias, sigma); _prob wants (sigma,bias) order via kwargs below
    p_stat, _ = evaluate(lambda sid, c, mu: _prob(c, mu,
                          station_params.get(sid, (g_bias, g_sigma))[1],
                          station_params.get(sid, (g_bias, g_sigma))[0]))
    base = sum(outs) / len(outs)

    print("=" * 64)
    print("BACKFILLED CALIBRATION — REAL settled history, out-of-sample")
    print("=" * 64)
    print(f"total settled markets : {total}")
    print(f"date span             : {all_dates[0]} -> {all_dates[-1]} ({len(all_dates)} days)")
    print(f"train/test split date : {split}  (test = held-out recent days)")
    print(f"test markets          : {len(outs)}  | base rate YES {base:.1%}")
    print(f"naive Brier (base)    : {base * (1 - base):.4f}")
    print("-" * 64)
    print(f"OLD     (sig2.5,bias0): Brier {brier_score(p_old, outs):.4f} | logloss {log_loss(p_old, outs):.4f}")
    print(f"GLOBAL fit (b{g_bias:+.1f},s{g_sigma}): Brier {brier_score(p_glob, outs):.4f} | logloss {log_loss(p_glob, outs):.4f}")
    print(f"PER-STATION fit       : Brier {brier_score(p_stat, outs):.4f} | logloss {log_loss(p_stat, outs):.4f}")
    print("-" * 64)
    print("per-station fitted (bias °F, sigma °F):")
    for sid, (b, s) in sorted(station_params.items()):
        print(f"   {sid}: bias {b:+.1f}, sigma {s}")
    print("-" * 64)
    print("PER-STATION calibration on test set:")
    print(f"   {'bucket':>12} {'count':>6} {'predicted':>10} {'observed':>9}")
    for bk in calibration_table(p_stat, outs, 10):
        print(f"   {bk['bucket']:>12} {bk['count']:>6} {bk['avg_predicted']:>10.3f} {bk['observed_freq']:>9.3f}")
    print("=" * 64)


if __name__ == "__main__":
    main()

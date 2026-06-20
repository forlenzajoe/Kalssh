"""Validate the ensemble lever: does multi-model spread fix the shoulder bucket?

Uses 5 archived NWP models (GFS, ECMWF, ICON, GEM, MeteoFrance) to get, for each
settled station-day, a consensus forecast and the cross-model SPREAD (a proxy
for that day's forecast uncertainty). Then tests, out-of-sample, whether an
adaptive sigma = a + b*spread beats a static sigma — especially in the broken
[0.1,0.2] shoulder bucket.

Decision rule: build ensemble-sigma into the model ONLY if it measurably beats
static sigma here. Run: python scripts/ensemble_validation.py
"""

from __future__ import annotations

import math
import statistics
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
MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_seamless", "meteofrance_seamless"]
_s = requests.Session()


def _cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _p(cond, mu, sigma):
    s = max(sigma, 1e-6)
    below = _cdf((cond.threshold - mu) / s)
    if cond.operator in ("gte", "gt"):
        return 1 - below
    if cond.operator in ("lte", "lt"):
        return below
    if cond.operator == "between" and cond.threshold2 is not None:
        lo, hi = sorted((cond.threshold, cond.threshold2))
        return _cdf((hi - mu) / s) - _cdf((lo - mu) / s)
    return 1 - below


def multimodel(station, lo, hi):
    """Return {date: (consensus_mean, cross_model_spread)}."""
    try:
        r = _s.get("https://historical-forecast-api.open-meteo.com/v1/forecast", params={
            "latitude": station.latitude, "longitude": station.longitude,
            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
            "timezone": station.tz, "start_date": lo.isoformat(), "end_date": hi.isoformat(),
            "models": ",".join(MODELS)}, timeout=60)
        d = r.json().get("daily", {})
    except requests.RequestException:
        return {}
    times = d.get("time", [])
    out = {}
    for i, t in enumerate(times):
        vals = [d[f"temperature_2m_max_{m}"][i] for m in MODELS
                if d.get(f"temperature_2m_max_{m}") and d[f"temperature_2m_max_{m}"][i] is not None]
        if len(vals) >= 3:
            out[t] = (statistics.mean(vals), statistics.pstdev(vals))
    return out


def main():
    cfg = load_config()
    client = KalshiClient(cfg)
    if client.mock:
        print("Live access required.")
        return
    station_bias = cfg.get("model.station_bias_f", {}) or {}

    recs = defaultdict(list)
    span = defaultdict(list)
    for series, sid in SERIES_STATION.items():
        cursor, pages = None, 0
        while pages < 30:
            par = {"series_ticker": series, "status": "settled", "limit": 1000}
            if cursor:
                par["cursor"] = cursor
            d = client._get("/markets", params=par)
            ms = d.get("markets", [])
            for m in ms:
                res = (m.get("result") or "").lower()
                if res not in ("yes", "no"):
                    continue
                c = parse_contract(m.get("title", ""), subtitle=m.get("yes_sub_title", ""),
                                   rules=m.get("rules_primary", ""))
                if c.target_date and c.threshold is not None:
                    recs[sid].append((c, c.target_date, 1 if res == "yes" else 0))
                    span[sid].append(c.target_date)
            cursor = d.get("cursor")
            pages += 1
            if not cursor or not ms:
                break

    data = []   # (sid, cond, date, outcome, mu_consensus, spread)
    for sid, rows in recs.items():
        mm = multimodel(STATIONS[sid], min(span[sid]), max(span[sid]))
        bias = float(station_bias.get(sid, 0.0))
        for c, d, o in rows:
            if d.isoformat() in mm:
                mu, sp = mm[d.isoformat()]
                data.append((sid, c, d, o, mu + bias, sp))

    # Spread vs realized "surprise": correlation with whether YES happened in a
    # narrow band (a quick signal check) + the main out-of-sample test.
    dates = sorted({d for _, _, d, _, _, _ in data})
    split = dates[int(len(dates) * 0.7)]
    train = [r for r in data if r[2] < split]
    test = [r for r in data if r[2] >= split]

    def score(sigma_fn, rows):
        ps = [_p(c, mu, sigma_fn(sp)) for (_, c, _, _, mu, sp) in rows]
        os_ = [o for (_, _, _, o, _, _) in rows]
        return ps, os_

    # Fit static sigma (best constant) and adaptive sigma = a + b*spread on train.
    sigmas = [round(x * 0.25, 2) for x in range(6, 24)]
    best_static = min(sigmas, key=lambda s: log_loss(*score(lambda sp: s, train)))
    abest = None
    for a in [round(x * 0.25, 2) for x in range(2, 16)]:
        for b in [round(x * 0.25, 2) for x in range(0, 13)]:
            ll = log_loss(*score(lambda sp, a=a, b=b: a + b * sp, train))
            if abest is None or ll < abest[2]:
                abest = (a, b, ll)
    a, b, _ = abest

    ps_s, outs = score(lambda sp: best_static, test)
    ps_a, _ = score(lambda sp, a=a, b=b: a + b * sp, test)
    base = sum(outs) / len(outs)

    print("=" * 64)
    print("ENSEMBLE-SIGMA VALIDATION (multi-model spread) — out-of-sample")
    print("=" * 64)
    print(f"station-day obs with multi-model data: {len(data)} | test {len(outs)}")
    sp_all = [sp for *_, sp in data]
    print(f"cross-model spread: mean {statistics.mean(sp_all):.2f}F, "
          f"p90 {sorted(sp_all)[int(len(sp_all)*0.9)]:.2f}F")
    print("-" * 64)
    print(f"STATIC  sigma={best_static}: Brier {brier_score(ps_s, outs):.4f} | "
          f"logloss {log_loss(ps_s, outs):.4f}")
    print(f"ADAPTIVE sigma={a}+{b}*spread: Brier {brier_score(ps_a, outs):.4f} | "
          f"logloss {log_loss(ps_a, outs):.4f}")
    print("-" * 64)
    print("Shoulder bucket [0.1,0.2] (predicted should approach observed):")
    for name, ps in (("STATIC  ", ps_s), ("ADAPTIVE", ps_a)):
        b12 = [x for x in calibration_table(ps, outs, 10) if x["bucket"] == "[0.1,0.2)"]
        if b12:
            x = b12[0]
            print(f"   {name}: n={x['count']:>4} predicted={x['avg_predicted']:.3f} "
                  f"observed={x['observed_freq']:.3f}")
    print("=" * 64)
    verdict = "BUILD IT" if (brier_score(ps_a, outs) < brier_score(ps_s, outs) - 0.001) else "NOT WORTH IT"
    print(f"VERDICT: ensemble-sigma vs static -> {verdict}")
    print("=" * 64)


if __name__ == "__main__":
    main()

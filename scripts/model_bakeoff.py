"""Out-of-sample model bakeoff on ~2 months of REAL settled Kalshi outcomes.

Uses the TRUE day-of forecast (Open-Meteo historical-forecast) as the mean, the
archived actual to build residuals, and Kalshi's settlement as the label. Fits
each model per station on older days and evaluates on held-out recent days.

Compares:
  * Gaussian        — Normal(forecast+bias, sigma), (bias,sigma) fit per station
  * Empirical       — non-parametric: forecast + empirical residual distribution
  * Student-t       — fat-tailed t(df) around forecast+bias

Reports Brier, log loss, and — critically — the [0.1,0.2] shoulder bucket that
the Gaussian gets wrong.

Run: python scripts/model_bakeoff.py
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

from scipy.stats import t as student_t  # noqa: E402

from src.backtest.metrics import brier_score, calibration_table, log_loss  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402
from src.models.contract_parser import parse_contract  # noqa: E402
from src.utils.config import load_config  # noqa: E402
from src.weather.stations import STATIONS  # noqa: E402

SERIES_STATION = {"KXHIGHNY": "nyc", "KXHIGHCHI": "chi", "KXHIGHLAX": "lax",
                  "KXHIGHMIA": "mia", "KXHIGHAUS": "aus", "KXHIGHDEN": "den",
                  "KXHIGHPHIL": "phl"}
_session = requests.Session()


def _cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _interval_prob(cond, cdf_fn):
    """P(YES) given a CDF function for the realized high."""
    t = cond.threshold
    below = cdf_fn(t)
    if cond.operator in ("gte", "gt"):
        return 1.0 - below
    if cond.operator in ("lte", "lt"):
        return below
    if cond.operator == "between" and cond.threshold2 is not None:
        lo, hi = sorted((t, cond.threshold2))
        return cdf_fn(hi) - cdf_fn(lo)
    return 1.0 - below


def p_gauss(cond, mu, sigma, bias):
    s = max(sigma, 1e-6)
    return _interval_prob(cond, lambda x: _cdf((x - (mu + bias)) / s))


def p_student(cond, mu, scale, df, bias):
    s = max(scale, 1e-6)
    return _interval_prob(cond, lambda x: float(student_t.cdf((x - (mu + bias)) / s, df)))


def p_empirical(cond, mu, residuals, bw=1.0):
    """Forecast + smoothed empirical residual distribution."""
    def cdf(x):
        # P(mu + e <= x) = P(e <= x-mu), smoothed with a normal kernel.
        z = [(x - mu - e) / bw for e in residuals]
        return sum(_cdf(zi) for zi in z) / len(residuals)
    return _interval_prob(cond, cdf)


def _ometeo(host, station, start, end):
    base = "v1/forecast" if "forecast" in host else "v1/archive"
    try:
        r = _session.get(f"https://{host}/{base}", params={
            "latitude": station.latitude, "longitude": station.longitude,
            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
            "timezone": station.tz, "start_date": start.isoformat(),
            "end_date": end.isoformat()}, timeout=60)
        r.raise_for_status()
        d = r.json().get("daily", {})
        return dict(zip(d.get("time", []), d.get("temperature_2m_max", [])))
    except requests.RequestException:
        return {}


def main():
    cfg = load_config()
    client = KalshiClient(cfg)
    if client.mock:
        print("Live access required.")
        return

    by_station = defaultdict(list)
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

    # Join true forecast (mean) + archived actual (for residuals).
    recs = defaultdict(list)         # sid -> [(cond, mu, date, outcome)]
    actuals = {}
    for sid, rows in by_station.items():
        st = STATIONS[sid]
        fc = _ometeo("historical-forecast-api.open-meteo.com", st, min(span[sid]), max(span[sid]))
        ac = _ometeo("archive-api.open-meteo.com", st, min(span[sid]), max(span[sid]))
        actuals[sid] = ac
        for cond, d, o in rows:
            mu = fc.get(d.isoformat())
            if mu is not None:
                recs[sid].append((cond, mu, d, o))

    all_dates = sorted({d for sid in recs for (_, _, d, _) in recs[sid]})
    split = all_dates[int(len(all_dates) * 0.7)]

    # Per-station fits on train.
    g_params, s_params, resid = {}, {}, {}
    biases = [round(x * 0.5, 1) for x in range(-8, 9)]
    sigmas = [round(x * 0.5, 1) for x in range(4, 21)]
    for sid, rows in recs.items():
        train = [(c, mu, o) for (c, mu, d, o) in rows if d < split]
        if len(train) < 40:
            continue
        # Gaussian (bias, sigma)
        best = min(((b, s, log_loss([p_gauss(c, mu, s, b) for c, mu, _ in train],
                                    [o for *_, o in train]))
                    for b in biases for s in sigmas), key=lambda r: r[2])
        g_params[sid] = (best[0], best[1])
        # Residuals (actual - forecast) on train days
        r = [actuals[sid].get(d.isoformat(), mu) - mu
             for (c, mu, d, o) in rows if d < split and actuals[sid].get(d.isoformat()) is not None]
        resid[sid] = r if len(r) >= 20 else None
        # Student-t: bias=median resid, scale from residual spread, df grid
        if r:
            bias = round(statistics.median(r), 1)
            scale = max(statistics.pstdev(r) * 0.8, 1.0)
            df_best = min(([df, log_loss([p_student(c, mu, scale, df, bias) for c, mu, _ in train],
                                         [o for *_, o in train])] for df in (3, 4, 5, 7, 10)),
                          key=lambda x: x[1])[0]
            s_params[sid] = (bias, scale, df_best)

    # Evaluate on test.
    def ev(fn):
        ps, os_ = [], []
        for sid, rows in recs.items():
            for (c, mu, d, o) in rows:
                if d >= split and sid in g_params:
                    ps.append(fn(sid, c, mu))
                    os_.append(o)
        return ps, os_

    test_g, outs = ev(lambda sid, c, mu: p_gauss(c, mu, g_params[sid][1], g_params[sid][0]))
    test_e, _ = ev(lambda sid, c, mu: p_empirical(c, mu, resid[sid]) if resid.get(sid) else
                   p_gauss(c, mu, g_params[sid][1], g_params[sid][0]))
    test_s, _ = ev(lambda sid, c, mu: p_student(c, mu, *(s_params[sid][1:]), s_params[sid][0])
                   if sid in s_params else p_gauss(c, mu, g_params[sid][1], g_params[sid][0]))
    base = sum(outs) / len(outs)

    print("=" * 64)
    print("MODEL BAKEOFF — true forecast, real outcomes, out-of-sample")
    print("=" * 64)
    print(f"test markets {len(outs)} | base rate YES {base:.1%} | naive Brier {base*(1-base):.4f}")
    print("-" * 64)
    for name, ps in (("Gaussian ", test_g), ("Empirical", test_e), ("Student-t", test_s)):
        print(f"{name}: Brier {brier_score(ps, outs):.4f} | logloss {log_loss(ps, outs):.4f}")
    print("-" * 64)
    print("Shoulder bucket [0.1,0.2] (the tradeable zone) by model:")
    for name, ps in (("Gaussian ", test_g), ("Empirical", test_e), ("Student-t", test_s)):
        b12 = [b for b in calibration_table(ps, outs, 10) if b["bucket"] == "[0.1,0.2)"]
        if b12:
            b = b12[0]
            print(f"   {name}: n={b['count']:>4} predicted={b['avg_predicted']:.3f} observed={b['observed_freq']:.3f}")
    print("=" * 64)


if __name__ == "__main__":
    main()

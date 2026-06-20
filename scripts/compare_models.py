"""Out-of-sample test: empirical fat-tailed error model vs tuned Gaussian.

Fits each model on OLDER settled days and evaluates on RECENT settled days
(temporal split — no leakage). Shows whether a non-parametric forecast-error
distribution fixes the [0.1,0.2] miscalibration the Gaussian cannot.

Run: python scripts/compare_models.py
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

SERIES_STATION = {"KXHIGHNY": "nyc", "KXHIGHCHI": "chi", "KXHIGHLAX": "lax",
                  "KXHIGHMIA": "mia", "KXHIGHAUS": "aus", "KXHIGHDEN": "den",
                  "KXHIGHPHIL": "phl"}
_session = requests.Session()
_cache: dict = {}


def _ometeo(host, station, start, end):
    key = (host, station.id, start, end)
    if key in _cache:
        return _cache[key]
    base = "v1/forecast" if "forecast" in host else "v1/archive"
    try:
        r = _session.get(f"https://{host}/{base}", params={
            "latitude": station.latitude, "longitude": station.longitude,
            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
            "timezone": station.tz, "start_date": start.isoformat(),
            "end_date": end.isoformat()}, timeout=30)
        r.raise_for_status()
        d = r.json().get("daily", {})
        out = dict(zip(d.get("time", []), d.get("temperature_2m_max", [])))
    except requests.RequestException:
        out = {}
    _cache[key] = out
    return out


def _empirical_prob(cond, mu, errors):
    """P(YES) from the empirical distribution of (mu + error)."""
    import random
    n = 0
    hit = 0
    for e in errors:
        # light kernel smoothing to avoid step artifacts
        val = mu + e + random.gauss(0, 0.7)
        t = cond.threshold
        if cond.operator in ("gte", "gt"):
            ok = val >= t
        elif cond.operator in ("lte", "lt"):
            ok = val <= t
        elif cond.operator == "between" and cond.threshold2 is not None:
            lo, hi = sorted((t, cond.threshold2))
            ok = lo <= val <= hi
        else:
            ok = val >= t
        n += 1
        hit += int(ok)
    return hit / n if n else 0.5


def _gauss_prob(cond, mu, sigma, bias):
    m = NormalForecastModel(temp_sigma_by_horizon={1: sigma})
    fc = Forecast(cond.location_text, cond.target_date, "f", 1,
                  high_temp_f=mu + bias, low_temp_f=mu + bias)
    return m.estimate(cond, fc, horizon_days=1).fair_yes


def main(days_back=12):
    import random
    random.seed(0)
    cfg = load_config()
    client = KalshiClient(cfg)
    if client.mock:
        print("Live access required.")
        return

    rows, needed = [], defaultdict(list)
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
            rows.append((sid, cond, cond.target_date, 1 if res == "yes" else 0))
            needed[sid].append(cond.target_date)

    fcst, actual = {}, {}
    for sid, dts in needed.items():
        st = STATIONS[sid]
        fcst[sid] = _ometeo("historical-forecast-api.open-meteo.com", st, min(dts), max(dts))
        actual[sid] = _ometeo("archive-api.open-meteo.com", st, min(dts), max(dts))

    # Temporal split: older half = train (build error sample), newer half = test.
    all_dates = sorted({d for _, _, d, _ in rows})
    split = all_dates[len(all_dates) // 2]
    # Per-station-day errors (dedup so a day isn't weighted by # of strikes).
    seen = set()
    errors = []
    for sid, cond, d, _ in rows:
        if d >= split:
            continue
        if (sid, d) in seen:
            continue
        seen.add((sid, d))
        mu = fcst.get(sid, {}).get(d.isoformat())
        ac = actual.get(sid, {}).get(d.isoformat())
        if mu is not None and ac is not None:
            errors.append(ac - mu)

    print("=" * 60)
    print("OUT-OF-SAMPLE: EMPIRICAL fat-tail model vs TUNED GAUSSIAN")
    print("=" * 60)
    print(f"train days < {split} ({len(errors)} station-days of error)")
    print(f"error sample: mean {sum(errors)/len(errors):+.2f}F, "
          f"min {min(errors):+.1f}, max {max(errors):+.1f}")
    print("-" * 60)

    g_probs, e_probs, outs = [], [], []
    for sid, cond, d, outcome in rows:
        if d < split:
            continue
        mu = fcst.get(sid, {}).get(d.isoformat())
        if mu is None:
            continue
        g_probs.append(_gauss_prob(cond, mu, 2.0, 1.0))
        e_probs.append(_empirical_prob(cond, mu + 1.0, errors))
        outs.append(outcome)

    def report(name, probs):
        print(f"{name}: Brier {brier_score(probs, outs):.4f} | "
              f"logloss {log_loss(probs, outs):.4f}")
        for b in calibration_table(probs, outs, 5):
            print(f"     {b['bucket']:>10} n={b['count']:>4} pred={b['avg_predicted']:.3f} "
                  f"obs={b['observed_freq']:.3f}")

    print(f"TEST set: {len(outs)} markets")
    report("Gaussian(bias+1,sig2)", g_probs)
    print()
    report("Empirical fat-tail   ", e_probs)
    print("=" * 60)


if __name__ == "__main__":
    main()

"""Fast per-station Gaussian (bias, sigma) fit on the TRUE forecast, for deploy.

Prints the params to put into config (model.station_bias_f). Uses the day-of
Open-Meteo forecast as the mean and Kalshi settlement as the label, fit on older
days and validated on held-out recent days.
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

from src.backtest.metrics import brier_score, log_loss  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402
from src.models.contract_parser import parse_contract  # noqa: E402
from src.utils.config import load_config  # noqa: E402
from src.weather.stations import STATIONS  # noqa: E402

SERIES_STATION = {"KXHIGHNY": "nyc", "KXHIGHCHI": "chi", "KXHIGHLAX": "lax",
                  "KXHIGHMIA": "mia", "KXHIGHAUS": "aus", "KXHIGHDEN": "den",
                  "KXHIGHPHIL": "phl"}
_s = requests.Session()


def _cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p(cond, mu, sigma, bias):
    s = max(sigma, 1e-6)
    m = mu + bias
    below = _cdf((cond.threshold - m) / s)
    if cond.operator in ("gte", "gt"):
        return 1 - below
    if cond.operator in ("lte", "lt"):
        return below
    if cond.operator == "between" and cond.threshold2 is not None:
        lo, hi = sorted((cond.threshold, cond.threshold2))
        return _cdf((hi - m) / s) - _cdf((lo - m) / s)
    return 1 - below


def fcst(station, lo, hi):
    try:
        r = _s.get("https://historical-forecast-api.open-meteo.com/v1/forecast", params={
            "latitude": station.latitude, "longitude": station.longitude,
            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
            "timezone": station.tz, "start_date": lo.isoformat(), "end_date": hi.isoformat()},
            timeout=60)
        d = r.json().get("daily", {})
        return dict(zip(d.get("time", []), d.get("temperature_2m_max", [])))
    except requests.RequestException:
        return {}


def main():
    client = KalshiClient(load_config())
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

    joined = defaultdict(list)
    for sid, rows in recs.items():
        fc = fcst(STATIONS[sid], min(span[sid]), max(span[sid]))
        for c, d, o in rows:
            mu = fc.get(d.isoformat())
            if mu is not None:
                joined[sid].append((c, mu, d, o))

    all_dates = sorted({d for sid in joined for (_, _, d, _) in joined[sid]})
    split = all_dates[int(len(all_dates) * 0.7)]
    biases = [round(x * 0.5, 1) for x in range(-8, 9)]
    sigmas = [round(x * 0.5, 1) for x in range(4, 21)]

    print(f"{'station':>8}  {'bias':>5} {'sigma':>5}   test_Brier")
    params = {}
    test_p, test_o = [], []
    for sid, rows in joined.items():
        train = [(c, mu, o) for (c, mu, d, o) in rows if d < split]
        test = [(c, mu, o) for (c, mu, d, o) in rows if d >= split]
        if len(train) < 40 or not test:
            continue
        best = min(((b, s) for b in biases for s in sigmas),
                   key=lambda bs: log_loss([p(c, mu, bs[1], bs[0]) for c, mu, _ in train],
                                           [o for *_, o in train]))
        params[sid] = best
        tp = [p(c, mu, best[1], best[0]) for c, mu, _ in test]
        to = [o for *_, o in test]
        test_p += tp
        test_o += to
        print(f"{sid:>8}  {best[0]:>+5.1f} {best[1]:>5.1f}   {brier_score(tp, to):.4f}")
    print("-" * 40)
    print(f"overall test Brier: {brier_score(test_p, test_o):.4f}  "
          f"(naive {sum(test_o)/len(test_o)*(1-sum(test_o)/len(test_o)):.4f})")
    print("\nconfig model.station_bias_f:")
    for sid, (b, s) in sorted(params.items()):
        print(f"    {sid}: {b}")


if __name__ == "__main__":
    main()

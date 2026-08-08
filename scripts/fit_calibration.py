"""Fit a probability calibration layer against real settled Kalshi outcomes.

The model is measurably overconfident: across settled markets it predicts ~16%
where ~36% actually occur, and across settled paper trades it has claimed ~86%
and delivered ~74%. Overconfident probabilities poison everything downstream --
the claimed edge, the EV after fees, and which signals clear the alert filter --
so the fix belongs at the probability, not at the threshold.

This fits Platt scaling in log-odds space::

    p_calibrated = sigmoid(a * logit(p_raw) + b)

Two parameters only, so it cannot meaningfully overfit a few hundred markets.
``a < 1`` shrinks the model toward 50% (the cure for overconfidence); ``b``
corrects a systematic directional lean.

Fitted on the OLDER portion of history and scored on the newer, held-out
portion, so the reported improvement is genuinely out-of-sample. Prints the
config block to paste under ``model.calibration``; if calibration does NOT beat
the raw model on held-out data, it says so and recommends leaving it off.

Run: python scripts/fit_calibration.py
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.metrics import brier_score, log_loss  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402
from src.models.contract_parser import parse_contract  # noqa: E402
from src.models.ensemble import build_model  # noqa: E402
from src.utils.config import load_config  # noqa: E402
from src.weather.base import Forecast  # noqa: E402
from src.weather.stations import STATIONS  # noqa: E402

SERIES_STATION = {"KXHIGHNY": "nyc", "KXHIGHCHI": "chi", "KXHIGHLAX": "lax",
                  "KXHIGHMIA": "mia", "KXHIGHAUS": "aus", "KXHIGHDEN": "den",
                  "KXHIGHPHIL": "phl"}
_s = requests.Session()
EPS = 1e-6


def _cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _logit(p: float) -> float:
    p = min(max(p, EPS), 1.0 - EPS)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def raw_prob(model, cond, station_id: str, mu: float, target_date) -> float:
    """The PRODUCTION model's uncalibrated P(YES).

    Deliberately calls the real configured model rather than re-deriving a
    normal CDF here: a calibration fitted against a simplified stand-in would
    be correcting a model that never runs, and applying it to the real one
    could easily make accuracy worse rather than better.
    """
    fc = Forecast(location_id=station_id, target_date=target_date,
                  source="historical", horizon_days=0, high_temp_f=mu)
    return float(model.estimate(cond, fc, 0).fair_yes)


def apply_cal(p: float, a: float, b: float) -> float:
    return _sigmoid(a * _logit(p) + b)


def forecasts(station, lo, hi) -> dict:
    try:
        r = _s.get("https://historical-forecast-api.open-meteo.com/v1/forecast", params={
            "latitude": station.latitude, "longitude": station.longitude,
            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
            "timezone": station.tz, "start_date": lo.isoformat(),
            "end_date": hi.isoformat()}, timeout=60)
        d = r.json().get("daily", {})
        return dict(zip(d.get("time", []), d.get("temperature_2m_max", [])))
    except requests.RequestException:
        return {}


def collect(config) -> list:
    """Return [(raw_prob, outcome, date)] over settled markets."""
    client = KalshiClient(config)
    # Fit against the RAW model. Building with calibration enabled would fit a
    # correction on top of an already-corrected probability, compounding it a
    # little more with every re-fit.
    raw_config = load_config()
    cal_cfg = raw_config.get("model.calibration", {}) or {}
    if cal_cfg.get("enabled", False):
        cal_cfg["enabled"] = False
        print("(calibration currently enabled - fitting against the RAW model)")
    model = build_model(raw_config)
    recs, span = defaultdict(list), defaultdict(list)
    for series, sid in SERIES_STATION.items():
        cursor, pages = None, 0
        while pages < 30:
            par = {"series_ticker": series, "status": "settled", "limit": 1000}
            if cursor:
                par["cursor"] = cursor
            d = client._get("/markets", params=par)
            markets = d.get("markets", [])
            for m in markets:
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
            if not cursor or not markets:
                break

    rows = []
    for sid, items in recs.items():
        if not span[sid]:
            continue
        fc = forecasts(STATIONS[sid], min(span[sid]), max(span[sid]))
        for c, date, outcome in items:
            mu = fc.get(date.isoformat())
            if mu is None:
                continue
            rows.append((raw_prob(model, c, sid, mu, date), outcome, date))
    return rows


def main() -> int:
    config = load_config()
    rows = collect(config)
    if len(rows) < 100:
        print(f"Only {len(rows)} usable settled markets - too few to calibrate.")
        return 1

    dates = sorted({d for _, _, d in rows})
    split = dates[int(len(dates) * 0.7)]
    train = [(p, o) for p, o, d in rows if d < split]
    test = [(p, o) for p, o, d in rows if d >= split]
    if len(train) < 60 or len(test) < 30:
        print(f"Split too thin (train {len(train)}, test {len(test)}).")
        return 1

    print("=" * 62)
    print("PROBABILITY CALIBRATION vs REAL SETTLED OUTCOMES")
    print("=" * 62)
    print(f"markets: {len(rows)}  train {len(train)} (< {split})  "
          f"test {len(test)} (>= {split})")

    # Grid search Platt parameters on the TRAINING split only. The grid is wide
    # enough that the optimum should land inside it; a fit sitting on an edge is
    # reported as untrustworthy rather than quietly adopted.
    A_LO, A_HI, B_LO, B_HI = 0.10, 2.50, -3.00, 3.00
    best, best_ll = (1.0, 0.0), float("inf")
    for ai in range(int(A_LO * 100), int(A_HI * 100) + 1, 5):
        a = ai / 100.0
        for bi in range(int(B_LO * 100), int(B_HI * 100) + 1, 5):
            b = bi / 100.0
            ll = log_loss([apply_cal(p, a, b) for p, _ in train],
                          [o for _, o in train])
            if ll < best_ll:
                best_ll, best = ll, (a, b)
    a, b = best
    on_edge = (abs(a - A_LO) < 1e-9 or abs(a - A_HI) < 1e-9
               or abs(b - B_LO) < 1e-9 or abs(b - B_HI) < 1e-9)

    raw_test = [p for p, _ in test]
    cal_test = [apply_cal(p, a, b) for p, _ in test]
    obs_test = [o for _, o in test]
    raw_ll, cal_ll = log_loss(raw_test, obs_test), log_loss(cal_test, obs_test)
    raw_br, cal_br = brier_score(raw_test, obs_test), brier_score(cal_test, obs_test)

    print("-" * 62)
    print(f"fitted: a={a:.2f}  b={b:+.2f}"
          f"   ({'shrinks toward 50%' if a < 1 else 'sharpens'})")
    print("-" * 62)
    print(f"{'':<12}{'logloss':>10}{'Brier':>10}   (held-out)")
    print(f"{'raw':<12}{raw_ll:>10.4f}{raw_br:>10.4f}")
    print(f"{'calibrated':<12}{cal_ll:>10.4f}{cal_br:>10.4f}")

    better = cal_ll < raw_ll and cal_br <= raw_br
    print("-" * 62)
    if on_edge:
        print("WARNING: the fit landed on the edge of the search grid, so the")
        print("optimum may lie outside it. Treat these parameters as unreliable")
        print("and widen the grid before adopting them.")
        return 1
    if better:
        print("Calibration IMPROVES held-out accuracy. Recommended config:\n")
        print("model:\n  calibration:\n    enabled: true")
        print(f"    a: {a:.2f}\n    b: {b:+.2f}")
    else:
        print("Calibration does NOT beat the raw model out-of-sample.")
        print("Leave model.calibration.enabled at false. Re-run as data grows.")

    # How the overconfident region actually moves.
    print("\nEffect on representative probabilities:")
    for p in (0.05, 0.10, 0.16, 0.30, 0.70, 0.85, 0.90, 0.95):
        print(f"   model says {p:>5.0%}  ->  calibrated {apply_cal(p, a, b):>5.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

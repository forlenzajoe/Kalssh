"""Historical calibration check against REAL settled Kalshi outcomes.

For each settled Kalshi high-temperature market this script:
  1. reads Kalshi's actual settlement (`result` = yes/no) — the ground truth;
  2. pulls the ACTUAL observed daily high (Open-Meteo archive) to verify our
     contract parsing + station mapping reproduces that settlement;
  3. pulls the day-of FORECAST (Open-Meteo historical-forecast) and runs the
     probability model to get a predicted P(YES);
  4. scores predictions vs outcomes: accuracy, Brier, log loss, and a
     calibration table (does "70%" actually happen ~70% of the time?).

This is the honest test of whether the model works. Run:
    python scripts/historical_calibration.py
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
from src.backtest.engine import _condition_true, _observed_value  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402
from src.models.contract_parser import parse_contract  # noqa: E402
from src.models.forecast_model import NormalForecastModel  # noqa: E402
from src.utils.config import load_config  # noqa: E402
from src.weather.base import Forecast  # noqa: E402
from src.weather.stations import STATIONS  # noqa: E402

# Kalshi high-temp series -> our station id.
SERIES_STATION = {
    "KXHIGHNY": "nyc", "KXHIGHCHI": "chi", "KXHIGHLAX": "lax", "KXHIGHMIA": "mia",
    "KXHIGHAUS": "aus", "KXHIGHDEN": "den", "KXHIGHPHIL": "phl",
}

_session = requests.Session()
_cache: dict = {}


def _openmeteo(host: str, station, start: date, end: date) -> dict:
    """Return {date_iso: max_temp_f} from an Open-Meteo daily endpoint."""
    key = (host, station.id, start, end)
    if key in _cache:
        return _cache[key]
    try:
        r = _session.get(
            f"https://{host}/v1/forecast" if "forecast" in host else f"https://{host}/v1/archive",
            params={
                "latitude": station.latitude, "longitude": station.longitude,
                "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
                "timezone": station.tz, "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
            timeout=30,
        )
        r.raise_for_status()
        daily = r.json().get("daily", {})
        out = dict(zip(daily.get("time", []), daily.get("temperature_2m_max", [])))
    except requests.RequestException:
        out = {}
    _cache[key] = out
    return out


def main(days_back: int = 10) -> None:
    cfg = load_config()
    client = KalshiClient(cfg)
    if client.mock:
        print("Live Kalshi access required (set mode: live). Aborting.")
        return
    model = NormalForecastModel(
        temp_sigma_by_horizon=cfg.get("weather.temp_error_sigma_f"),
    )

    # 1) Collect settled markets per city.
    records = []  # (station, date, condition, outcome)
    needed_dates: dict[str, list] = defaultdict(list)
    for series, sid in SERIES_STATION.items():
        station = STATIONS[sid]
        try:
            data = client._get("/markets", params={"series_ticker": series,
                                                   "status": "settled", "limit": 200})
        except requests.RequestException:
            continue
        for m in data.get("markets", []):
            result = (m.get("result") or "").lower()
            if result not in ("yes", "no"):
                continue
            cond = parse_contract(m.get("title", ""), subtitle=m.get("yes_sub_title", ""),
                                  rules=m.get("rules_primary", ""))
            if cond.target_date is None or cond.threshold is None:
                continue
            if (date.today() - cond.target_date).days > days_back:
                continue
            records.append((station, cond.target_date, cond, 1 if result == "yes" else 0))
            needed_dates[sid].append(cond.target_date)

    if not records:
        print("No recent settled markets found.")
        return

    # 2) Batch-fetch actuals (archive) and forecasts (historical-forecast) per station.
    actual_by, fcst_by = {}, {}
    for sid, dts in needed_dates.items():
        station = STATIONS[sid]
        lo, hi = min(dts), max(dts)
        actual_by[sid] = _openmeteo("archive-api.open-meteo.com", station, lo, hi)
        fcst_by[sid] = _openmeteo("historical-forecast-api.open-meteo.com", station, lo, hi)

    # 3) Score.
    settle_match = settle_total = 0
    probs, outcomes = [], []
    for station, tgt, cond, outcome in records:
        iso = tgt.isoformat()
        actual = actual_by.get(station.id, {}).get(iso)
        fcst = fcst_by.get(station.id, {}).get(iso)

        # Settlement reproduction (parsing/station/units correctness).
        if actual is not None:
            obs = Forecast(location_id=station.id, target_date=tgt, source="archive",
                           horizon_days=0, high_temp_f=actual, low_temp_f=actual)
            predicted_yes = _condition_true(cond, _observed_value(cond, obs))
            if predicted_yes is not None:
                settle_total += 1
                if int(predicted_yes) == outcome:
                    settle_match += 1

        # Probability calibration (model on the day-of forecast).
        if fcst is not None:
            fc = Forecast(location_id=station.id, target_date=tgt, source="histfcst",
                          horizon_days=1, high_temp_f=fcst, low_temp_f=fcst)
            out = model.estimate(cond, fc, horizon_days=1)
            probs.append(out.fair_yes)
            outcomes.append(outcome)

    # 4) Report.
    print("=" * 60)
    print("HISTORICAL CALIBRATION vs REAL SETTLED KALSHI OUTCOMES")
    print("=" * 60)
    print(f"Settled markets scored : {len(records)}")
    print(f"Date range             : last {days_back} days")
    print("-" * 60)
    print("A) SETTLEMENT REPRODUCTION (parsing + station + units correct?)")
    if settle_total:
        print(f"   actual-high reproduces Kalshi result: {settle_match}/{settle_total} "
              f"= {settle_match / settle_total:.1%}")
    else:
        print("   no actuals available")
    print("-" * 60)
    print("B) MODEL PROBABILITY CALIBRATION (day-of forecast -> P(YES))")
    if probs:
        preds = [1 if p >= 0.5 else 0 for p in probs]
        acc = sum(int(a == b) for a, b in zip(preds, outcomes)) / len(preds)
        print(f"   predictions          : {len(probs)}")
        print(f"   directional accuracy : {acc:.1%}")
        print(f"   Brier score          : {brier_score(probs, outcomes):.4f}  (0=perfect, 0.25=coin flip)")
        print(f"   log loss             : {log_loss(probs, outcomes):.4f}")
        print(f"   base rate (YES)      : {sum(outcomes) / len(outcomes):.1%}")
        print()
        print(f"   {'bucket':>12} {'count':>6} {'predicted':>10} {'observed':>9}")
        for b in calibration_table(probs, outcomes, n_buckets=10):
            print(f"   {b['bucket']:>12} {b['count']:>6} {b['avg_predicted']:>10.3f} "
                  f"{b['observed_freq']:>9.3f}")
    else:
        print("   no forecasts available")
    print("=" * 60)


if __name__ == "__main__":
    main()

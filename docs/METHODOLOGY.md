# Probability Methodology

This document explains exactly how the scanner turns a Kalshi weather contract
and public weather data into a fair probability, an edge, and a recommendation.
The guiding principle is **transparency over cleverness**: every number can be
traced and questioned.

---

## 1. From contract text to a measurable event

`src/models/contract_parser.py` parses a market's title/subtitle/rules into an
`EventCondition`:

```
variable  ∈ {high_temp, low_temp, rainfall, snowfall, temp_range, wind}
operator  ∈ {gt, gte, lt, lte, between}
threshold (and threshold2 for "between")
unit      ∈ {F, in, mph}
location_text, target_date
ambiguous (bool) + notes
```

Example:

> "Will the high temperature in NYC be above 80F on June 18, 2026?"
> → `high_temp ≥ 80F`, location "nyc", date 2026-06-18.

Anything the parser cannot resolve confidently (missing threshold, unclear
direction, unknown variable) sets `ambiguous = True` with an explanatory note.
Parse ambiguity reduces **model confidence**; an unresolved **settlement
station** (separately, in `src/scanner.py`) is treated as disqualifying.

---

## 2. Settlement matching

`src/weather/stations.py` maps the location text to a `Station` (city + NWS
station id + coordinates). Kalshi temperature markets settle off a specific
station (often the city's primary airport/Central Park), so matching the right
station is essential. If no station matches, the market is flagged
**ambiguous settlement** and avoided by the risk layer.

---

## 3. The transparent baseline model

`src/models/forecast_model.py` (`NormalForecastModel`) estimates the fair YES
probability in three steps.

### (a) Point forecast `μ`
Pulled from a `Forecast` for the contract variable:
- `high_temp` → forecast daily high
- `low_temp` → forecast daily low
- `temp_range` → `high − low`
- `rainfall` → forecast precipitation (inches)
- `snowfall` → forecast snowfall (inches)
- `wind` → forecast max wind (mph)

### (b) Forecast uncertainty `σ`
The realized value is uncertain. We model that uncertainty with a
**horizon-indexed standard deviation** — i.e. the historical 1-sigma forecast
error at that lead time, configured in `weather.temp_error_sigma_f`:

| Horizon (days) | σ (°F) |
| --- | --- |
| 0 | 2.5 |
| 1 | 3.0 |
| 2 | 4.0 |
| 3 | 5.0 |
| … | … |
| 7 | 9.0 |

Longer horizons → wider distributions → probabilities pulled toward 0.5.
Derived quantities:
- `temp_range`: `σ = σ_temp · √2` (difference of two roughly independent temps).
- `rainfall`/`snowfall`: `σ = max(0.35·|μ|, 0.1)` — a crude magnitude-scaled
  spread (see limitations).
- `wind`: `σ = max(0.25·|μ|, 2.0)`.

### (c) Probability curve around the threshold
Model the realized value `X ~ Normal(μ, σ)` and integrate the relevant region.
With `Φ` the standard-normal CDF and `z = (threshold − μ) / σ`:

| Operator | Fair YES probability |
| --- | --- |
| `≥` / `>` | `1 − Φ(z)` |
| `≤` / `<` | `Φ(z)` |
| `between [a,b]` | `Φ((b−μ)/σ) − Φ((a−μ)/σ)` |

This yields a smooth probability that respects **how far** the forecast sits
from the strike and **how uncertain** it is. `fair_no = 1 − fair_yes`.

### (d) Confidence
Starts at 1.0 and is multiplied down by:
- long horizon (beyond `model.confidence.long_horizon_days`),
- parse ambiguity (`ambiguous_settlement_penalty`),
- skewed precip variables (×0.7).

`confidence = 0` whenever there is no forecast or no threshold, which forces the
market to be skipped.

---

## 4. Implied probability, fees, and slippage

`src/kalshi/pricing.py` uses **executable** prices, not last price:
- Buy YES → pay the YES ask. Implied `P(YES) = yes_ask / 100`.
- Buy NO → pay the NO ask `= (100 − yes_bid) / 100`.

Kalshi's trading fee is modeled as
`fee = ceil(fee_rate · contracts · p · (1 − p))` (rounded up to whole cents,
`fee_rate = 0.07`), and we add a configurable `slippage_cents` cushion to the
crossing price. The **breakeven probability** is `total_cost / contracts`.

---

## 5. Edge and expected value

`src/edge.py` computes, for each side:

```
gross_edge = fair_prob − executable_price
EV_per_contract = fair_prob − total_cost   (total_cost includes fee + slippage)
```

It picks the side with the higher EV. EV after fees — not raw edge — is the
decision variable.

---

## 6. Risk gating and sizing

`src/risk.py` applies conservative gates. A market becomes **Buy** only if it
clears all of: `min_edge`, `min_ev_after_fees`, `min_confidence`,
`min_liquidity_score`, `max_spread_cents`, and (globally) the
`max_daily_exposure_usd` cap. Ambiguous settlement or non-positive EV → **Avoid**.
Other failures → **Watch**.

Position size uses **fractional Kelly**. For a binary contract bought at cost
`c` paying `$1` on a win with probability `q`, the full-Kelly fraction of
bankroll is `(q − c) / (1 − c)`; we scale by `kelly_fraction` (default 0.25) and
cap at `max_position_per_market_usd`.

---

## 7. Calibration & backtesting

`src/backtest/` scores predictions with:
- **Calibration table** — bucket predictions by probability and compare the
  average predicted probability to the observed frequency. A well-calibrated
  model tracks the diagonal.
- **Brier score** — mean squared error of probabilities (0 = perfect).
- **Log loss** — penalizes confident wrong predictions.
- Strategy metrics — win rate, average edge, realized return, max drawdown,
  P&L after fees.

Calibration is the single most important diagnostic: an "edge" from a
mis-calibrated model is an illusion.

---

## Limitations

1. **Precip/snow are skewed and non-negative**; the normal approximation is
   crude. Confidence is reduced, but a censored/gamma or ML model is the right
   fix. The `sklearn` hook in `src/models/ensemble.py` exists for this.
2. **`temp_error_sigma_f` is generic.** Calibrate it from archived
   forecast-vs-observed data for your stations and sources.
3. **Synthetic backtest is illustrative.** Mock observations are derived from
   forecasts, so its accuracy/return overstate reality. Use the CSV path with
   real archived data for honest numbers.
4. **No real Kalshi history via API in v1.** Build a dataset by persisting
   scans, or import CSVs.
5. **Station registry is small** (~10 US cities). Unmapped markets are avoided.
6. **Single point forecast per source.** Ensembling multiple sources and using
   their disagreement as an uncertainty signal is a clear next step.
7. **Live auth is implemented but unexercised** here — validate against Kalshi's
   demo environment before relying on live data.

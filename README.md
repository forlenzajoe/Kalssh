# Kalshi Weather Mispricing Scanner

A research, backtesting, signal-generation, and **paper-trading** system that
looks for potentially positive-expected-value Kalshi **weather** contracts by
comparing Kalshi market prices against independently estimated fair
probabilities derived from public weather data (NOAA/NWS, extensible to
Open-Meteo, Meteostat, etc.).

> ⚠️ **No live trading.** v1 is research/paper only. There is a hard
> `trading.paper_only` guard and **no order-placement code exists**. The system
> is deliberately conservative: it penalizes wide spreads, thin liquidity,
> ambiguous settlement rules, and low-confidence estimates, and assumes not
> every apparent edge is real.

---

## What it does

1. **Kalshi integration** — pulls active markets, filters to weather, reads
   order books / bid-ask / last / volume / liquidity / close time / settlement
   rules, and computes **implied probabilities from executable bid/ask**, with
   explicit fee + spread + slippage assumptions.
2. **Weather data** — modular sources (NWS built in; Open-Meteo adapter
   included) behind a registry, matched to the correct settlement station, with
   ambiguous matches flagged.
3. **Probability model** — parses contract language into measurable conditions
   and estimates a fair YES/NO probability with a transparent point-forecast +
   uncertainty model. An extensible model layer supports logistic regression,
   gradient boosting, random forest, Bayesian, or ensemble models.
4. **Edge calculation** — best YES bid/ask, implied prob, fair prob, gross edge,
   EV after fees, spread cost, liquidity score, time to settlement, confidence,
   a recommended action (Buy YES / Buy NO / Watch / Avoid), and a risk-based
   max position size.
5. **Backtesting** — calibration by probability bucket, Brier score, log loss,
   win rate, average edge, realized return, drawdown, P&L after fees, plus
   charts and a summary report. Supports a synthetic offline backtest and CSV
   import of archived data.
6. **Paper trading** — logs hypothetical trades (entry, fair value, market,
   timestamp, outcome, realized P&L, notes) to SQLite or CSV.
7. **Dashboard** — a local Streamlit app for opportunities, paper-trade history,
   and model performance.
8. **Risk controls** — configurable edge / liquidity / spread / position /
   exposure limits; avoids ambiguous and low-confidence markets; paper-only by
   default.

---

## Project structure

```
.
├── README.md
├── pyproject.toml / requirements.txt
├── .env.example                 # copy to .env (optional; mock mode needs nothing)
├── config.yaml                  # all tunable settings
├── docs/METHODOLOGY.md          # probability methodology + limitations
├── data/
│   ├── mock/                    # (bundled mock data is generated in code)
│   └── sample_backtest.csv      # example CSV for `backtest --csv`
├── src/
│   ├── cli.py                   # command-line entry point
│   ├── scanner.py               # orchestration
│   ├── edge.py                  # edge / EV calculation
│   ├── risk.py                  # risk gates + position sizing
│   ├── kalshi/                  # client, models, pricing, weather filter
│   ├── weather/                 # base, registry, stations, noaa, open_meteo
│   ├── models/                  # contract parser + probability models
│   ├── backtest/                # metrics, engine, report
│   ├── paper_trading/           # engine + sqlite/csv store
│   ├── dashboard/app.py         # Streamlit dashboard
│   └── utils/                   # config, logging, mock_data
└── tests/                       # parsing, pricing, probability, edge, risk, e2e
```

---

## Installation

Requires **Python 3.10+**.

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt
#   (or:  pip install -e ".[dev,ml]"  to get the console script + ML/test extras)
```

> The project ships with bundled **mock data**, so it runs end-to-end with **no
> API keys**. To go live later, copy `.env.example` to `.env`, fill in
> credentials, and set `mode: live` in `config.yaml`.

---

## How to run

All commands default to **mock mode**. Replace `python -m src.cli` with the
`kalshi-scan` console script if you installed with `pip install -e .`.

### Run the scanner

```bash
python -m src.cli scan                 # print ranked opportunities
python -m src.cli scan --top 10        # show only the top 10
python -m src.cli scan --paper         # also log paper trades for Buy signals
```

### Run the dashboard

**As a desktop app (recommended):** create the desktop shortcut once, then just
double-click it — it opens in its own window, no browser needed:

```powershell
powershell -ExecutionPolicy Bypass -File create_desktop_shortcut.ps1
# then double-click "Kalshi Weather Scanner" on your Desktop
# (or run directly:)  .venv\Scripts\pythonw.exe desktop_app.py
```

**As a browser tab:**

```bash
streamlit run src/dashboard/app.py
# then open the printed Local URL (e.g. http://localhost:8501)
```

The dashboard has a **🔄 Refresh now** button and an **Auto-refresh** selector
(30s / 60s / 5 min) for near-real-time updates, plus a 🟢 LIVE / 🟡 MOCK banner.

### Live vs mock mode

`mode` in `config.yaml` controls data:

- `mode: live` — real Kalshi weather **markets** (metadata) + real **NWS
  weather** forecasts/observations, including the **intraday edge**. Falls back
  to mock automatically if the network fails.
- `mode: mock` — bundled offline fixtures for demos/tests.

> **Important — Kalshi prices require your API key.** Kalshi's market *metadata*
> (which markets exist, thresholds, settlement rules) is public, but live
> **prices, order books, and volume are gated behind authentication**. Without a
> key the scanner can pull markets and compute fair values, but it will show no
> executable price (every market becomes "Avoid"). To get prices, create a
> Kalshi API key and set `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` in
> `.env` (see below). NWS weather is genuinely key-less.

#### Getting a Kalshi API key (for live prices)

1. Log in at [kalshi.com](https://kalshi.com) → Account → **API Keys**.
2. Create a key; download the **private key** `.pem` file and copy the **key ID**.
3. In `.env` set:
   ```
   KALSHI_API_KEY_ID=your-key-id
   KALSHI_PRIVATE_KEY_PATH=C:\path\to\kalshi_private_key.pem
   ```
4. `pip install cryptography` (needed for request signing), then run live.

### Run a backtest

```bash
# Synthetic offline backtest over the bundled mock markets:
python -m src.cli backtest --days 30 --html reports/backtest.html

# Backtest from your own archived CSV (columns: fair_prob,entry_price,outcome
# [,ticker,date,side,contracts,edge]):
python -m src.cli backtest --csv data/sample_backtest.csv
```

### Paper-trade lifecycle

```bash
python -m src.cli scan --paper         # log signals
python -m src.cli history              # list trades (note the IDs)
python -m src.cli settle <TRADE_ID> yes   # settle an outcome -> realized P&L
```

### Run the tests

```bash
pytest -q
```

---

## How to interpret the output

The scan table has one row per market:

| Column | Meaning |
| --- | --- |
| `ACTION` | **Buy YES / Buy NO** = cleared every risk gate and is +EV; **Watch** = promising but failed a soft gate (edge/liquidity/spread/exposure); **Avoid** = disqualifying problem (ambiguous settlement, negative EV). |
| `FAIR` | Model fair probability of **YES**. |
| `IMPL` | Executable implied prob = cost to **buy YES** (the YES ask in dollars). |
| `EDGE` | `fair − executable implied` on the recommended side (gross, before fees). |
| `EV$` | Expected value **per contract after fees + slippage** (dollars). This is the number that actually matters. |
| `CONF` | Model confidence 0–1 (penalized for long horizon, parse ambiguity, skewed precip variables). |
| `LIQ` | Liquidity score 0–1 (spread + depth + volume). |
| `SPR` | Bid/ask spread in cents. |
| `HRS` | Hours to market close. |

**Reading it well:** a large `EDGE` with low `CONF`, low `LIQ`, a wide `SPR`, or
an ambiguous settlement flag is exactly the kind of "edge" that is probably not
real — the system downgrades these to Watch/Avoid on purpose. Trust `EV$`
together with `CONF` and `LIQ`, not raw edge.

Backtest output adds **calibration** (predicted vs observed frequency per
bucket — they should track the diagonal), **Brier score** and **log loss**
(lower is better), and realized P&L / drawdown.

---

## Configuration

Everything is in [`config.yaml`](config.yaml). Key knobs:

- `mode`: `mock` (default, offline) or `live`.
- `fees`: Kalshi fee coefficient, slippage cents, taker assumption.
- `weather.temp_error_sigma_f`: horizon → forecast error (the heart of the
  uncertainty model; refine these from your own backtest verification).
- `risk`: `min_edge`, `min_ev_after_fees`, `min_confidence`,
  `min_liquidity_score`, `max_spread_cents`, `max_position_per_market_usd`,
  `max_daily_exposure_usd`, `avoid_ambiguous_settlement`, `kelly_fraction`.

Secrets live in `.env` (see `.env.example`) and are never committed.

---

## Probability methodology & limitations

See [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) for the full write-up. In short:
realized weather is modeled as `Normal(point_forecast, horizon_sigma)` and the
fair probability is the integral of that distribution over the contract's
threshold region. It is transparent, auditable, and intentionally conservative;
the pluggable model layer is where you graduate to ML once you have backtest
data.

**Headline limitations** (full list in the methodology doc):

- The normal model is crude for **precipitation/snow** (non-negative, skewed) —
  confidence is reduced for these and they are prime ML candidates.
- `temp_error_sigma_f` ships with generic NWS-style values; **calibrate them**.
- The synthetic backtest uses mock observations derived from forecasts, so its
  win rate/return are **illustrative, not predictive**. Real edges are smaller.
- Settlement-station mapping covers ~10 major US cities; anything unmapped is
  flagged ambiguous and avoided.
- Live Kalshi auth (RSA signing) is implemented but **unexercised here** — test
  carefully against the demo environment before trusting live data.

---

## Roadmap / next improvements

1. Calibrate `temp_error_sigma_f` from archived NWS forecast-vs-observed data.
2. Replace the normal precip model with a proper PoP + amount distribution
   (e.g. censored/gamma), or an ML classifier via the existing `sklearn` hook.
3. Ensemble multiple weather sources (NWS + Open-Meteo + Meteostat).
4. Persist scans + market snapshots to build a real Kalshi historical dataset
   for honest backtesting.
5. Expand the station registry and add automated settlement-rule parsing.
6. Add slippage modeling from real order-book depth (walk the book).

---

## Disclaimer

This software is for research and education. It does not place trades, is not
financial advice, and makes no guarantee of profitability. Prediction-market
trading carries risk of loss.

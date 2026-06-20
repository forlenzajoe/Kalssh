"""End-to-end smoke tests that the mock pipeline runs and is internally sane."""

from src.backtest.engine import run_synthetic_backtest
from src.kalshi.client import KalshiClient
from src.scanner import scan


def test_scan_runs_in_mock_mode(config):
    opps = scan(config)
    assert len(opps) >= 6                         # weather markets only
    actions = {o.action for o in opps}
    assert actions & {"Buy YES", "Buy NO"}        # at least one buy
    # Non-weather markets filtered out.
    assert all("FED" not in o.ticker and "PREZ" not in o.ticker for o in opps)


def test_ambiguous_market_is_avoided(config):
    opps = {o.ticker: o for o in scan(config)}
    assert opps["HIGHXX-B90"].action == "Avoid"


def test_weather_filter_excludes_non_weather(config):
    client = KalshiClient(config)
    weather = client.get_weather_markets()
    tickers = {m.ticker for m in weather}
    assert "FED-JUL26" not in tickers
    assert "HIGHNY-B80" in tickers


def test_synthetic_backtest_produces_metrics(config):
    metrics = run_synthetic_backtest(config, days=20)
    assert metrics.n_predictions > 0
    assert 0.0 <= metrics.brier <= 1.0
    assert len(metrics.calibration) >= 1

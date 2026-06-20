"""Top-level scanner: market data -> parse -> forecast -> model -> edge -> risk.

This is the orchestration layer the CLI and dashboard call. It is deliberately
linear and easy to follow; each step is implemented in its own module.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from .edge import Opportunity, evaluate_market
from .kalshi.client import KalshiClient
from .kalshi.models import Market
from .models.contract_parser import EventCondition, parse_contract
from .models.ensemble import build_model
from .risk import RiskParams, apply_risk, enforce_daily_exposure
from .utils.config import Config
from .utils.logging import get_logger
from .weather.registry import get_source
from .weather.stations import Station, resolve_station

logger = get_logger("scanner")


def _target_date(condition: EventCondition, market: Market) -> Optional[date]:
    """Resolve the settlement date: parsed date, else the market close date."""
    if condition.target_date is not None:
        return condition.target_date
    if market.close_time is not None:
        return market.close_time.date()
    return None


def _settlement(market: Market, condition: EventCondition) -> tuple[Optional[Station], bool, list[str]]:
    """Resolve the settlement station and flag *settlement* ambiguity.

    Returns ``(station, ambiguous, notes)``. Settlement ambiguity means we
    cannot resolve the city/station the contract settles against — that is a
    disqualifying problem. Parse ambiguity (uncertain direction/threshold) is a
    separate concern handled via the model confidence penalty, not here.
    """
    notes: list[str] = []
    station = (resolve_station(market.title)
               or resolve_station(market.rules_primary)
               or resolve_station(condition.location_text))
    ambiguous = station is None
    if station is None:
        notes.append("Could not resolve a settlement station from the title.")
    else:
        notes.append(f"Matched settlement station {station.nws_station} ({station.name}).")
    if condition.ambiguous:
        notes.append("Note: contract language parse was uncertain -> "
                     "model confidence reduced.")
        notes.extend(condition.notes)
    return station, ambiguous, notes


def scan(config: Config) -> list[Opportunity]:
    """Run a full scan and return ranked opportunities (best EV first)."""
    client = KalshiClient(config)
    model = build_model(config)
    weather = get_source(config)
    risk_params = RiskParams.from_config(config)
    bankroll = config.bankroll

    fee_rate = float(config.get("fees.trade_fee_rate", 0.07))
    slippage = int(config.get("fees.slippage_cents", 1))
    assume_taker = bool(config.get("fees.assume_taker", True))
    max_spread = int(config.get("risk.max_spread_cents", 8))

    markets = client.get_weather_markets()
    markets = client.enrich_with_order_books(markets)

    opportunities: list[Opportunity] = []
    for market in markets:
        condition = parse_contract(
            market.title, subtitle=market.subtitle, rules=market.rules_primary
        )
        station, ambiguous, notes = _settlement(market, condition)
        tgt = _target_date(condition, market)

        forecast = None
        horizon = 0
        if station is not None and tgt is not None:
            horizon = max((tgt - date.today()).days, 0)
            try:
                forecast = weather.get_forecast(station, tgt)
                # Apply the per-station forecast bias correction (fit on history).
                if forecast is not None:
                    station_bias = (config.get("model.station_bias_f", {}) or {})
                    bias = float(station_bias.get(
                        station.id, config.get("model.bias_correction_f", 0.0)))
                    if bias and forecast.high_temp_f is not None:
                        forecast.high_temp_f += bias
                    if bias and forecast.low_temp_f is not None:
                        forecast.low_temp_f += bias
                # Same-day markets: enrich with live observations-so-far so the
                # intraday model can exploit an already-determined outcome.
                if forecast is not None and tgt == date.today():
                    intraday = weather.get_intraday(station)
                    if intraday is not None:
                        forecast.observed_high_so_far = intraday.observed_high_so_far
                        forecast.observed_low_so_far = intraday.observed_low_so_far
                        forecast.local_hour = intraday.local_hour
                        notes.append(
                            f"Intraday: high-so-far {intraday.observed_high_so_far:.0f}°F "
                            f"at local hour {intraday.local_hour:.1f}."
                        )
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning("Forecast fetch failed for %s: %s", market.ticker, exc)
                notes.append(f"Forecast fetch error: {exc}")

        model_out = model.estimate(condition, forecast, horizon)
        model_out.notes.append(f"event: {condition.describe()}")

        opp = evaluate_market(
            market, model_out,
            ambiguous_settlement=ambiguous,
            station_id=station.id if station else None,
            settlement_notes=notes,
            fee_rate=fee_rate,
            slippage_cents=slippage,
            assume_taker=assume_taker,
            max_spread_cents=max(max_spread, 20),
        )
        apply_risk(opp, risk_params, bankroll)
        opportunities.append(opp)

    enforce_daily_exposure(opportunities, risk_params)
    opportunities.sort(key=lambda o: (o.action.startswith("Buy"), o.ev_after_fees), reverse=True)
    logger.info(
        "Scan complete: %d markets, %d buys, %d watch, %d avoid",
        len(opportunities),
        sum(o.action.startswith("Buy") for o in opportunities),
        sum(o.action == "Watch" for o in opportunities),
        sum(o.action == "Avoid" for o in opportunities),
    )
    return opportunities

"""Extensible model layer: factory + ensemble + ML-model stubs.

The factory lets the rest of the system request a model by name from config.
The :class:`EnsembleModel` shows how to blend several models by confidence-
weighting. ML models (logistic regression, gradient boosting, random forest,
Bayesian) plug in here once trained on backtest data; until then they degrade
gracefully to the transparent baseline.
"""

from __future__ import annotations

from typing import Optional

from ..utils.config import Config
from ..weather.base import Forecast
from .base import ModelOutput, ProbabilityModel
from .contract_parser import EventCondition
from .forecast_model import NormalForecastModel


class EnsembleModel(ProbabilityModel):
    """Confidence-weighted average of several sub-models."""

    name = "ensemble"

    def __init__(self, members: list[ProbabilityModel]) -> None:
        if not members:
            raise ValueError("EnsembleModel requires at least one member model.")
        self.members = members

    def estimate(
        self,
        condition: EventCondition,
        forecast: Optional[Forecast],
        horizon_days: int,
    ) -> ModelOutput:
        outputs = [m.estimate(condition, forecast, horizon_days) for m in self.members]
        weights = [o.confidence for o in outputs]
        total = sum(weights)
        if total <= 0:
            # Fall back to a simple mean with zero confidence.
            mean = sum(o.fair_yes for o in outputs) / len(outputs)
            return ModelOutput(fair_yes=mean, confidence=0.0, model=self.name,
                               notes=["All members had zero confidence."])
        fair = sum(o.fair_yes * w for o, w in zip(outputs, weights)) / total
        conf = total / len(outputs)
        notes = [f"Ensemble of {len(outputs)} models (confidence-weighted)."]
        return ModelOutput(fair_yes=fair, confidence=conf, model=self.name, notes=notes)


class SklearnProbabilityModel(ProbabilityModel):
    """Wrapper for a trained scikit-learn classifier (logistic/GBM/RF).

    Expects a fitted estimator exposing ``predict_proba`` and a ``featurize``
    callable mapping (condition, forecast, horizon) -> feature vector. Until a
    model is trained via the backtest pipeline, instantiate with ``None`` and it
    transparently delegates to the baseline.
    """

    name = "sklearn"

    def __init__(self, estimator=None, featurize=None, fallback: Optional[ProbabilityModel] = None):
        self.estimator = estimator
        self.featurize = featurize
        self.fallback = fallback or NormalForecastModel()

    def estimate(self, condition, forecast, horizon_days) -> ModelOutput:
        if self.estimator is None or self.featurize is None or forecast is None:
            out = self.fallback.estimate(condition, forecast, horizon_days)
            out.notes.append("sklearn model not trained; used fallback.")
            return out
        features = self.featurize(condition, forecast, horizon_days)
        prob = float(self.estimator.predict_proba([features])[0][1])
        return ModelOutput(fair_yes=prob, confidence=0.6, model=self.name,
                           notes=["Trained sklearn predict_proba."])


def build_model(config: Config) -> ProbabilityModel:
    """Construct the model named by ``model.default`` in config."""
    name = str(config.get("model.default", "normal_forecast"))
    sigma_table = config.get("weather.temp_error_sigma_f", None)
    conf = config.get("model.confidence", {}) or {}
    ens = config.get("model.ensemble_sigma", {}) or {}
    baseline = NormalForecastModel(
        temp_sigma_by_horizon=sigma_table,
        long_horizon_days=int(conf.get("long_horizon_days", 7)),
        long_horizon_penalty=float(conf.get("long_horizon_penalty", 0.6)),
        ambiguous_penalty=float(conf.get("ambiguous_settlement_penalty", 0.4)),
        bias_correction_f=float(config.get("model.bias_correction_f", 0.0)),
        ensemble_sigma_a=(float(ens["a"]) if "a" in ens else None),
        ensemble_sigma_b=(float(ens["b"]) if "b" in ens else None),
    )
    if name == "normal_forecast":
        return baseline
    if name == "intraday_temp":
        from .intraday import IntradayTemperatureModel

        return IntradayTemperatureModel(baseline=baseline)
    if name == "ensemble":
        from .intraday import IntradayTemperatureModel

        return EnsembleModel([baseline, IntradayTemperatureModel(baseline=baseline)])
    if name == "sklearn":
        return SklearnProbabilityModel(fallback=baseline)
    # Unknown -> baseline.
    return baseline

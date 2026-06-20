"""Abstract probability-model interface and output container.

Every model takes a parsed :class:`EventCondition` plus a :class:`Forecast` and
returns a :class:`ModelOutput` with a fair YES probability, fair NO probability,
a 0..1 confidence score, and free-text notes explaining the estimate.

The extensible layer: add logistic regression, gradient boosting, random forest,
Bayesian, or ensemble models by subclassing :class:`ProbabilityModel`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional

from ..weather.base import Forecast
from .contract_parser import EventCondition


@dataclass
class ModelOutput:
    """Result of a fair-probability estimate for one contract."""

    fair_yes: float
    confidence: float
    model: str
    notes: list[str] = field(default_factory=list)
    point_forecast: Optional[float] = None
    sigma: Optional[float] = None

    @property
    def fair_no(self) -> float:
        return 1.0 - self.fair_yes

    def __post_init__(self) -> None:
        self.fair_yes = min(max(self.fair_yes, 0.0), 1.0)
        self.confidence = min(max(self.confidence, 0.0), 1.0)


class ProbabilityModel(abc.ABC):
    """Base class for fair-probability models."""

    name: str = "base"

    @abc.abstractmethod
    def estimate(
        self,
        condition: EventCondition,
        forecast: Optional[Forecast],
        horizon_days: int,
    ) -> ModelOutput:
        """Estimate the fair YES probability for ``condition``.

        Implementations must tolerate ``forecast is None`` (return confidence 0).
        """

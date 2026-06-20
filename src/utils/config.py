"""Configuration loading and access.

Loads ``config.yaml`` and overlays environment variables (from ``.env`` if
present). Exposes a small typed wrapper, :class:`Config`, with convenient
dotted access and a few derived helpers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

try:  # python-dotenv is optional at import time
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - trivial fallback
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def _deep_get(mapping: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = mapping
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


@dataclass
class Config:
    """Thin wrapper around the parsed YAML config plus environment overrides."""

    data: dict[str, Any] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    # -- access ---------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        """Return a config value by dotted path, e.g. ``risk.min_edge``."""
        return _deep_get(self.data, dotted, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    # -- derived helpers ------------------------------------------------------
    @property
    def mode(self) -> str:
        """Effective run mode.

        ``live`` does NOT require API credentials: Kalshi's market/orderbook data
        is public and read-only, and the weather APIs are key-less. Credentials
        are only relevant to order placement, which this project never does. If a
        live network fetch fails at runtime, the client falls back to mock with a
        warning (handled in :class:`~src.kalshi.client.KalshiClient`).
        """
        return str(self.get("mode", "mock")).lower()

    @property
    def requested_mode(self) -> str:
        return str(self.get("mode", "mock")).lower()

    @property
    def has_kalshi_credentials(self) -> bool:
        return bool(
            self.env.get("KALSHI_API_KEY_ID")
            and self.env.get("KALSHI_PRIVATE_KEY_PATH")
        )

    @property
    def paper_only(self) -> bool:
        return bool(self.get("trading.paper_only", True))

    @property
    def bankroll(self) -> float:
        return float(self.get("trading.bankroll_usd", 1000.0))

    def env_str(self, key: str, default: str = "") -> str:
        return str(self.env.get(key, default))


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load configuration from YAML and the process environment.

    Args:
        path: Optional explicit path to a YAML config file. Defaults to the
            repository ``config.yaml``.

    Returns:
        A populated :class:`Config`.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    # Selected environment overrides for operational fields.
    if "KALSHI_ENV" in os.environ:
        data.setdefault("kalshi", {})["env"] = os.environ["KALSHI_ENV"]
    if "LOG_LEVEL" in os.environ:
        data.setdefault("logging", {})["level"] = os.environ["LOG_LEVEL"]

    return Config(data=data, env=dict(os.environ))

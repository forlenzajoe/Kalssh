"""Centralized logging setup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: str = "INFO", logfile: str | None = None) -> logging.Logger:
    """Configure root logging once and return the package logger.

    Args:
        level: Logging level name (e.g. ``"INFO"``, ``"DEBUG"``).
        logfile: Optional path to also write logs to a file.

    Returns:
        The ``kalshi`` package logger.
    """
    global _CONFIGURED
    logger = logging.getLogger("kalshi")
    if _CONFIGURED:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if logfile:
        try:
            Path(logfile).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(logfile, encoding="utf-8")
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except OSError:  # pragma: no cover - filesystem dependent
            logger.warning("Could not open log file %s; logging to stderr only", logfile)

    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger under the ``kalshi`` namespace."""
    base = logging.getLogger("kalshi")
    return base.getChild(name) if name else base

"""Logging bootstrap."""

from __future__ import annotations

import sys

from loguru import logger


def configure_logging() -> None:
    """Configure a simple stderr logger for local development."""
    logger.remove()
    logger.add(sys.stderr, level="INFO")


"""Logging bootstrap."""

from __future__ import annotations

import os
import sys

from loguru import logger


def configure_logging() -> None:
    """Configure a simple stderr logger for local development."""
    logger.remove()
    logger.add(sys.stderr, level=os.getenv("BHIKSHA_LOG_LEVEL", "INFO").upper())

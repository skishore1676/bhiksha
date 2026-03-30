"""Timezone and session helpers."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import polars as pl


UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")


def ensure_utc(timestamp: datetime) -> datetime:
    """Treat naive timestamps as UTC and normalize aware timestamps to UTC."""
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def as_et_time(timestamp: datetime) -> time:
    """Return the ET time-of-day for a UTC or naive-UTC timestamp."""
    return ensure_utc(timestamp).astimezone(ET).timetz().replace(tzinfo=None)


def et_timestamp_expr(column: str = "timestamp") -> pl.Expr:
    """Return a Polars expression converted to America/New_York."""
    return (
        pl.col(column)
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone("America/New_York")
    )


def et_time_expr(column: str = "timestamp") -> pl.Expr:
    """Return ET time-of-day as a Polars expression."""
    return et_timestamp_expr(column).dt.time()


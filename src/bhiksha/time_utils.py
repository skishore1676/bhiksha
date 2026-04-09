"""Helpers for parsing and normalizing operator-facing time strings."""

from __future__ import annotations

import re
from datetime import time

_TIME_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?$")


def normalize_time_text(value: str | None) -> str | None:
    """Accept H:MM/HH:MM strings and normalize them to zero-padded ISO time."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    match = _TIME_RE.fullmatch(stripped)
    if match is None:
        return stripped
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    second = int(match.group("second") or 0)
    if hour > 23 or minute > 59 or second > 59:
        return stripped
    if match.group("second") is None:
        return f"{hour:02d}:{minute:02d}"
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def parse_time_text(value: time | str) -> time:
    """Accept time objects or loose H:MM strings and return a time object."""
    if isinstance(value, time):
        return value
    normalized = normalize_time_text(value)
    if normalized is None:
        raise ValueError("Time value is required")
    try:
        return time.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid time value: {value!r}") from exc

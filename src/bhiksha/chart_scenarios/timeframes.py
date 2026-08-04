"""Exact XNYS session aggregation for immutable chart evidence."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

import exchange_calendars
import pandas as pd

from bhiksha.domain.models import Bar

SUPPORTED_TIMEFRAMES = frozenset({"39m", "daily"})
CALENDAR_VERSION = "exchange_calendars-4.13.2-XNYS"


@lru_cache(maxsize=1)
def _xnys() -> object:
    if exchange_calendars.__version__ != "4.13.2":
        raise RuntimeError("chart evidence requires exchange_calendars 4.13.2")
    return exchange_calendars.get_calendar("XNYS")


def is_xnys_session_date(value: date) -> bool:
    return bool(_xnys().is_session(pd.Timestamp(value)))


def xnys_session_bounds(value: date) -> tuple[datetime, datetime]:
    label = pd.Timestamp(value)
    if not _xnys().is_session(label):
        raise ValueError(f"not an XNYS session: {value.isoformat()}")
    opened = _xnys().session_open(label).to_pydatetime().astimezone(UTC)
    closed = _xnys().session_close(label).to_pydatetime().astimezone(UTC)
    return opened, closed


def xnys_session_dates(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        return ()
    labels = _xnys().sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return tuple(label.date() for label in labels)


def aggregate_completed_bars(
    bars: list[Bar], *, timeframe: str, evaluated_at: datetime
) -> list[Bar]:
    return [
        bar
        for bar, _visible_at in aggregate_completed_bars_with_visibility(
            bars, timeframe=timeframe, evaluated_at=evaluated_at
        )
    ]


def aggregate_completed_bars_with_visibility(
    bars: list[Bar], *, timeframe: str, evaluated_at: datetime
) -> list[tuple[Bar, datetime]]:
    """Aggregate exact source minutes and return each bar's visibility time."""

    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"unsupported chart-scenario timeframe: {timeframe}")
    cutoff = evaluated_at.astimezone(UTC)
    ordered = sorted(bars, key=lambda item: item.timestamp)
    identities = [(bar.symbol, bar.timestamp.astimezone(UTC)) for bar in ordered]
    if len(identities) != len(set(identities)):
        raise ValueError(
            "source minute bars must have unique symbol/timestamp identity"
        )
    by_session: dict[date, list[Bar]] = defaultdict(list)
    for bar in ordered:
        timestamp = bar.timestamp.astimezone(UTC)
        # Calendar lookup uses the New York session label, not the host timezone.
        session_day = timestamp.astimezone(_new_york()).date()
        if not is_xnys_session_date(session_day):
            continue
        opened, closed = xnys_session_bounds(session_day)
        if opened <= timestamp < closed:
            by_session[session_day].append(bar)

    aggregated: list[tuple[Bar, datetime]] = []
    for session_day, session_bars in sorted(by_session.items()):
        opened, closed = xnys_session_bounds(session_day)
        indexed = {bar.timestamp.astimezone(UTC): bar for bar in session_bars}
        session_minutes = int((closed - opened).total_seconds() // 60)
        if timeframe == "daily":
            expected = [
                opened + timedelta(minutes=index) for index in range(session_minutes)
            ]
            if closed <= cutoff and sorted(indexed) == expected:
                values = [indexed[timestamp] for timestamp in expected]
                aggregated.append(
                    (_aggregate_bucket(values, timestamp=expected[-1]), closed)
                )
            continue
        minutes = 39
        for ordinal in range(session_minutes // minutes):
            bucket_start = opened + timedelta(minutes=ordinal * minutes)
            bucket_end = bucket_start + timedelta(minutes=minutes)
            expected = [
                bucket_start + timedelta(minutes=index) for index in range(minutes)
            ]
            if bucket_end > cutoff or any(
                timestamp not in indexed for timestamp in expected
            ):
                continue
            values = [indexed[timestamp] for timestamp in expected]
            aggregated.append(
                (_aggregate_bucket(values, timestamp=bucket_start), bucket_end)
            )
    return aggregated


@lru_cache(maxsize=1)
def _new_york() -> object:
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/New_York")


def _aggregate_bucket(values: list[Bar], *, timestamp: datetime) -> Bar:
    return Bar(
        symbol=values[0].symbol,
        timestamp=timestamp,
        open=values[0].open,
        high=max(item.high for item in values),
        low=min(item.low for item in values),
        close=values[-1].close,
        volume=sum(item.volume for item in values),
    )


__all__ = [
    "CALENDAR_VERSION",
    "SUPPORTED_TIMEFRAMES",
    "aggregate_completed_bars",
    "aggregate_completed_bars_with_visibility",
    "is_xnys_session_date",
    "xnys_session_bounds",
    "xnys_session_dates",
]

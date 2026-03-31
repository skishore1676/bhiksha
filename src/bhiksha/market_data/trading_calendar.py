"""NYSE trading-calendar helpers for session-aware lookbacks."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    first_day = date(year, month, 1)
    offset = (weekday - first_day.weekday()) % 7
    first_occurrence = first_day + timedelta(days=offset)
    return first_occurrence + timedelta(weeks=n - 1)


def _observe(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=16)
def nyse_holidays(year: int) -> frozenset[date]:
    holidays = {
        _observe(date(year, 1, 1)),
        _nth_weekday_of_month(year, 1, 0, 3),
        _nth_weekday_of_month(year, 2, 0, 3),
        _easter(year) - timedelta(days=2),
        _observe(date(year, 6, 19)),
        _observe(date(year, 7, 4)),
        _nth_weekday_of_month(year, 9, 0, 1),
        _nth_weekday_of_month(year, 11, 3, 4),
        _observe(date(year, 12, 25)),
    }

    first_monday = _nth_weekday_of_month(year, 5, 0, 1)
    last_monday = first_monday + timedelta(weeks=4)
    if last_monday.month != 5:
        last_monday = first_monday + timedelta(weeks=3)
    holidays.add(last_monday)

    return frozenset(holidays)


def is_trading_day(value: date) -> bool:
    return value.weekday() < 5 and value not in nyse_holidays(value.year)


def previous_trading_day(value: date) -> date:
    candidate = value - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def trading_days_ago(anchor: date, trading_days: int) -> date:
    candidate = anchor
    if not is_trading_day(candidate):
        candidate = previous_trading_day(candidate)
    for _ in range(max(trading_days - 1, 0)):
        candidate = previous_trading_day(candidate)
    return candidate


def trading_window_start(now: datetime, trading_days: int) -> datetime:
    anchor = now.astimezone(UTC).date()
    start_day = trading_days_ago(anchor, trading_days)
    return datetime.combine(start_day, datetime.min.time(), tzinfo=UTC)

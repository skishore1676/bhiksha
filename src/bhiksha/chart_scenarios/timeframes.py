"""Pure XNYS session-date rules used by immutable chart evidence."""

from __future__ import annotations

from datetime import date, timedelta


def is_xnys_session_date(value: date) -> bool:
    """Return whether a date is a regular XNYS trading-session date.

    This intentionally models full-day closures only. Early closes remain
    sessions; the exact available minute tape determines which anchored
    intraday buckets are complete.
    """

    if value.weekday() >= 5:
        return False
    year = value.year
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # MLK
        _nth_weekday(year, 2, 0, 3),  # Presidents
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),  # Memorial
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    # A Saturday New Year's Day is observed on the prior calendar year.
    holidays.add(_observed(date(year + 1, 1, 1)))
    return value not in holidays


def _observed(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (ordinal - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm.
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
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


__all__ = ["is_xnys_session_date"]

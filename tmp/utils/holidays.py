"""
holidays.py

NYSE market holiday calendar utility.
Holidays are computed algorithmically where possible (e.g., Thanksgiving = 4th Thursday of Nov).
Fixed-date holidays that fall on weekends are observed on the nearest weekday (Fri or Mon).
"""
from datetime import date, timedelta
from functools import lru_cache


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth occurrence of a weekday in a given month.
    weekday: 0=Monday, 6=Sunday.  n: 1-based (1st, 2nd, 3rd, 4th).
    """
    first_day = date(year, month, 1)
    # Days until first occurrence of target weekday
    offset = (weekday - first_day.weekday()) % 7
    first_occurrence = first_day + timedelta(days=offset)
    return first_occurrence + timedelta(weeks=n - 1)


def _observe(d: date) -> date:
    """If a holiday falls on Saturday, observe Friday. If Sunday, observe Monday."""
    if d.weekday() == 5:  # Saturday -> Friday
        return d - timedelta(days=1)
    elif d.weekday() == 6:  # Sunday -> Monday
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=8)
def nyse_holidays(year: int) -> frozenset:
    """
    Return the set of NYSE-observed market holidays for a given year.
    
    NYSE closes for 9 holidays per year:
      1. New Year's Day (Jan 1)
      2. Martin Luther King Jr. Day (3rd Monday of Jan)
      3. Presidents' Day (3rd Monday of Feb)
      4. Good Friday (Friday before Easter)
      5. Memorial Day (last Monday of May)
      6. Juneteenth (Jun 19) — observed since 2022
      7. Independence Day (Jul 4)
      8. Labor Day (1st Monday of Sep)
      9. Thanksgiving Day (4th Thursday of Nov)
     10. Christmas Day (Dec 25)
    """
    holidays = set()

    # 1. New Year's Day
    holidays.add(_observe(date(year, 1, 1)))

    # 2. MLK Day — 3rd Monday of January
    holidays.add(_nth_weekday_of_month(year, 1, 0, 3))  # Monday=0

    # 3. Presidents' Day — 3rd Monday of February
    holidays.add(_nth_weekday_of_month(year, 2, 0, 3))

    # 4. Good Friday — 2 days before Easter Sunday
    holidays.add(_easter(year) - timedelta(days=2))

    # 5. Memorial Day — last Monday of May
    #    Find 1st Monday, then check if 5th exists; otherwise use 4th
    first_mon = _nth_weekday_of_month(year, 5, 0, 1)
    last_mon = first_mon + timedelta(weeks=4)
    if last_mon.month != 5:
        last_mon = first_mon + timedelta(weeks=3)
    holidays.add(last_mon)

    # 6. Juneteenth — June 19
    holidays.add(_observe(date(year, 6, 19)))

    # 7. Independence Day — July 4
    holidays.add(_observe(date(year, 7, 4)))

    # 8. Labor Day — 1st Monday of September
    holidays.add(_nth_weekday_of_month(year, 9, 0, 1))

    # 9. Thanksgiving — 4th Thursday of November
    holidays.add(_nth_weekday_of_month(year, 11, 3, 4))  # Thursday=3

    # 10. Christmas Day — Dec 25
    holidays.add(_observe(date(year, 12, 25)))

    return frozenset(holidays)


def _easter(year: int) -> date:
    """Compute Easter Sunday using the Anonymous Gregorian algorithm."""
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


def is_market_holiday(d: date) -> bool:
    """Check if a given date is an NYSE market holiday."""
    return d in nyse_holidays(d.year)


def is_trading_day(d: date) -> bool:
    """Check if a given date is a valid NYSE trading day (not weekend, not holiday)."""
    return d.weekday() < 5 and not is_market_holiday(d)


def next_trading_day(d: date) -> date:
    """Return the next trading day after the given date (skips weekends and holidays)."""
    candidate = d + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate

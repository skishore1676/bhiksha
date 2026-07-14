from datetime import UTC, date, datetime

from bhiksha.market_data.trading_calendar import (
    is_trading_day,
    next_trading_day,
    next_trading_session_required_through,
    trading_days_ago,
    trading_window_start,
)


def test_is_trading_day_rejects_weekend_and_good_friday() -> None:
    assert is_trading_day(date(2026, 3, 28)) is False
    assert is_trading_day(date(2026, 4, 3)) is False
    assert is_trading_day(date(2026, 3, 30)) is True


def test_is_trading_day_handles_juneteenth_and_special_closure() -> None:
    assert is_trading_day(date(2021, 6, 18)) is True
    assert is_trading_day(date(2022, 6, 20)) is False
    assert is_trading_day(date(2025, 1, 9)) is False
    assert is_trading_day(date(2025, 1, 10)) is True


def test_trading_days_ago_skips_weekends() -> None:
    assert trading_days_ago(date(2026, 3, 31), 3) == date(2026, 3, 27)
    assert trading_days_ago(date(2025, 1, 13), 2) == date(2025, 1, 10)


def test_trading_window_start_uses_midnight_utc_for_anchor_session() -> None:
    start = trading_window_start(datetime(2026, 3, 31, 14, 0, tzinfo=UTC), 3)
    assert start == datetime(2026, 3, 27, 0, 0, tzinfo=UTC)


def test_next_trading_day_skips_weekend_and_holiday() -> None:
    assert next_trading_day(date(2026, 7, 17)) == date(2026, 7, 20)
    assert next_trading_day(date(2026, 9, 4)) == date(2026, 9, 8)


def test_after_close_required_through_uses_next_full_session() -> None:
    required = next_trading_session_required_through(datetime(2026, 7, 17, 20, 20, tzinfo=UTC))
    assert required.isoformat() == "2026-07-20T15:15:00-05:00"


def test_session_boundary_moves_to_next_day_immediately_after_required_through() -> None:
    required = next_trading_session_required_through(datetime(2026, 7, 16, 20, 16, tzinfo=UTC))

    assert required.isoformat() == "2026-07-17T15:15:00-05:00"

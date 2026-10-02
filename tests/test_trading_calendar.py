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


def test_regular_session_bounds_include_dst_holidays_and_early_close():
    from bhiksha.market_data.trading_calendar import regular_session_bounds
    assert regular_session_bounds(date(2026,11,26)) is None
    assert regular_session_bounds(date(2026,10,3)) is None
    assert regular_session_bounds(date(2025,1,9)) is None
    assert regular_session_bounds(date(2026,10,2)) == (
        datetime(2026,10,2,13,30,tzinfo=UTC),datetime(2026,10,2,20,tzinfo=UTC))
    assert regular_session_bounds(date(2026,11,27)) == (
        datetime(2026,11,27,14,30,tzinfo=UTC),datetime(2026,11,27,18,tzinfo=UTC))


def test_observation_continuity_excludes_early_close_weekend_and_holiday():
    from bhiksha.ops.exit_edge_lab import _observable_seconds
    assert _observable_seconds(datetime(2026,11,27,17,59,45,tzinfo=UTC),
        datetime(2026,11,30,14,30,15,tzinfo=UTC)) == 30
    assert _observable_seconds(datetime(2026,11,25,20,59,45,tzinfo=UTC),
        datetime(2026,11,27,14,30,15,tzinfo=UTC)) == 30

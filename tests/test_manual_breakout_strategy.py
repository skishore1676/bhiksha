from datetime import datetime, timedelta

import polars as pl

from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.manual_breakout import ManualBreakoutStrategy


def test_manual_breakout_emits_first_close_through_trigger_once() -> None:
    strategy = ManualBreakoutStrategy()
    frame = pl.DataFrame(
        {
            "symbol": ["AAPL"] * 4,
            "timestamp": [datetime(2026, 4, 1, 14, 29) + timedelta(minutes=i) for i in range(4)],
            "close": [259.2, 259.4, 259.6, 259.8],
            "vma_10_5m": [259.0, 259.1, 259.2, 259.3],
        }
    )

    decision = strategy.evaluate_entry(
        frame.head(3),
        "manual_breakout_aapl_long_v1",
        {
            "direction": "long",
            "trigger_price": 259.5,
            "trigger_direction": "ABOVE",
            "after_time_et": "09:30",
        },
    )

    assert decision.signal is True
    assert decision.direction is not None
    assert decision.direction.value == "long"
    assert "manual_breakout_triggered" in decision.reason

    later = strategy.evaluate_entry(
        frame,
        "manual_breakout_aapl_long_v1",
        {
            "direction": "long",
            "trigger_price": 259.5,
            "trigger_direction": "ABOVE",
            "after_time_et": "09:30",
        },
    )
    assert later.signal is False
    assert "manual_breakout_waiting" in later.reason


def test_manual_breakout_exit_fires_on_vma_loss_for_long() -> None:
    strategy = ManualBreakoutStrategy()
    frame = pl.DataFrame(
        {
            "symbol": ["AAPL"],
            "timestamp": [datetime(2026, 4, 1, 15, 0)],
            "close": [258.9],
            "vma_10_5m": [259.1],
        }
    )

    decision = strategy.evaluate_exit(
        frame,
        "manual_breakout_aapl_long_v1",
        {
            "direction": "long",
            "trigger_price": 259.5,
            "trigger_direction": "ABOVE",
            "vma_length": 10,
            "vma_timeframe": "5m",
        },
        TrackedPosition(
            symbol="AAPL",
            deployment_id="manual_breakout_aapl_long_v1",
            option_symbol="AAPL_OPTION",
            quantity=1,
        ),
    )

    assert decision.exit is True
    assert decision.action == "square_off"
    assert decision.reason == ["manual_breakout_vma_loss_exit"]


def test_manual_breakout_ignores_pre_activation_bars_for_first_signal() -> None:
    strategy = ManualBreakoutStrategy()
    frame = pl.DataFrame(
        {
            "symbol": ["AAPL"] * 4,
            "timestamp": [datetime(2026, 4, 1, 13, 29) + timedelta(minutes=i) for i in range(4)],
            "close": [258.9, 259.1, 259.2, 259.25],
            "vma_10_5m": [258.8, 258.9, 259.0, 259.1],
        }
    )

    decision = strategy.evaluate_entry(
        frame.head(3),
        "manual_breakout_aapl_long_v1",
        {
            "direction": "long",
            "trigger_price": 258.5,
            "trigger_direction": "ABOVE",
            "after_time_et": "09:31",
        },
    )

    assert decision.signal is True
    assert "manual_breakout_triggered" in decision.reason

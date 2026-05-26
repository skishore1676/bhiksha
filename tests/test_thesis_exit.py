from datetime import UTC, datetime

import polars as pl

from bhiksha.execution.thesis_exit import evaluate_underlying_thesis_exit
from bhiksha.state.position_tracker import TrackedPosition
from historical_config import historical_deployment


def _deployment_with_exit(policy: str, params: dict | None = None):
    base = historical_deployment("market_impulse_qqq_short_v1")
    return base.model_copy(
        update={
            "exit": base.exit.model_copy(
                update={
                    "use_algorithmic_exit": False,
                    "thesis_exit_anchor": "underlying",
                    "thesis_exit_policy": policy,
                    "thesis_exit_params": params or {},
                    "hard_flat_time_et": "15:55",
                }
            )
        }
    )


def _frame_at(timestamp: datetime) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["QQQ"],
            "timestamp": [timestamp],
            "open": [100.0],
            "high": [100.1],
            "low": [99.9],
            "close": [100.0],
            "volume": [1000.0],
        }
    )


def _position() -> TrackedPosition:
    return TrackedPosition(
        symbol="QQQ",
        deployment_id="market_impulse_qqq_short_v1",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        underlying_entry_price=100.0,
    )


def test_hold_to_eod_underlying_treats_utc_bar_as_et_wall_clock() -> None:
    deployment = _deployment_with_exit("hold_to_eod_underlying")

    before_eod = evaluate_underlying_thesis_exit(
        deployment,
        _frame_at(datetime(2026, 5, 26, 15, 56, tzinfo=UTC)),
        _position(),
    )
    at_eod = evaluate_underlying_thesis_exit(
        deployment,
        _frame_at(datetime(2026, 5, 26, 19, 56, tzinfo=UTC)),
        _position(),
    )

    assert before_eod is not None
    assert before_eod.exit is False
    assert at_eod is not None
    assert at_eod.exit is True
    assert at_eod.reason == ["hold_to_eod_underlying"]


def test_time_stop_underlying_treats_utc_bar_as_et_wall_clock() -> None:
    deployment = _deployment_with_exit("time_stop_underlying", {"exit_time_et": "11:00"})

    before_stop = evaluate_underlying_thesis_exit(
        deployment,
        _frame_at(datetime(2026, 5, 26, 14, 59, tzinfo=UTC)),
        _position(),
    )
    at_stop = evaluate_underlying_thesis_exit(
        deployment,
        _frame_at(datetime(2026, 5, 26, 15, 0, tzinfo=UTC)),
        _position(),
    )

    assert before_stop is not None
    assert before_stop.exit is False
    assert at_stop is not None
    assert at_stop.exit is True
    assert at_stop.reason == ["time_stop_underlying:1100"]

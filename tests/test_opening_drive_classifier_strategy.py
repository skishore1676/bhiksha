from datetime import datetime, timedelta

import polars as pl

from bhiksha.config.models import DeploymentManifest
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.opening_drive_classifier import OpeningDriveClassifierStrategy


def test_opening_drive_classifier_emits_continue_long_signal() -> None:
    strategy = OpeningDriveClassifierStrategy()
    deployment = DeploymentManifest.model_validate(
        {
            "deployment_id": "opening_drive_spy_long_v1",
            "symbol": "SPY",
            "strategy": {
                "key": "opening_drive_classifier",
                "version": 1,
                "params": {
                    "direction": "long",
                    "opening_window_minutes": 25,
                    "entry_start_offset_minutes": 30,
                    "entry_end_offset_minutes": 120,
                    "min_drive_return_pct": 0.0015,
                    "breakout_buffer_pct": 0.0,
                    "kinematic_periods_back": 1,
                    "use_volume_filter": True,
                    "volume_multiplier": 1.2,
                    "use_directional_mass": True,
                    "use_jerk_confirmation": True,
                    "enable_continue": True,
                    "enable_fail": False,
                },
            },
            "execution": {"profile": "single_leg_long_premium_v1"},
            "risk": {"profile": "conservative_day1"},
            "exit": {"profile": "opening_drive_exit_v1", "use_algorithmic_exit": False},
        }
    )
    frame = _opening_drive_frame(
        closes=[100.0, 100.15, 100.2, 100.35, 100.4, 100.45, 100.5, 100.55, 100.6, 100.7],
        directional_mass=[0.2] * 10,
        volumes=[100.0] * 5 + [220.0, 230.0, 240.0, 260.0, 300.0],
    )

    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)

    assert decision.signal is True
    assert decision.direction is not None
    assert decision.direction.value == "long"
    assert "opening_drive_continue_long" in decision.reason
    assert decision.features["opening_drive_mode"] == "continue"


def test_opening_drive_classifier_emits_fail_short_signal() -> None:
    strategy = OpeningDriveClassifierStrategy()
    deployment = DeploymentManifest.model_validate(
        {
            "deployment_id": "opening_drive_spy_short_v1",
            "symbol": "SPY",
            "strategy": {
                "key": "opening_drive_classifier",
                "version": 1,
                "params": {
                    "direction": "short",
                    "opening_window_minutes": 25,
                    "entry_start_offset_minutes": 30,
                    "entry_end_offset_minutes": 120,
                    "min_drive_return_pct": 0.0015,
                    "breakout_buffer_pct": 0.0,
                    "kinematic_periods_back": 1,
                    "use_volume_filter": False,
                    "use_directional_mass": False,
                    "use_jerk_confirmation": False,
                    "enable_continue": False,
                    "enable_fail": True,
                },
            },
            "execution": {"profile": "single_leg_long_premium_v1"},
            "risk": {"profile": "conservative_day1"},
            "exit": {"profile": "opening_drive_exit_v1", "use_algorithmic_exit": False},
        }
    )
    frame = _opening_drive_frame(
        closes=[100.0, 100.15, 100.2, 100.35, 100.4, 100.34, 100.31, 100.29, 100.27, 100.18],
        directional_mass=[-0.2] * 10,
        volumes=[100.0] * 10,
    )

    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)

    assert decision.signal is True
    assert decision.direction is not None
    assert decision.direction.value == "short"
    assert "opening_drive_fail_short" in decision.reason
    assert decision.features["opening_drive_mode"] == "fail"


def test_opening_drive_classifier_exit_is_runtime_managed() -> None:
    strategy = OpeningDriveClassifierStrategy()
    frame = pl.DataFrame(
        {
            "symbol": ["SPY"],
            "timestamp": [datetime(2026, 4, 1, 15, 0, 0)],
            "open": [100.0],
            "high": [100.1],
            "low": [99.9],
            "close": [100.0],
        }
    )

    decision = strategy.evaluate_exit(
        frame,
        "opening_drive_spy_long_v1",
        {},
        TrackedPosition(symbol="SPY", deployment_id="opening_drive_spy_long_v1", option_symbol="SPY_OPTION", quantity=1),
    )

    assert decision.exit is False
    assert decision.reason == ["strategy_exit_not_configured"]


def _opening_drive_frame(
    *,
    closes: list[float],
    directional_mass: list[float],
    volumes: list[float],
) -> pl.DataFrame:
    start = datetime(2026, 4, 1, 13, 30, 0)
    timestamps = [start + timedelta(minutes=5 * idx) for idx in range(len(closes))]
    opens = [closes[0], *closes[:-1]]
    highs = [max(open_price, close) + 0.05 for open_price, close in zip(opens, closes, strict=False)]
    lows = [min(open_price, close) - 0.05 for open_price, close in zip(opens, closes, strict=False)]
    return pl.DataFrame(
        {
            "symbol": ["SPY"] * len(closes),
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "directional_mass": directional_mass,
        }
    )

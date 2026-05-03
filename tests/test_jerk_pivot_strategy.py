from datetime import datetime, timedelta

import polars as pl

from bhiksha.config.loader import load_deployments
from bhiksha.strategy.jerk_pivot_momentum import JerkPivotMomentumStrategy


def _tsla_deployment():
    return next(
        deployment
        for deployment in load_deployments("config/deployments")
        if deployment.deployment_id == "jerk_pivot_momentum_tsla_short_v1"
    )


def _base_frame(*, latest_timestamp: datetime, latest_volume: float = 1500.0) -> pl.DataFrame:
    timestamps = [latest_timestamp - timedelta(minutes=10 - index) for index in range(11)]
    return pl.DataFrame(
        {
            "symbol": ["TSLA"] * 11,
            "timestamp": timestamps,
            "close": [100.05] * 10 + [99.90],
            "velocity_1m": [0.05] * 10 + [-0.20],
            "accel_1m": [0.02] * 10 + [-0.10],
            "jerk_1m": [1.0] * 10 + [-20.0],
            "vpoc_4h": [100.0] * 11,
            "volume": [1400.0] * 10 + [latest_volume],
            "volume_ma_20": [1000.0] * 11,
        }
    )


def _parametric_frame(*, latest_timestamp: datetime) -> pl.DataFrame:
    timestamps = [latest_timestamp - timedelta(minutes=10 - index) for index in range(11)]
    return pl.DataFrame(
        {
            "symbol": ["AMD"] * 11,
            "timestamp": timestamps,
            "close": [100.05] * 10 + [99.90],
            "velocity_3": [0.05] * 10 + [-0.20],
            "accel_3": [0.02] * 10 + [-0.10],
            "jerk_3": [1.0] * 10 + [-20.0],
            "vpoc_4h": [100.0] * 11,
            "volume": [1400.0] * 10 + [1500.0],
            "volume_ma_20": [1000.0] * 11,
        }
    )


def test_jerk_pivot_short_signal_on_latest_bar() -> None:
    deployment = _tsla_deployment()
    strategy = JerkPivotMomentumStrategy()
    frame = _base_frame(latest_timestamp=datetime(2026, 3, 30, 15, 0, 0))

    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)

    assert decision.signal is True
    assert decision.direction.value == "short"
    assert "jerk_pivot_short" in decision.reason
    assert decision.features["jerk_smooth_1m"] < 0
    assert decision.features["prev_jerk_smooth_1m"] > 0


def test_jerk_pivot_honors_kinematic_periods_back() -> None:
    strategy = JerkPivotMomentumStrategy()
    params = {
        "direction": "short",
        "jerk_lookback": 10,
        "kinematic_periods_back": 3,
        "use_volume_filter": True,
        "volume_multiplier": 1.0,
        "volume_ma_period": 20,
        "vpoc_proximity_pct": 0.002,
    }
    frame = _parametric_frame(latest_timestamp=datetime(2026, 3, 30, 15, 0, 0))

    assert {"velocity_3", "accel_3", "jerk_3"} <= strategy.required_features(params)
    assert "velocity_1m" not in strategy.required_features(params)

    decision = strategy.evaluate_entry(frame, "jerk-parametric", params)

    assert decision.signal is True
    assert decision.direction.value == "short"
    assert decision.features["velocity_3"] < 0
    assert decision.features["accel_3"] < 0
    assert decision.features["jerk_smooth_3"] < 0


def test_jerk_pivot_volume_filter_blocks_signal() -> None:
    deployment = _tsla_deployment()
    strategy = JerkPivotMomentumStrategy()
    frame = _base_frame(
        latest_timestamp=datetime(2026, 3, 30, 15, 0, 0),
        latest_volume=1200.0,
    )

    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)

    assert decision.signal is False
    assert decision.direction is None
    assert "volume_gate_blocked" in decision.reason


def test_jerk_pivot_time_filter_blocks_signal() -> None:
    deployment = _tsla_deployment()
    strategy = JerkPivotMomentumStrategy()
    frame = _base_frame(latest_timestamp=datetime(2026, 3, 30, 20, 31, 0))

    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)

    assert decision.signal is False
    assert decision.direction is None
    assert "time_window_blocked" in decision.reason

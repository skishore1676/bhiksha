from datetime import datetime, timedelta

import polars as pl

from bhiksha.config.models import DeploymentManifest
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.manual_trigger import ManualTriggerStrategy


def test_manual_trigger_emits_first_above_signal_once() -> None:
    strategy = ManualTriggerStrategy()
    deployment = DeploymentManifest.model_validate(
        {
            "deployment_id": "manual_trigger_spy_long_v1",
            "symbol": "SPY",
            "strategy": {
                "key": "manual_trigger",
                "version": 1,
                "params": {
                    "direction": "long",
                    "trigger_price": 601.0,
                    "trigger_direction": "ABOVE",
                    "after_time_et": "09:35",
                },
            },
            "execution": {"profile": "single_leg_long_premium_v1"},
            "risk": {"profile": "manual_trigger_v1"},
            "exit": {"profile": "manual_trigger_exit_v1", "use_algorithmic_exit": False},
        }
    )
    frame = pl.DataFrame(
        {
            "symbol": ["SPY"] * 4,
            "timestamp": [datetime(2026, 4, 1, 14, 30) + timedelta(minutes=i) for i in range(4)],
            "close": [600.5, 600.9, 601.02, 601.10],
        }
    )

    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)

    assert decision.signal is False
    frame = frame.head(3)
    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)
    assert decision.signal is True
    assert decision.direction is not None
    assert decision.direction.value == "long"
    assert "manual_trigger_met" in decision.reason


def test_manual_trigger_close_by_supports_short() -> None:
    strategy = ManualTriggerStrategy()
    deployment = DeploymentManifest.model_validate(
        {
            "deployment_id": "manual_trigger_tsla_short_v1",
            "symbol": "TSLA",
            "strategy": {
                "key": "manual_trigger",
                "version": 1,
                "params": {
                    "direction": "short",
                    "trigger_price": 250.0,
                    "trigger_direction": "CLOSE_BY",
                    "close_by_factor": 0.002,
                },
            },
            "execution": {"profile": "single_leg_long_premium_v1"},
            "risk": {"profile": "manual_trigger_v1"},
            "exit": {"profile": "manual_trigger_exit_v1", "use_algorithmic_exit": False},
        }
    )
    frame = pl.DataFrame(
        {
            "symbol": ["TSLA"] * 3,
            "timestamp": [datetime(2026, 4, 1, 14, 30) + timedelta(minutes=i) for i in range(3)],
            "close": [251.2, 250.6, 250.1],
        }
    )

    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)

    assert decision.signal is True
    assert decision.direction is not None
    assert decision.direction.value == "short"


def test_manual_trigger_exit_is_runtime_managed() -> None:
    strategy = ManualTriggerStrategy()
    frame = pl.DataFrame(
        {
            "symbol": ["SPY"],
            "timestamp": [datetime(2026, 4, 1, 15, 0, 0)],
            "close": [601.0],
        }
    )

    decision = strategy.evaluate_exit(
        frame,
        "manual_trigger_spy_long_v1",
        {},
        TrackedPosition(symbol="SPY", deployment_id="manual_trigger_spy_long_v1", option_symbol="SPY_OPTION", quantity=1),
    )

    assert decision.exit is False
    assert decision.reason == ["strategy_exit_not_configured"]


def test_manual_trigger_accepts_loose_after_time_strings() -> None:
    strategy = ManualTriggerStrategy()
    frame = pl.DataFrame(
        {
            "symbol": ["AAPL", "AAPL"],
            "timestamp": [datetime(2026, 4, 1, 14, 29), datetime(2026, 4, 1, 14, 30)],
            "close": [199.8, 200.1],
        }
    )

    decision = strategy.evaluate_entry(
        frame,
        "manual_trigger_aapl_long_v1",
        {
            "direction": "long",
            "trigger_price": 200.0,
            "trigger_direction": "ABOVE",
            "after_time_et": "9:30",
        },
    )

    assert decision.signal is True
    assert "after_time_et=9:30" in decision.reason

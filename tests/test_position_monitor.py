from datetime import datetime

import polars as pl

from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.execution.position_monitor import PositionMonitor
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.state.position_tracker import PositionTracker, TrackedPosition
from bhiksha.strategy.registry import default_strategy_registry
from historical_config import historical_deployment


def test_position_monitor_emits_exit_for_vma_reclaim() -> None:
    deployment = historical_deployment("market_impulse_qqq_short_v1")
    tracker = PositionTracker()
    tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        option_symbol="QQQ260401P00556000",
        quantity=1,
        stop_order_id="STOP123",
    )
    monitor = PositionMonitor(
        ReplaySignalEvaluator(FeatureService(), default_strategy_registry()),
        tracker,
    )
    frame = pl.DataFrame(
        {
            "symbol": ["QQQ", "QQQ", "QQQ"],
            "timestamp": [
                datetime(2026, 3, 30, 14, 29, 0),
                datetime(2026, 3, 30, 14, 30, 0),
                datetime(2026, 3, 30, 14, 31, 0),
            ],
            "open": [99.8, 99.9, 100.1],
            "high": [100.0, 100.1, 100.8],
            "low": [99.6, 99.8, 100.0],
            "close": [99.9, 100.0, 100.6],
            "volume": [1000.0, 1100.0, 1200.0],
        }
    )

    evaluations = monitor.evaluate_symbol("QQQ", frame, {deployment.deployment_id: deployment})

    assert len(evaluations) == 1
    assert evaluations[0].decision.exit is True
    assert evaluations[0].decision.action == "square_off"


def test_position_monitor_can_use_supplied_position_snapshot() -> None:
    deployment = historical_deployment("market_impulse_qqq_short_v1")
    tracker = PositionTracker()
    monitor = PositionMonitor(
        ReplaySignalEvaluator(FeatureService(), default_strategy_registry()),
        tracker,
    )
    frame = pl.DataFrame(
        {
            "symbol": ["QQQ", "QQQ", "QQQ"],
            "timestamp": [
                datetime(2026, 3, 30, 14, 29, 0),
                datetime(2026, 3, 30, 14, 30, 0),
                datetime(2026, 3, 30, 14, 31, 0),
            ],
            "open": [99.8, 99.9, 100.1],
            "high": [100.0, 100.1, 100.8],
            "low": [99.6, 99.8, 100.0],
            "close": [99.9, 100.0, 100.6],
            "volume": [1000.0, 1100.0, 1200.0],
        }
    )

    evaluations = monitor.evaluate_symbol(
        "QQQ",
        frame,
        {deployment.deployment_id: deployment},
        positions=[
            TrackedPosition(
                symbol="QQQ",
                deployment_id=deployment.deployment_id,
                option_symbol="QQQ260401P00556000",
                quantity=1,
                stop_order_id="STOP123",
            )
        ],
    )

    assert len(evaluations) == 1
    assert evaluations[0].decision.exit is True


def test_position_monitor_emits_fixed_rr_underlying_exit() -> None:
    base = historical_deployment("market_impulse_qqq_short_v1")
    deployment = base.model_copy(
        update={
            "exit": base.exit.model_copy(
                update={
                    "use_algorithmic_exit": False,
                    "thesis_exit_anchor": "underlying",
                    "thesis_exit_policy": "fixed_rr_underlying",
                    "thesis_exit_params": {
                        "stop_loss_underlying_pct": 0.005,
                        "take_profit_underlying_r_multiple": 1.5,
                    },
                }
            )
        }
    )
    tracker = PositionTracker()
    tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        option_symbol="QQQ260401P00556000",
        quantity=1,
        stop_order_id="STOP123",
        underlying_entry_price=100.0,
    )
    monitor = PositionMonitor(
        ReplaySignalEvaluator(FeatureService(), default_strategy_registry()),
        tracker,
    )
    frame = pl.DataFrame(
        {
            "symbol": ["QQQ"],
            "timestamp": [datetime(2026, 3, 30, 14, 31, 0)],
            "open": [99.8],
            "high": [100.1],
            "low": [99.1],
            "close": [99.2],
            "volume": [1200.0],
        }
    )

    evaluations = monitor.evaluate_symbol("QQQ", frame, {deployment.deployment_id: deployment})

    assert len(evaluations) == 1
    assert evaluations[0].decision.exit is True
    assert evaluations[0].decision.reason == ["thesis_take_profit_underlying"]


def test_position_monitor_emits_trailing_vma_underlying_exit() -> None:
    base = historical_deployment("market_impulse_qqq_short_v1")
    deployment = base.model_copy(
        update={
            "exit": base.exit.model_copy(
                update={
                    "use_algorithmic_exit": False,
                    "thesis_exit_anchor": "underlying",
                    "thesis_exit_policy": "trailing_vma_underlying",
                    "thesis_exit_params": {"vma_col": "vma_10"},
                }
            )
        }
    )
    tracker = PositionTracker()
    tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        option_symbol="QQQ260401P00556000",
        quantity=1,
        stop_order_id="STOP123",
    )
    monitor = PositionMonitor(
        ReplaySignalEvaluator(FeatureService(), default_strategy_registry()),
        tracker,
    )
    frame = pl.DataFrame(
        {
            "symbol": ["QQQ"],
            "timestamp": [datetime(2026, 3, 30, 14, 31, 0)],
            "open": [99.8],
            "high": [100.5],
            "low": [99.7],
            "close": [100.6],
            "volume": [1200.0],
            "vma_10": [100.2],
        }
    )

    evaluations = monitor.evaluate_symbol("QQQ", frame, {deployment.deployment_id: deployment})

    assert len(evaluations) == 1
    assert evaluations[0].decision.exit is True
    assert evaluations[0].decision.reason == ["thesis_vma_reclaim_underlying"]

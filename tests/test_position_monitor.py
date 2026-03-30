from datetime import datetime

import polars as pl

from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.config.loader import load_deployments
from bhiksha.execution.position_monitor import PositionMonitor
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.state.position_tracker import PositionTracker
from bhiksha.strategy.registry import default_strategy_registry


def test_position_monitor_emits_exit_for_vma_reclaim() -> None:
    deployment = next(
        deployment
        for deployment in load_deployments("config/deployments")
        if deployment.deployment_id == "market_impulse_qqq_short_v1"
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

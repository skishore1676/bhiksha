from datetime import datetime

import polars as pl

from bhiksha.config.loader import load_deployments
from bhiksha.market_data.newton.engine import PhysicsEngine
from bhiksha.strategy.market_impulse import MarketImpulseStrategy
from bhiksha.state.position_tracker import TrackedPosition


def test_market_impulse_short_signal_on_latest_bar() -> None:
    deployment = next(
        deployment
        for deployment in load_deployments("config/deployments")
        if deployment.deployment_id == "market_impulse_qqq_short_v1"
    )
    strategy = MarketImpulseStrategy()
    frame = pl.DataFrame(
        {
            "symbol": ["QQQ"],
            "timestamp": [datetime(2026, 3, 30, 14, 30, 0)],
            "high": [100.5],
            "low": [99.9],
            "close": [99.8],
            "vma_10": [100.0],
            "impulse_regime_1h": ["bearish"],
        }
    )

    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)

    assert decision.signal is True
    assert decision.direction.value == "short"


def test_market_impulse_required_features_include_custom_vwma_periods() -> None:
    strategy = MarketImpulseStrategy()

    features = strategy.required_features(
        {
            "regime_timeframe": "1h",
            "vma_length": 10,
            "vwma_periods": [5, 13, 21],
        }
    )

    assert "market_impulse:1h:vma_10:vwma_5_13_21" in features


def test_newton_engine_uses_custom_market_impulse_vwma_periods() -> None:
    engine = PhysicsEngine()

    transforms = engine.transforms_for_features({"market_impulse:1h:vma_10:vwma_5_13_21"})

    assert len(transforms) == 1
    assert transforms[0].vwma_periods == (5, 13, 21)
    assert transforms[0].timeframe == "1h"


def test_newton_engine_builds_parametric_kinematic_features() -> None:
    engine = PhysicsEngine()
    frame = pl.DataFrame(
        {
            "symbol": ["AMD"] * 12,
            "timestamp": [datetime(2026, 3, 30, 14, minute, 0) for minute in range(12)],
            "open": [float(value) for value in range(12)],
            "high": [float(value) + 0.5 for value in range(12)],
            "low": [float(value) - 0.5 for value in range(12)],
            "close": [float(value) for value in range(12)],
            "volume": [1000.0] * 12,
        }
    )

    enriched = engine.enrich_for_features(frame, {"jerk_3"})

    assert {"velocity_3", "accel_3", "jerk_3"} <= set(enriched.columns)
    assert enriched["velocity_3"].to_list()[-1] == 3.0
    assert enriched["accel_3"].to_list()[-1] == 0.0
    assert enriched["jerk_3"].to_list()[-1] == 0.0


def test_market_impulse_short_exit_on_vma_reclaim() -> None:
    deployment = next(
        deployment
        for deployment in load_deployments("config/deployments")
        if deployment.deployment_id == "market_impulse_qqq_short_v1"
    )
    strategy = MarketImpulseStrategy()
    frame = pl.DataFrame(
        {
            "symbol": ["QQQ"],
            "timestamp": [datetime(2026, 3, 30, 14, 31, 0)],
            "high": [100.6],
            "low": [99.9],
            "close": [100.2],
            "vma_10": [100.0],
            "impulse_regime_1h": ["bearish"],
        }
    )

    decision = strategy.evaluate_exit(
        frame,
        deployment.deployment_id,
        deployment.strategy.params,
        TrackedPosition(
            symbol="QQQ",
            deployment_id=deployment.deployment_id,
            option_symbol="QQQ260401P00556000",
            quantity=1,
        ),
    )

    assert decision.exit is True
    assert decision.action == "square_off"
    assert "vma_reclaim_exit" in decision.reason

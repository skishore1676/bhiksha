from datetime import datetime

import polars as pl

from bhiksha.config.models import DeploymentManifest
from bhiksha.strategy.elastic_band_reversion import ElasticBandReversionStrategy
from bhiksha.state.position_tracker import TrackedPosition


def test_elastic_band_reversion_emits_long_signal() -> None:
    strategy = ElasticBandReversionStrategy()
    deployment = DeploymentManifest.model_validate(
        {
            "deployment_id": "elastic_band_nvda_long_v1",
            "symbol": "NVDA",
            "strategy": {
                "key": "elastic_band_reversion",
                "version": 1,
                    "params": {
                        "direction": "long",
                        "z_score_threshold": 0.7,
                    "z_score_window": 3,
                    "kinematic_periods_back": 1,
                    "use_directional_mass": True,
                    "use_jerk_confirmation": True,
                },
            },
            "execution": {"profile": "single_leg_long_premium_v1"},
            "risk": {"profile": "conservative_day1"},
            "exit": {"profile": "elastic_band_exit_v1", "use_algorithmic_exit": False},
        }
    )
    frame = pl.DataFrame(
        {
            "symbol": ["NVDA"] * 6,
            "timestamp": [datetime(2026, 4, 1, 14, 30 + i, 0) for i in range(6)],
            "close": [100.0, 99.8, 99.7, 99.4, 99.0, 98.9],
            "vpoc_4h": [100.0] * 6,
            "directional_mass": [0.2, 0.3, 0.25, 0.22, 0.18, 0.15],
        }
    )

    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)

    assert decision.signal is True
    assert decision.direction is not None
    assert decision.direction.value == "long"
    assert "elastic_band_long" in decision.reason


def test_elastic_band_reversion_emits_short_signal_without_directional_mass() -> None:
    strategy = ElasticBandReversionStrategy()
    deployment = DeploymentManifest.model_validate(
        {
            "deployment_id": "elastic_band_nvda_short_v1",
            "symbol": "NVDA",
            "strategy": {
                "key": "elastic_band_reversion",
                "version": 1,
                    "params": {
                        "direction": "short",
                        "z_score_threshold": 0.9,
                    "z_score_window": 3,
                    "kinematic_periods_back": 1,
                    "use_directional_mass": False,
                    "use_jerk_confirmation": False,
                },
            },
            "execution": {"profile": "single_leg_long_premium_v1"},
            "risk": {"profile": "conservative_day1"},
            "exit": {"profile": "elastic_band_exit_v1", "use_algorithmic_exit": False},
        }
    )
    frame = pl.DataFrame(
        {
            "symbol": ["NVDA"] * 6,
            "timestamp": [datetime(2026, 4, 1, 14, 30 + i, 0) for i in range(6)],
            "close": [100.0, 100.2, 100.3, 100.7, 101.1, 101.4],
            "vpoc_4h": [100.0] * 6,
        }
    )

    decision = strategy.evaluate_entry(frame, deployment.deployment_id, deployment.strategy.params)

    assert decision.signal is True
    assert decision.direction is not None
    assert decision.direction.value == "short"
    assert "elastic_band_short" in decision.reason


def test_elastic_band_reversion_exit_is_runtime_managed() -> None:
    strategy = ElasticBandReversionStrategy()
    frame = pl.DataFrame(
        {
            "symbol": ["NVDA"],
            "timestamp": [datetime(2026, 4, 1, 14, 35, 0)],
            "close": [99.5],
            "vpoc_4h": [100.0],
        }
    )

    decision = strategy.evaluate_exit(
        frame,
        "elastic_band_nvda_long_v1",
        {},
        TrackedPosition(symbol="NVDA", deployment_id="elastic_band_nvda_long_v1", option_symbol="NVDA_OPTION", quantity=1),
    )

    assert decision.exit is False
    assert decision.reason == ["strategy_exit_not_configured"]

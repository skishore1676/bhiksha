from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from bhiksha.market_data.newton.engine import PhysicsEngine
from bhiksha.strategy.registry import default_strategy_registry


@pytest.mark.parametrize(
    ("strategy_key", "params"),
    [
        (
            "market_impulse",
            {
                "direction": "short",
                "entry_buffer_minutes": 5,
                "entry_window_minutes": 90,
                "regime_timeframe": "1h",
                "vwma_periods": [5, 13, 21],
            },
        ),
        (
            "jerk_pivot_momentum",
            {
                "direction": "short",
                "jerk_lookback": 12,
                "kinematic_periods_back": 3,
                "use_volume_filter": True,
                "volume_multiplier": 1.0,
                "vpoc_proximity_pct": 0.0015,
            },
        ),
        (
            "opening_drive_classifier",
            {
                "direction": "short",
                "opening_window_minutes": 15,
                "entry_start_offset_minutes": 25,
                "entry_end_offset_minutes": 120,
                "kinematic_periods_back": 3,
                "use_directional_mass": True,
                "use_jerk_confirmation": False,
                "use_regime_filter": True,
                "regime_timeframe": "5m",
                "use_volume_filter": False,
            },
        ),
        (
            "elastic_band_reversion",
            {
                "direction": "long",
                "kinematic_periods_back": 5,
                "use_directional_mass": True,
                "use_jerk_confirmation": True,
                "z_score_window": 360,
            },
        ),
        (
            "manual_breakout",
            {
                "direction": "short",
                "trigger_direction": "BELOW",
                "trigger_price": 333.0,
                "vma_length": 10,
                "vma_timeframe": "5m",
            },
        ),
    ],
)
def test_supported_strategy_required_features_are_runtime_resolvable(strategy_key: str, params: dict) -> None:
    strategy = default_strategy_registry().get(strategy_key)
    required = strategy.required_features(params)

    enriched = PhysicsEngine().enrich_for_features(_sample_ohlcv(), required)

    concrete_required = {feature for feature in required if ":" not in feature}
    assert concrete_required <= set(enriched.columns)
    strategy.evaluate_entry(enriched, f"{strategy_key}_contract_test", params)


def _sample_ohlcv() -> pl.DataFrame:
    rows = 500
    start = datetime(2026, 4, 1, 13, 30, tzinfo=UTC)
    closes = [100.0 + (idx * 0.02) for idx in range(rows)]
    opens = [closes[0], *closes[:-1]]
    return pl.DataFrame(
        {
            "symbol": ["SPY"] * rows,
            "timestamp": [start + timedelta(minutes=idx) for idx in range(rows)],
            "open": opens,
            "high": [max(open_price, close) + 0.05 for open_price, close in zip(opens, closes, strict=True)],
            "low": [min(open_price, close) - 0.05 for open_price, close in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1000.0 + (idx % 20) * 50.0 for idx in range(rows)],
        }
    )

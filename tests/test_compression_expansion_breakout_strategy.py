"""Unit tests for the Compression Expansion Breakout strategy plugin.

Semantics mirror mala_v2 ``src/strategy/compression_breakout.py``; parameter
values come from the blocked Mala_Evidence_v1 rows (compression-breakout and
vpoc-migration discovery families share the ``compression_expansion_breakout``
strategy key).
"""

from datetime import datetime, timedelta

import polars as pl
import pytest

from bhiksha.market_data.newton.engine import PhysicsEngine
from bhiksha.strategy.compression_expansion_breakout import CompressionExpansionBreakoutStrategy
from bhiksha.state.position_tracker import TrackedPosition


TSLA_SHORT_PARAMS = {
    # compression-breakout-current-basket-discovery__tsla_short (also
    # vpoc-migration-discovery-01__tsla_short)
    "direction": "short",
    "breakout_lookback": 20,
    "compression_factor": 0.7,
    "compression_window": 15,
    "use_volume_filter": True,
    "velocity_periods_back": 3,
}

AMD_SHORT_PARAMS = {
    # vpoc-migration-discovery-01__amd_short
    "direction": "short",
    "breakout_lookback": 15,
    "compression_factor": 0.7,
    "compression_window": 30,
    "use_volume_filter": False,
    "velocity_periods_back": 5,
}

# 2026-07-06 is EDT (UTC-4): 14:30 UTC == 10:30 ET, inside the 09:40-15:30 session gate.
_START_UTC = datetime(2026, 7, 6, 13, 30, 0)


def _base_frame(
    *,
    bars: int = 61,
    last_close: float = 98.0,
    last_volume: float = 5000.0,
    ema_8: float = 99.0,
    ema_12: float = 99.5,
    velocity: float = -1.5,
    volatile_prefix: bool = True,
    volatile_until: int = 20,
    last_minute_offset: int | None = None,
    symbol: str = "TSLA",
) -> pl.DataFrame:
    """Volatile opening phase, then a tight range, then a breakout bar.

    The tight tail keeps the short rolling stdev far below the long rolling
    stdev (which still spans the volatile phase), which is the compression
    state the strategy requires on the bar before the breakout.
    """
    closes: list[float] = []
    for index in range(bars - 1):
        if volatile_prefix and index < volatile_until:
            closes.append(100.0 + (1.0 if index % 2 == 0 else -1.0))
        else:
            closes.append(100.0 + (0.02 if index % 2 == 0 else -0.02))
    closes.append(last_close)

    highs = [close + 0.05 for close in closes]
    lows = [close - 0.05 for close in closes]
    volumes = [1000.0] * (bars - 1) + [last_volume]
    velocities = [0.0] * (bars - 1) + [velocity]
    timestamps = [_START_UTC + timedelta(minutes=index) for index in range(bars)]
    if last_minute_offset is not None:
        timestamps[-1] = _START_UTC + timedelta(minutes=last_minute_offset)

    return pl.DataFrame(
        {
            "symbol": [symbol] * bars,
            "timestamp": timestamps,
            "close": closes,
            "high": highs,
            "low": lows,
            "volume": volumes,
            "ema_8": [ema_8] * bars,
            "ema_12": [ema_12] * bars,
            "velocity_3": velocities,
            "velocity_5": velocities,
            "volume_ma_20": [1000.0] * bars,
        }
    )


def test_compression_breakout_short_fires_after_compression_and_breakdown() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    frame = _base_frame()

    decision = strategy.evaluate_entry(frame, "compression_test", TSLA_SHORT_PARAMS)

    assert decision.signal is True
    assert decision.direction.value == "short"
    assert "compression_ok" in decision.reason
    assert "compression_breakout_short" in decision.reason
    assert decision.features["prev_short_vol"] <= 0.7 * decision.features["prev_long_vol"]


def test_compression_breakout_blocked_without_compression() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    # No volatile prefix: short and long stdev are alike, ratio ~1 > 0.7.
    frame = _base_frame(volatile_prefix=False)
    params = dict(TSLA_SHORT_PARAMS, compression_window=5, breakout_lookback=5)

    decision = strategy.evaluate_entry(frame, "compression_test", params)

    assert decision.signal is False
    assert "compression_blocked" in decision.reason


def test_compression_breakout_blocked_without_breakout() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    # Close stays inside the prior 20-bar range.
    frame = _base_frame(last_close=99.99)

    decision = strategy.evaluate_entry(frame, "compression_test", TSLA_SHORT_PARAMS)

    assert decision.signal is False
    assert "breakout_blocked_short" in decision.reason


def test_compression_breakout_blocked_when_ema_bias_is_bullish() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    frame = _base_frame(ema_8=99.5, ema_12=99.0)

    decision = strategy.evaluate_entry(frame, "compression_test", TSLA_SHORT_PARAMS)

    assert decision.signal is False
    assert "ema_bias_blocked_short" in decision.reason


def test_compression_breakout_blocked_when_velocity_positive() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    frame = _base_frame(velocity=0.5)

    decision = strategy.evaluate_entry(frame, "compression_test", TSLA_SHORT_PARAMS)

    assert decision.signal is False
    assert "velocity_blocked_short" in decision.reason


def test_compression_breakout_velocity_zero_is_strict_boundary() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    frame = _base_frame(velocity=0.0)

    decision = strategy.evaluate_entry(frame, "compression_test", TSLA_SHORT_PARAMS)

    assert decision.signal is False


def test_compression_breakout_volume_gate_blocks_and_disabling_it_fires() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    # 1100 <= 1.2 * 1000: gate blocks when the filter is on.
    frame = _base_frame(last_volume=1100.0)

    gated = strategy.evaluate_entry(frame, "compression_test", TSLA_SHORT_PARAMS)
    ungated = strategy.evaluate_entry(
        frame, "compression_test", dict(TSLA_SHORT_PARAMS, use_volume_filter=False)
    )

    assert gated.signal is False
    assert "volume_gate_blocked" in gated.reason
    assert ungated.signal is True
    assert "volume_filter_disabled" in ungated.reason


def test_compression_breakout_blocked_before_session_start() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    # 13:35 UTC == 09:35 ET < 09:40 session start.
    frame = _base_frame(last_minute_offset=5)

    decision = strategy.evaluate_entry(frame, "compression_test", TSLA_SHORT_PARAMS)

    assert decision.signal is False
    assert "time_window_blocked" in decision.reason


def test_compression_breakout_long_side_with_direction_filter() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    frame = _base_frame(last_close=102.0, ema_8=100.5, ema_12=100.0, velocity=1.5)

    long_allowed = strategy.evaluate_entry(
        frame, "compression_test", dict(TSLA_SHORT_PARAMS, direction="long")
    )
    short_only = strategy.evaluate_entry(frame, "compression_test", TSLA_SHORT_PARAMS)

    assert long_allowed.signal is True
    assert long_allowed.direction.value == "long"
    assert short_only.signal is False


def test_compression_breakout_amd_vpoc_migration_params_fire() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    # compression_window=30 needs a 90-bar long window that still spans the
    # volatile phase while the trailing 30 bars are tight.
    frame = _base_frame(bars=115, volatile_until=35, symbol="AMD")

    decision = strategy.evaluate_entry(frame, "vpoc_migration_test", AMD_SHORT_PARAMS)

    assert decision.signal is True
    assert decision.direction.value == "short"


def test_compression_breakout_required_features() -> None:
    strategy = CompressionExpansionBreakoutStrategy()

    with_volume = strategy.required_features(TSLA_SHORT_PARAMS)
    without_volume = strategy.required_features(AMD_SHORT_PARAMS)

    assert {"timestamp", "symbol", "close", "high", "low", "ema_8", "ema_12", "volume"} <= with_volume
    assert "velocity_3" in with_volume
    assert "volume_ma_20" in with_volume
    assert "velocity_5" in without_volume
    assert not any(feature.startswith("volume_ma") for feature in without_volume)


def test_compression_breakout_features_resolve_through_newton_engine() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    raw = pl.DataFrame(
        {
            "symbol": ["TSLA"] * 80,
            "timestamp": [_START_UTC + timedelta(minutes=index) for index in range(80)],
            "open": [100.0] * 80,
            "high": [100.1] * 80,
            "low": [99.9] * 80,
            "close": [100.0 + 0.01 * index for index in range(80)],
            "volume": [1000.0] * 80,
        }
    )

    enriched = PhysicsEngine().enrich_for_features(raw, strategy.required_features(TSLA_SHORT_PARAMS))

    concrete = {feature for feature in strategy.required_features(TSLA_SHORT_PARAMS) if ":" not in feature}
    assert concrete <= set(enriched.columns)
    decision = strategy.evaluate_entry(enriched, "compression_engine_test", TSLA_SHORT_PARAMS)
    assert decision.signal is False


def test_compression_breakout_missing_columns_fail_loudly() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    frame = _base_frame().drop("velocity_3")

    with pytest.raises(ValueError, match="requires columns"):
        strategy.evaluate_entry(frame, "compression_test", TSLA_SHORT_PARAMS)


def test_compression_breakout_exit_is_hold() -> None:
    strategy = CompressionExpansionBreakoutStrategy()
    frame = _base_frame()

    decision = strategy.evaluate_exit(
        frame,
        "compression_test",
        TSLA_SHORT_PARAMS,
        TrackedPosition(
            symbol="TSLA",
            deployment_id="compression_test",
            option_symbol="TSLA260710P00300000",
            quantity=1,
        ),
    )

    assert decision.exit is False
    assert decision.action == "hold"
    assert "strategy_exit_not_configured" in decision.reason

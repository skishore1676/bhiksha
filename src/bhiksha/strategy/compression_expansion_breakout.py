"""Compression Expansion Breakout strategy plugin for the live runtime.

Semantic source of truth: mala_v2 ``src/strategy/compression_breakout.py``
(strategy key ``compression_expansion_breakout``; covers the
compression-breakout and vpoc-migration discovery catalog rows).

Setup: volatility compression (short rolling stdev of close at or below
``compression_factor`` times the long rolling stdev on the prior bar) followed
by a breakout beyond the prior ``breakout_lookback`` bar extreme, gated by EMA
trend bias, price velocity, an optional volume gate, and a session-time gate.
"""

from __future__ import annotations

import math
from typing import Any

import polars as pl

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.market_data.newton.transforms import validate_periods_back, velocity_column_name
from bhiksha.market_data.session import as_et_time
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.base import coerce_time


class CompressionExpansionBreakoutStrategy:
    """Evaluate the latest bar for a post-compression directional breakout."""

    key = "compression_expansion_breakout"

    def required_features(self, params: dict[str, Any]) -> set[str]:
        velocity_periods_back = validate_periods_back(int(params.get("velocity_periods_back", 1)))
        required = {
            "timestamp",
            "symbol",
            "close",
            "high",
            "low",
            "ema_8",
            "ema_12",
            velocity_column_name(velocity_periods_back),
            "volume",
        }
        if bool(params.get("use_volume_filter", True)):
            volume_ma_period = int(params.get("volume_ma_period", 20))
            required.add(f"volume_ma_{volume_ma_period}")
        return required

    def evaluate_entry(self, frame: pl.DataFrame, deployment_id: str, params: dict[str, Any]) -> SignalDecision:
        if frame.is_empty():
            raise ValueError("Cannot evaluate Compression Expansion Breakout on an empty frame")

        required = self.required_features(params)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Compression Expansion Breakout requires columns: {sorted(missing)}")

        direction_filter = str(params.get("direction", "")).strip().lower() or None
        compression_window = int(params.get("compression_window", 20))
        breakout_lookback = int(params.get("breakout_lookback", 20))
        compression_factor = float(params.get("compression_factor", 0.8))
        velocity_periods_back = validate_periods_back(int(params.get("velocity_periods_back", 1)))
        velocity_col = velocity_column_name(velocity_periods_back)
        use_volume_filter = bool(params.get("use_volume_filter", True))
        volume_ma_period = int(params.get("volume_ma_period", 20))
        volume_ma_col = f"volume_ma_{volume_ma_period}"
        volume_multiplier = float(params.get("volume_multiplier", 1.2))
        use_time_filter = bool(params.get("use_time_filter", True))
        session_start = coerce_time(params.get("session_start", "09:40"))
        session_end = coerce_time(params.get("session_end", "15:30"))

        working = frame.with_columns(
            [
                pl.col("close").rolling_std(window_size=compression_window).alias("_short_vol"),
                pl.col("close").rolling_std(window_size=compression_window * 3).alias("_long_vol"),
                pl.col("high").rolling_max(window_size=breakout_lookback).shift(1).alias("_prior_high"),
                pl.col("low").rolling_min(window_size=breakout_lookback).shift(1).alias("_prior_low"),
            ]
        ).with_columns(
            [
                pl.col("_short_vol").shift(1).alias("_prev_short_vol"),
                pl.col("_long_vol").shift(1).alias("_prev_long_vol"),
            ]
        )
        latest = working.tail(1).to_dicts()[0]

        timestamp = latest["timestamp"]
        close = _as_float(latest.get("close"))
        ema_8 = _as_float(latest.get("ema_8"))
        ema_12 = _as_float(latest.get("ema_12"))
        velocity = _as_float(latest.get(velocity_col))
        volume = _as_float(latest.get("volume"))
        volume_ma = _as_float(latest.get(volume_ma_col)) if use_volume_filter else None
        short_vol = _as_float(latest.get("_short_vol"))
        long_vol = _as_float(latest.get("_long_vol"))
        prev_short_vol = _as_float(latest.get("_prev_short_vol"))
        prev_long_vol = _as_float(latest.get("_prev_long_vol"))
        prior_high = _as_float(latest.get("_prior_high"))
        prior_low = _as_float(latest.get("_prior_low"))

        # Mirror mala_v2: prior-bar compression ratio, with the current bar's
        # rolling stats also required to be non-null.
        compression_ok = (
            prev_short_vol is not None
            and prev_long_vol is not None
            and prev_short_vol <= compression_factor * prev_long_vol
            and short_vol is not None
            and long_vol is not None
        )

        if use_volume_filter:
            volume_ok = volume is not None and volume_ma is not None and volume > volume_multiplier * volume_ma
        else:
            volume_ok = True

        bar_time_et = as_et_time(timestamp)
        time_ok = not use_time_filter or (session_start <= bar_time_et <= session_end)

        bullish_bias = ema_8 is not None and ema_12 is not None and ema_8 > ema_12
        bearish_bias = ema_8 is not None and ema_12 is not None and ema_8 < ema_12
        long_breakout = close is not None and prior_high is not None and close > prior_high
        short_breakout = close is not None and prior_low is not None and close < prior_low
        trigger_long = velocity is not None and velocity > 0
        trigger_short = velocity is not None and velocity < 0

        long_candidate = compression_ok and bullish_bias and long_breakout and trigger_long and volume_ok and time_ok
        short_candidate = compression_ok and bearish_bias and short_breakout and trigger_short and volume_ok and time_ok

        reasons: list[str] = [
            "time_window_ok" if time_ok else "time_window_blocked",
            "compression_ok" if compression_ok else "compression_blocked",
        ]
        if use_volume_filter:
            reasons.append("volume_gate_ok" if volume_ok else "volume_gate_blocked")
        else:
            reasons.append("volume_filter_disabled")
        if not use_time_filter:
            reasons.append("time_filter_disabled")

        signal = False
        direction: SignalDirection | None = None
        if long_candidate and direction_filter in (None, "long"):
            signal = True
            direction = SignalDirection.LONG
            reasons.extend(["ema_bias_bullish", "breakout_above_prior_high", "velocity_positive", "compression_breakout_long"])
        elif short_candidate and direction_filter in (None, "short"):
            signal = True
            direction = SignalDirection.SHORT
            reasons.extend(["ema_bias_bearish", "breakout_below_prior_low", "velocity_negative", "compression_breakout_short"])

        if not signal:
            if direction_filter in (None, "long"):
                reasons.append("ema_bias_bullish" if bullish_bias else "ema_bias_blocked_long")
                reasons.append("breakout_above_prior_high" if long_breakout else "breakout_blocked_long")
                reasons.append("velocity_positive" if trigger_long else "velocity_blocked_long")
            if direction_filter in (None, "short"):
                reasons.append("ema_bias_bearish" if bearish_bias else "ema_bias_blocked_short")
                reasons.append("breakout_below_prior_low" if short_breakout else "breakout_blocked_short")
                reasons.append("velocity_negative" if trigger_short else "velocity_blocked_short")

        return SignalDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=timestamp,
            signal=signal,
            direction=direction,
            reason=reasons,
            features={
                "close": close,
                "ema_8": ema_8,
                "ema_12": ema_12,
                velocity_col: velocity,
                "volume": volume,
                volume_ma_col: volume_ma,
                "short_vol": short_vol,
                "long_vol": long_vol,
                "prev_short_vol": prev_short_vol,
                "prev_long_vol": prev_long_vol,
                "prior_high": prior_high,
                "prior_low": prior_low,
            },
        )

    def evaluate_exit(
        self,
        frame: pl.DataFrame,
        deployment_id: str,
        params: dict[str, Any],
        position: TrackedPosition,
    ) -> ExitDecision:
        if frame.is_empty():
            raise ValueError("Cannot evaluate Compression Expansion Breakout exit on an empty frame")

        latest = frame.tail(1).to_dicts()[0]
        return ExitDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["strategy_exit_not_configured"],
            features={
                "position_option_symbol": position.option_symbol,
            },
        )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if math.isnan(numeric):
        return None
    return numeric

"""Jerk-Pivot Momentum strategy plugin for the live runtime."""

from __future__ import annotations

import math
from typing import Any

import polars as pl

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.market_data.session import as_et_time
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.base import coerce_time


class JerkPivotMomentumStrategy:
    """Evaluate the latest bar for the jerk-inflection VPOC momentum setup."""

    key = "jerk_pivot_momentum"

    def required_features(self, params: dict[str, Any]) -> set[str]:
        volume_ma_period = int(params.get("volume_ma_period", 20))
        required = {
            "timestamp",
            "symbol",
            "close",
            "velocity_1m",
            "accel_1m",
            "jerk_1m",
            "vpoc_4h",
            "volume",
        }
        if bool(params.get("use_volume_filter", True)):
            required.add(f"volume_ma_{volume_ma_period}")
        return required

    def evaluate_entry(self, frame: pl.DataFrame, deployment_id: str, params: dict[str, Any]) -> SignalDecision:
        if frame.is_empty():
            raise ValueError("Cannot evaluate Jerk Pivot Momentum on an empty frame")

        required = self.required_features(params)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Jerk Pivot Momentum requires columns: {sorted(missing)}")

        jerk_lookback = max(int(params.get("jerk_lookback", 20)), 1)
        volume_ma_period = int(params.get("volume_ma_period", 20))
        volume_ma_col = f"volume_ma_{volume_ma_period}"
        working = frame.with_columns(
            pl.col("jerk_1m").rolling_mean(window_size=jerk_lookback).alias("_jerk_smooth")
        ).with_columns(
            pl.col("_jerk_smooth").shift(1).alias("_prev_jerk_smooth")
        )
        latest = working.tail(1).to_dicts()[0]

        direction_filter = str(params.get("direction", "")).strip().lower() or None
        vpoc_proximity_pct = float(params.get("vpoc_proximity_pct", 0.005))
        volume_multiplier = float(params.get("volume_multiplier", 1.0))
        use_volume_filter = bool(params.get("use_volume_filter", True))
        use_time_filter = bool(params.get("use_time_filter", True))
        session_start = coerce_time(params.get("session_start", "09:35"))
        session_end = coerce_time(params.get("session_end", "15:30"))

        timestamp = latest["timestamp"]
        close = _as_float(latest.get("close"))
        vpoc = _as_float(latest.get("vpoc_4h"))
        velocity = _as_float(latest.get("velocity_1m"))
        accel = _as_float(latest.get("accel_1m"))
        jerk_smooth = _as_float(latest.get("_jerk_smooth"))
        prev_jerk_smooth = _as_float(latest.get("_prev_jerk_smooth"))
        volume = _as_float(latest.get("volume"))
        volume_ma = _as_float(latest.get(volume_ma_col))

        vpoc_dist_pct = None
        if close is not None and vpoc not in (None, 0.0):
            vpoc_dist_pct = abs(close - vpoc) / vpoc
        near_vpoc = vpoc_dist_pct is not None and vpoc_dist_pct <= vpoc_proximity_pct
        above_vpoc = close is not None and vpoc is not None and close >= vpoc
        below_vpoc = close is not None and vpoc is not None and close <= vpoc
        long_kinematic = velocity is not None and accel is not None and velocity > 0 and accel > 0
        short_kinematic = velocity is not None and accel is not None and velocity < 0 and accel < 0
        jerk_cross_up = (
            jerk_smooth is not None
            and prev_jerk_smooth is not None
            and jerk_smooth > 0
            and prev_jerk_smooth <= 0
        )
        jerk_cross_down = (
            jerk_smooth is not None
            and prev_jerk_smooth is not None
            and jerk_smooth < 0
            and prev_jerk_smooth >= 0
        )

        if use_volume_filter:
            volume_ok = (
                volume is not None
                and volume_ma is not None
                and volume >= volume_multiplier * volume_ma
            )
        else:
            volume_ok = True

        bar_time_et = as_et_time(timestamp)
        time_ok = not use_time_filter or (session_start <= bar_time_et <= session_end)

        long_candidate = near_vpoc and above_vpoc and long_kinematic and jerk_cross_up and volume_ok and time_ok
        short_candidate = near_vpoc and below_vpoc and short_kinematic and jerk_cross_down and volume_ok and time_ok

        signal = False
        direction: SignalDirection | None = None
        reasons: list[str] = []

        if direction_filter == "long":
            reasons.extend(
                _directional_reasons(
                    "long",
                    time_ok=time_ok,
                    use_time_filter=use_time_filter,
                    volume_ok=volume_ok,
                    use_volume_filter=use_volume_filter,
                    near_vpoc=near_vpoc,
                    side_ok=above_vpoc,
                    velocity_ok=velocity is not None and velocity > 0,
                    accel_ok=accel is not None and accel > 0,
                    jerk_cross_ok=jerk_cross_up,
                )
            )
            if long_candidate:
                signal = True
                direction = SignalDirection.LONG
                reasons.append("jerk_pivot_long")
        elif direction_filter == "short":
            reasons.extend(
                _directional_reasons(
                    "short",
                    time_ok=time_ok,
                    use_time_filter=use_time_filter,
                    volume_ok=volume_ok,
                    use_volume_filter=use_volume_filter,
                    near_vpoc=near_vpoc,
                    side_ok=below_vpoc,
                    velocity_ok=velocity is not None and velocity < 0,
                    accel_ok=accel is not None and accel < 0,
                    jerk_cross_ok=jerk_cross_down,
                )
            )
            if short_candidate:
                signal = True
                direction = SignalDirection.SHORT
                reasons.append("jerk_pivot_short")
        else:
            reasons.extend(
                _shared_reasons(
                    time_ok=time_ok,
                    use_time_filter=use_time_filter,
                    volume_ok=volume_ok,
                    use_volume_filter=use_volume_filter,
                    near_vpoc=near_vpoc,
                )
            )
            if long_candidate:
                signal = True
                direction = SignalDirection.LONG
                reasons.extend([
                    "above_vpoc",
                    "velocity_positive",
                    "accel_positive",
                    "jerk_cross_up",
                    "jerk_pivot_long",
                ])
            elif short_candidate:
                signal = True
                direction = SignalDirection.SHORT
                reasons.extend([
                    "below_vpoc",
                    "velocity_negative",
                    "accel_negative",
                    "jerk_cross_down",
                    "jerk_pivot_short",
                ])
            else:
                reasons.extend(["long_setup_blocked", "short_setup_blocked"])

        return SignalDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=timestamp,
            signal=signal,
            direction=direction,
            reason=reasons,
            features={
                "close": close,
                "vpoc_4h": vpoc,
                "vpoc_dist_pct": vpoc_dist_pct,
                "velocity_1m": velocity,
                "accel_1m": accel,
                "jerk_smooth_1m": jerk_smooth,
                "prev_jerk_smooth_1m": prev_jerk_smooth,
                "volume": volume,
                volume_ma_col: volume_ma,
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
            raise ValueError("Cannot evaluate Jerk Pivot Momentum exit on an empty frame")

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


def _shared_reasons(
    *,
    time_ok: bool,
    use_time_filter: bool,
    volume_ok: bool,
    use_volume_filter: bool,
    near_vpoc: bool,
) -> list[str]:
    reasons = [
        "time_window_ok" if time_ok else "time_window_blocked",
        "near_vpoc" if near_vpoc else "vpoc_proximity_blocked",
    ]
    if use_volume_filter:
        reasons.append("volume_gate_ok" if volume_ok else "volume_gate_blocked")
    else:
        reasons.append("volume_filter_disabled")
    if not use_time_filter:
        reasons.append("time_filter_disabled")
    return reasons


def _directional_reasons(
    direction: str,
    *,
    time_ok: bool,
    use_time_filter: bool,
    volume_ok: bool,
    use_volume_filter: bool,
    near_vpoc: bool,
    side_ok: bool,
    velocity_ok: bool,
    accel_ok: bool,
    jerk_cross_ok: bool,
) -> list[str]:
    reasons = _shared_reasons(
        time_ok=time_ok,
        use_time_filter=use_time_filter,
        volume_ok=volume_ok,
        use_volume_filter=use_volume_filter,
        near_vpoc=near_vpoc,
    )
    direction_label = "above_vpoc" if direction == "long" else "below_vpoc"
    velocity_label = "velocity_positive" if direction == "long" else "velocity_negative"
    accel_label = "accel_positive" if direction == "long" else "accel_negative"
    jerk_label = "jerk_cross_up" if direction == "long" else "jerk_cross_down"
    reasons.append(direction_label if side_ok else "side_of_vpoc_blocked")
    reasons.append(velocity_label if velocity_ok else "velocity_alignment_blocked")
    reasons.append(accel_label if accel_ok else "accel_alignment_blocked")
    reasons.append(jerk_label if jerk_cross_ok else "jerk_crossover_blocked")
    return reasons


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if math.isnan(numeric):
        return None
    return numeric

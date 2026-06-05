"""Evaluate underlying-anchored thesis exits from deployment payload metadata.

Supported thesis_exit_policy values
-------------------------------------
fixed_rr_underlying
    Classic stop-loss + take-profit anchored to the underlying price at entry.
    Params: stop_loss_underlying_pct, take_profit_underlying_r_multiple

trailing_vma_underlying
    Exit when close crosses the strategy's VWMA column (Market Impulse native).
    Params: vma_col

time_stop_underlying
    Exit at a specific wall-clock time (ET). Any bar whose timestamp falls at or
    after that time triggers square-off.
    Params: exit_time_et  (e.g. "14:30")

hold_to_eod_underlying
    Exit at end-of-day (15:55 ET). Equivalent to time_stop_underlying at 15:55.
    Params: none (uses hard_flat_time_et from the exit spec, default 15:55)

ma_trailing_underlying
    Exit when close crosses a moving-average column in the adverse direction.
    Params: ma_col  (e.g. "ema_12_exit")

ma_crossover_underlying
    Exit when a fast MA crosses a slow MA in the adverse direction (trend-end signal).
    Params: fast_ma_col, slow_ma_col  (e.g. "ema_8_exit", "ema_20_exit")

atr_trailing_underlying
    Exit when price retraces more than atr_multiple × ATR from the extreme price
    reached since entry (trailing high for shorts, trailing low for longs).
    Params: atr_col  (e.g. "atr_14_exit"), atr_multiple  (e.g. 2.0)
    Requires the frame to contain atr_col values and position.entry_timestamp.
"""

from __future__ import annotations

from datetime import datetime as dt_datetime
from datetime import time as dt_time
from typing import Any

import polars as pl

from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import ExitDecision
from bhiksha.market_data.session import as_et_time
from bhiksha.state.position_tracker import TrackedPosition

# Default EOD flat time used by hold_to_eod_underlying when no explicit time is set.
_DEFAULT_EOD_TIME = dt_time(15, 55)


def evaluate_underlying_thesis_exit(
    deployment: DeploymentManifest,
    frame: pl.DataFrame,
    position: TrackedPosition,
) -> ExitDecision | None:
    policy = deployment.exit.thesis_exit_policy
    if not policy:
        return None
    if frame.is_empty():
        return None

    latest = frame.tail(1).to_dicts()[0]
    if policy == "fixed_rr_underlying":
        return _evaluate_fixed_rr_underlying(deployment, latest, position)
    if policy == "trailing_vma_underlying":
        return _evaluate_trailing_vma_underlying(deployment, latest)
    if policy == "time_stop_underlying":
        return _evaluate_time_stop_underlying(deployment, latest)
    if policy == "hold_to_eod_underlying":
        return _evaluate_hold_to_eod_underlying(deployment, latest)
    if policy == "ma_trailing_underlying":
        return _evaluate_ma_trailing_underlying(deployment, latest)
    if policy == "ma_crossover_underlying":
        return _evaluate_ma_crossover_underlying(deployment, latest)
    if policy == "atr_trailing_underlying":
        return _evaluate_atr_trailing_underlying(deployment, frame, latest, position)
    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=str(latest["symbol"]),
        timestamp=latest["timestamp"],
        exit=False,
        action="hold",
        reason=[f"unsupported_thesis_exit_policy:{policy}"],
    )


def _evaluate_fixed_rr_underlying(
    deployment: DeploymentManifest,
    latest: dict[str, Any],
    position: TrackedPosition,
) -> ExitDecision:
    if position.underlying_entry_price is None:
        return ExitDecision(
            deployment_id=deployment.deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["underlying_entry_price_unavailable"],
        )

    params = deployment.exit.thesis_exit_params
    stop_loss_pct = _as_float(params.get("stop_loss_underlying_pct"))
    reward_multiple = _as_float(params.get("take_profit_underlying_r_multiple"))
    if stop_loss_pct is None or reward_multiple is None:
        return ExitDecision(
            deployment_id=deployment.deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["fixed_rr_underlying_params_missing"],
        )

    direction = str(deployment.strategy.params.get("direction", "")).lower()
    entry_price = position.underlying_entry_price
    risk_distance = entry_price * stop_loss_pct
    high = _as_float(latest.get("high"))
    low = _as_float(latest.get("low"))
    if high is None or low is None or risk_distance <= 0:
        return ExitDecision(
            deployment_id=deployment.deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["underlying_bar_unavailable"],
        )

    if direction == "long":
        stop_price = entry_price - risk_distance
        target_price = entry_price + (risk_distance * reward_multiple)
        if low <= stop_price:
            return _square_off(
                deployment=deployment,
                latest=latest,
                reasons=["thesis_stop_loss_underlying"],
            )
        if high >= target_price:
            return _square_off(
                deployment=deployment,
                latest=latest,
                reasons=["thesis_take_profit_underlying"],
            )
    else:
        stop_price = entry_price + risk_distance
        target_price = entry_price - (risk_distance * reward_multiple)
        if high >= stop_price:
            return _square_off(
                deployment=deployment,
                latest=latest,
                reasons=["thesis_stop_loss_underlying"],
            )
        if low <= target_price:
            return _square_off(
                deployment=deployment,
                latest=latest,
                reasons=["thesis_take_profit_underlying"],
            )

    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=str(latest["symbol"]),
        timestamp=latest["timestamp"],
        exit=False,
        action="hold",
        reason=["thesis_exit_hold"],
    )


def _evaluate_trailing_vma_underlying(
    deployment: DeploymentManifest,
    latest: dict[str, Any],
) -> ExitDecision:
    params = deployment.exit.thesis_exit_params
    vma_col = str(params.get("vma_col") or "")
    close = _as_float(latest.get("close"))
    vma_value = _as_float(latest.get(vma_col))
    if not vma_col or close is None or vma_value is None:
        return ExitDecision(
            deployment_id=deployment.deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["trailing_vma_underlying_params_missing"],
        )

    direction = str(deployment.strategy.params.get("direction", "")).lower()
    if direction == "long" and close < vma_value:
        return _square_off(
            deployment=deployment,
            latest=latest,
            reasons=["thesis_vma_loss_underlying"],
        )
    if direction != "long" and close > vma_value:
        return _square_off(
            deployment=deployment,
            latest=latest,
            reasons=["thesis_vma_reclaim_underlying"],
        )
    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=str(latest["symbol"]),
        timestamp=latest["timestamp"],
        exit=False,
        action="hold",
        reason=["thesis_exit_hold"],
    )


def _evaluate_time_stop_underlying(
    deployment: DeploymentManifest,
    latest: dict[str, Any],
) -> ExitDecision:
    """Square off when the bar timestamp reaches or passes the configured exit time."""
    params = deployment.exit.thesis_exit_params
    exit_time_str = str(params.get("exit_time_et") or "")
    if not exit_time_str:
        # Misconfigured — fall back to EOD
        exit_time = _DEFAULT_EOD_TIME
    else:
        try:
            parts = exit_time_str.split(":")
            exit_time = dt_time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            exit_time = _DEFAULT_EOD_TIME

    ts = latest.get("timestamp")
    if ts is None:
        return ExitDecision(
            deployment_id=deployment.deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["time_stop_underlying_no_timestamp"],
        )

    bar_time = _bar_time_et(ts)

    if bar_time >= exit_time:
        return _square_off(
            deployment=deployment,
            latest=latest,
            reasons=[f"time_stop_underlying:{exit_time.strftime('%H%M')}"],
        )
    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=str(latest["symbol"]),
        timestamp=latest["timestamp"],
        exit=False,
        action="hold",
        reason=["thesis_exit_hold"],
    )


def _evaluate_hold_to_eod_underlying(
    deployment: DeploymentManifest,
    latest: dict[str, Any],
) -> ExitDecision:
    """Exit at EOD. Uses hard_flat_time_et from the exit spec (default 15:55)."""
    hard_flat = str(deployment.exit.hard_flat_time_et or "15:55")
    try:
        parts = hard_flat.split(":")
        eod_time = dt_time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        eod_time = _DEFAULT_EOD_TIME

    ts = latest.get("timestamp")
    if ts is None:
        return ExitDecision(
            deployment_id=deployment.deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["hold_to_eod_underlying_no_timestamp"],
        )

    bar_time = _bar_time_et(ts)

    if bar_time >= eod_time:
        return _square_off(
            deployment=deployment,
            latest=latest,
            reasons=["hold_to_eod_underlying"],
        )
    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=str(latest["symbol"]),
        timestamp=latest["timestamp"],
        exit=False,
        action="hold",
        reason=["thesis_exit_hold"],
    )


def _evaluate_ma_trailing_underlying(
    deployment: DeploymentManifest,
    latest: dict[str, Any],
) -> ExitDecision:
    """Exit when close crosses the MA column in the adverse direction."""
    params = deployment.exit.thesis_exit_params
    ma_col = str(params.get("ma_col") or "")
    close = _as_float(latest.get("close"))
    ma_value = _as_float(latest.get(ma_col))

    if not ma_col or close is None or ma_value is None:
        return ExitDecision(
            deployment_id=deployment.deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["ma_trailing_underlying_params_missing"],
        )

    direction = str(deployment.strategy.params.get("direction", "")).lower()
    if direction == "long" and close < ma_value:
        return _square_off(
            deployment=deployment,
            latest=latest,
            reasons=[f"ma_trailing_underlying_loss:{ma_col}"],
        )
    if direction != "long" and close > ma_value:
        return _square_off(
            deployment=deployment,
            latest=latest,
            reasons=[f"ma_trailing_underlying_reclaim:{ma_col}"],
        )
    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=str(latest["symbol"]),
        timestamp=latest["timestamp"],
        exit=False,
        action="hold",
        reason=["thesis_exit_hold"],
    )


def _evaluate_ma_crossover_underlying(
    deployment: DeploymentManifest,
    latest: dict[str, Any],
) -> ExitDecision:
    """Exit when fast MA crosses slow MA in the adverse direction (trend-end signal)."""
    params = deployment.exit.thesis_exit_params
    fast_col = str(params.get("fast_ma_col") or "")
    slow_col = str(params.get("slow_ma_col") or "")
    fast_val = _as_float(latest.get(fast_col))
    slow_val = _as_float(latest.get(slow_col))

    if not fast_col or not slow_col or fast_val is None or slow_val is None:
        return ExitDecision(
            deployment_id=deployment.deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["ma_crossover_underlying_params_missing"],
        )

    direction = str(deployment.strategy.params.get("direction", "")).lower()
    # For a long position, exit when fast MA falls below slow MA (bearish crossover).
    # For a short position, exit when fast MA rises above slow MA (bullish crossover).
    if direction == "long" and fast_val < slow_val:
        return _square_off(
            deployment=deployment,
            latest=latest,
            reasons=[f"ma_crossover_underlying_bearish:{fast_col}>{slow_col}"],
        )
    if direction != "long" and fast_val > slow_val:
        return _square_off(
            deployment=deployment,
            latest=latest,
            reasons=[f"ma_crossover_underlying_bullish:{fast_col}>{slow_col}"],
        )
    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=str(latest["symbol"]),
        timestamp=latest["timestamp"],
        exit=False,
        action="hold",
        reason=["thesis_exit_hold"],
    )


def _evaluate_atr_trailing_underlying(
    deployment: DeploymentManifest,
    frame: pl.DataFrame,
    latest: dict[str, Any],
    position: TrackedPosition,
) -> ExitDecision:
    """ATR-based trailing stop from the extreme price reached since entry.

    Trailing mechanism:
        short: trail_stop = max(high since entry) + atr_multiple * ATR
               exit if current high >= trail_stop  (i.e. price wicks back too far)
        long:  trail_stop = min(low since entry) - atr_multiple * ATR
               exit if current low <= trail_stop
    """
    params = deployment.exit.thesis_exit_params
    atr_col    = str(params.get("atr_col") or "atr_14_exit")
    atr_mult   = _as_float(params.get("atr_multiple")) or 2.0
    direction  = str(deployment.strategy.params.get("direction", "")).lower()

    atr_value = _as_float(latest.get(atr_col))
    high      = _as_float(latest.get("high"))
    low       = _as_float(latest.get("low"))

    if atr_value is None or high is None or low is None or atr_value <= 0:
        return ExitDecision(
            deployment_id=deployment.deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["atr_trailing_underlying_data_missing"],
        )

    # Narrow the frame to bars since entry so we can find the extreme.
    since_entry = frame
    if position.entry_timestamp is not None and "timestamp" in frame.columns:
        try:
            since_entry = frame.filter(pl.col("timestamp") >= position.entry_timestamp)
        except Exception:
            pass  # If filter fails, use full frame (conservative)

    if direction == "long":
        # Trail stop below the lowest low reached since entry
        low_since_entry = _as_float(since_entry["low"].min()) if not since_entry.is_empty() else low
        if low_since_entry is None:
            low_since_entry = low
        trail_stop = low_since_entry - atr_mult * atr_value
        if low <= trail_stop:
            return _square_off(
                deployment=deployment,
                latest=latest,
                reasons=[f"atr_trailing_underlying_stop:{atr_col}x{atr_mult}"],
            )
    else:
        # Trail stop above the highest high reached since entry
        high_since_entry = _as_float(since_entry["high"].max()) if not since_entry.is_empty() else high
        if high_since_entry is None:
            high_since_entry = high
        trail_stop = high_since_entry + atr_mult * atr_value
        if high >= trail_stop:
            return _square_off(
                deployment=deployment,
                latest=latest,
                reasons=[f"atr_trailing_underlying_stop:{atr_col}x{atr_mult}"],
            )

    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=str(latest["symbol"]),
        timestamp=latest["timestamp"],
        exit=False,
        action="hold",
        reason=["thesis_exit_hold"],
    )


def _square_off(
    *,
    deployment: DeploymentManifest,
    latest: dict[str, Any],
    reasons: list[str],
) -> ExitDecision:
    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=str(latest["symbol"]),
        timestamp=latest["timestamp"],
        exit=True,
        action="square_off",
        reason=reasons,
        cancel_protection_orders=True,
    )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bar_time_et(value: Any) -> dt_time:
    if isinstance(value, dt_datetime):
        timestamp = value
    else:
        timestamp = dt_datetime.fromisoformat(str(value))
    return as_et_time(timestamp)

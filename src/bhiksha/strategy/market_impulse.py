"""Market Impulse strategy plugin for the live runtime."""

from __future__ import annotations

from datetime import time
from typing import Any

import polars as pl

from bhiksha.domain.models import ExitDecision
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import SignalDecision
from bhiksha.market_data.session import as_et_time
from bhiksha.state.position_tracker import TrackedPosition


class MarketImpulseStrategy:
    """Evaluate the latest bar for the cross-and-reclaim Market Impulse setup."""

    key = "market_impulse"

    def required_features(self, params: dict[str, Any]) -> set[str]:
        vma_length = int(params.get("vma_length", 10))
        regime_timeframe = str(params.get("regime_timeframe", "1h"))
        vwma_periods = _coerce_vwma_periods(params.get("vwma_periods"))
        if vwma_periods is not None:
            vwma_spec = "_".join(str(period) for period in vwma_periods)
            impulse_request = f"market_impulse:{regime_timeframe}:vma_{vma_length}:vwma_{vwma_spec}"
        else:
            impulse_request = f"market_impulse:{regime_timeframe}:vma_{vma_length}"
        return {
            "timestamp",
            "symbol",
            "close",
            "high",
            "low",
            impulse_request,
        }

    def evaluate_entry(self, frame: pl.DataFrame, deployment_id: str, params: dict[str, Any]) -> SignalDecision:
        if frame.is_empty():
            raise ValueError("Cannot evaluate Market Impulse on an empty frame")

        latest = frame.tail(1).to_dicts()[0]
        direction_filter = str(params.get("direction", "")).strip().lower() or None
        vma_length = int(params.get("vma_length", 10))
        regime_timeframe = str(params.get("regime_timeframe", "1h"))
        entry_buffer_minutes = int(params.get("entry_buffer_minutes", 3))
        entry_window_minutes = int(params.get("entry_window_minutes", 60))
        market_open_hour = int(params.get("market_open_hour", 9))
        market_open_minute = int(params.get("market_open_minute", 30))

        start_minutes = market_open_hour * 60 + market_open_minute + entry_buffer_minutes
        end_minutes = market_open_hour * 60 + market_open_minute + entry_window_minutes
        entry_start = time(start_minutes // 60, start_minutes % 60)
        entry_end = time(end_minutes // 60, end_minutes % 60)

        timestamp = latest["timestamp"]
        bar_time_et = as_et_time(timestamp)
        vma_col = f"vma_{vma_length}"
        regime_col = f"impulse_regime_{regime_timeframe}"

        reasons: list[str] = []
        if entry_start <= bar_time_et <= entry_end:
            reasons.append("time_window_ok")
        else:
            reasons.append("time_window_blocked")

        regime = str(latest[regime_col])
        vma = float(latest[vma_col])
        high = float(latest["high"])
        low = float(latest["low"])
        close = float(latest["close"])

        long_signal = regime == "bullish" and low <= vma and close > vma
        short_signal = regime == "bearish" and high >= vma and close < vma

        signal = False
        direction: SignalDirection | None = None

        if entry_start <= bar_time_et <= entry_end:
            if long_signal and direction_filter in (None, "long"):
                signal = True
                direction = SignalDirection.LONG
                reasons.extend(["regime_bullish", "cross_and_reclaim_long"])
            elif short_signal and direction_filter in (None, "short"):
                signal = True
                direction = SignalDirection.SHORT
                reasons.extend(["regime_bearish", "cross_and_reclaim_short"])

        if not signal:
            if regime == "bullish":
                reasons.append("regime_bullish")
            elif regime == "bearish":
                reasons.append("regime_bearish")
            else:
                reasons.append("regime_neutral")

        return SignalDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=timestamp,
            signal=signal,
            direction=direction,
            reason=reasons,
            features={
                "close": close,
                vma_col: vma,
                regime_col: regime,
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
            raise ValueError("Cannot evaluate Market Impulse exit on an empty frame")

        latest = frame.tail(1).to_dicts()[0]
        direction_filter = str(params.get("direction", "")).strip().lower() or None
        vma_length = int(params.get("vma_length", 10))
        regime_timeframe = str(params.get("regime_timeframe", "1h"))
        timestamp = latest["timestamp"]
        vma_col = f"vma_{vma_length}"
        regime_col = f"impulse_regime_{regime_timeframe}"
        vma = float(latest[vma_col])
        close = float(latest["close"])
        regime = str(latest[regime_col])

        reasons: list[str] = []
        exit_now = False
        action = "hold"

        if direction_filter in (None, "short"):
            if close > vma:
                exit_now = True
                action = "square_off"
                reasons.append("vma_reclaim_exit")
            elif regime == "bullish":
                exit_now = True
                action = "square_off"
                reasons.append("regime_flip_bullish_exit")
        elif direction_filter == "long":
            if close < vma:
                exit_now = True
                action = "square_off"
                reasons.append("vma_loss_exit")
            elif regime == "bearish":
                exit_now = True
                action = "square_off"
                reasons.append("regime_flip_bearish_exit")

        if not reasons:
            reasons.append("hold_position")

        return ExitDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=timestamp,
            exit=exit_now,
            action=action,
            reason=reasons,
            cancel_protection_orders=exit_now,
            features={
                vma_col: vma,
                regime_col: regime,
                "close": close,
                "position_option_symbol": position.option_symbol,
            },
        )


def _coerce_vwma_periods(value: Any) -> tuple[int, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw_parts = value.replace(",", "_").replace("-", "_").split("_")
    elif isinstance(value, (list, tuple)):
        raw_parts = list(value)
    else:
        return None
    try:
        periods = tuple(int(part) for part in raw_parts if str(part).strip())
    except (TypeError, ValueError):
        return None
    if len(periods) < 3:
        return None
    return periods

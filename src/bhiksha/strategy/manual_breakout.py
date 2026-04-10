"""Manual breakout strategy for operator-authored breakout deployments."""

from __future__ import annotations

import math
from typing import Any

import polars as pl

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.market_data.session import ET, ensure_utc
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.base import coerce_time
from bhiksha.strategy.manual_trigger import _as_float


class ManualBreakoutStrategy:
    """Fire on the first full 1-minute close through a configured trigger level."""

    key = "manual_breakout"

    def required_features(self, params: dict[str, Any]) -> set[str]:
        vma_length = int(params.get("vma_length", 10))
        vma_timeframe = str(params.get("vma_timeframe", "5m")).strip().lower() or "5m"
        return {"timestamp", "symbol", "close", f"vma_{vma_length}_{vma_timeframe}"}

    def evaluate_entry(self, frame: pl.DataFrame, deployment_id: str, params: dict[str, Any]) -> SignalDecision:
        if frame.is_empty():
            raise ValueError("Cannot evaluate Manual Breakout on an empty frame")

        required = self.required_features(params)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Manual Breakout requires columns: {sorted(missing)}")

        working = frame.with_columns(
            pl.col("timestamp")
            .map_elements(lambda ts: ensure_utc(ts).astimezone(ET).date(), return_dtype=pl.Date)
            .alias("_trade_date")
        )
        latest = working.tail(1).to_dicts()[0]
        current_day = latest["_trade_date"]
        day_frame = working.filter(pl.col("_trade_date") == current_day)
        trigger_price = float(params["trigger_price"])
        trigger_direction = str(params.get("trigger_direction", "ABOVE")).strip().upper()
        direction_filter = str(params.get("direction", "")).strip().lower() or "long"
        after_time_et = params.get("after_time_et")
        latest_close = _as_float(latest.get("close"))

        signal = False
        reasons = [
            f"trigger_direction={trigger_direction}",
            f"trigger_price={trigger_price}",
            f"close={latest_close}" if latest_close is not None else "close_unavailable",
        ]
        if after_time_et:
            reasons.append(f"after_time_et={after_time_et}")
            activation_time = coerce_time(after_time_et)
            day_frame = day_frame.filter(
                pl.col("timestamp")
                .map_elements(lambda ts: ensure_utc(ts).astimezone(ET).time(), return_dtype=pl.Time)
                >= activation_time
            )

        close_series = day_frame.get_column("close").cast(pl.Float64).to_list()
        timestamp = latest["timestamp"]
        if after_time_et:
            current_et_time = ensure_utc(timestamp).astimezone(ET).time()
            if current_et_time < activation_time:
                reasons.append("manual_breakout_waiting")
                return SignalDecision(
                    deployment_id=deployment_id,
                    symbol=str(latest["symbol"]),
                    timestamp=timestamp,
                    signal=False,
                    direction=SignalDirection.LONG if direction_filter == "long" else SignalDirection.SHORT,
                    reason=reasons,
                    features={
                        "close": latest_close,
                        "trigger_price": trigger_price,
                        "trigger_direction": trigger_direction,
                    },
                )

        prev_close = _as_float(close_series[-2]) if len(close_series) >= 2 else None
        if latest_close is not None:
            if trigger_direction == "ABOVE":
                signal = latest_close >= trigger_price and (prev_close is None or prev_close < trigger_price)
            elif trigger_direction == "BELOW":
                signal = latest_close <= trigger_price and (prev_close is None or prev_close > trigger_price)
            elif trigger_direction == "CLOSE_BY":
                close_by_factor = float(params.get("close_by_factor", 0.001))
                prev_within = (
                    prev_close is not None and abs(prev_close - trigger_price) / trigger_price <= close_by_factor
                )
                current_within = abs(latest_close - trigger_price) / trigger_price <= close_by_factor
                signal = current_within and not prev_within
            else:
                raise ValueError(f"Unsupported trigger_direction: {trigger_direction}")

        if signal:
            reasons.append("manual_breakout_triggered")
        else:
            reasons.append("manual_breakout_waiting")

        return SignalDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=timestamp,
            signal=signal,
            direction=SignalDirection.LONG if direction_filter == "long" else SignalDirection.SHORT,
            reason=reasons,
            features={
                "close": latest_close,
                "previous_close": prev_close,
                "trigger_price": trigger_price,
                "trigger_direction": trigger_direction,
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
            raise ValueError("Cannot evaluate Manual Breakout exit on an empty frame")

        required = self.required_features(params)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Manual Breakout requires columns: {sorted(missing)}")

        latest = frame.tail(1).to_dicts()[0]
        direction_filter = str(params.get("direction", "")).strip().lower() or "long"
        vma_length = int(params.get("vma_length", 10))
        vma_timeframe = str(params.get("vma_timeframe", "5m")).strip().lower() or "5m"
        vma_col = f"vma_{vma_length}_{vma_timeframe}"
        close = _as_float(latest.get("close"))
        vma_value = _as_float(latest.get(vma_col))

        reasons: list[str] = []
        should_exit = False
        if close is None or vma_value is None or math.isnan(vma_value):
            reasons.append("manual_breakout_vma_unavailable")
        elif direction_filter == "long":
            if close < vma_value:
                should_exit = True
                reasons.append("manual_breakout_vma_loss_exit")
            else:
                reasons.append("manual_breakout_hold")
        else:
            if close > vma_value:
                should_exit = True
                reasons.append("manual_breakout_vma_reclaim_exit")
            else:
                reasons.append("manual_breakout_hold")

        return ExitDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=should_exit,
            action="square_off" if should_exit else "hold",
            reason=reasons,
            cancel_protection_orders=should_exit,
            features={
                "close": close,
                vma_col: vma_value,
                "position_option_symbol": getattr(position, "option_symbol", None),
            },
        )

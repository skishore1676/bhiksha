"""Manual trigger strategy plugin for operator-authored session deployments."""

from __future__ import annotations

from datetime import datetime, time
import math
from typing import Any

import polars as pl

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.market_data.session import ET, ensure_utc
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.base import coerce_time


class ManualTriggerStrategy:
    """Fire a one-shot signal when the underlying crosses an operator price trigger."""

    key = "manual_trigger"

    def required_features(self, params: dict[str, Any]) -> set[str]:
        return {"timestamp", "symbol", "close"}

    def evaluate_entry(self, frame: pl.DataFrame, deployment_id: str, params: dict[str, Any]) -> SignalDecision:
        if frame.is_empty():
            raise ValueError("Cannot evaluate Manual Trigger on an empty frame")

        required = self.required_features(params)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Manual Trigger requires columns: {sorted(missing)}")

        trigger_price = float(params["trigger_price"])
        trigger_direction = str(params.get("trigger_direction", "ABOVE")).strip().upper()
        close_by_factor = float(params.get("close_by_factor", 0.001))
        after_time_et = params.get("after_time_et")
        direction_filter = str(params.get("direction", "")).strip().lower() or "long"

        working = frame.with_columns(
            pl.col("timestamp")
            .map_elements(lambda ts: ensure_utc(ts).astimezone(ET).date(), return_dtype=pl.Date)
            .alias("_trade_date")
        )

        signal_condition = _trigger_expr(
            trigger_price=trigger_price,
            trigger_direction=trigger_direction,
            close_by_factor=close_by_factor,
        )

        if after_time_et:
            activation_time = coerce_time(after_time_et)
            signal_condition = signal_condition & (
                pl.col("timestamp")
                .map_elements(lambda ts: ensure_utc(ts).astimezone(ET).time(), return_dtype=pl.Time)
                >= activation_time
            )

        working = working.with_columns(signal_condition.fill_null(False).alias("_raw_trigger")).with_columns(
            (
                pl.col("_raw_trigger")
                & (pl.col("_raw_trigger").cast(pl.Int64).cum_sum().over("_trade_date") == 1)
            ).alias("_first_trigger")
        )

        latest = working.tail(1).to_dicts()[0]
        close = _as_float(latest.get("close"))
        signal = bool(latest.get("_first_trigger", False))
        direction = SignalDirection.LONG if direction_filter == "long" else SignalDirection.SHORT

        reasons = [
            f"trigger_direction={trigger_direction}",
            f"trigger_price={trigger_price}",
            f"close={close}" if close is not None else "close_unavailable",
        ]
        if after_time_et:
            reasons.append(f"after_time_et={after_time_et}")
        if signal:
            reasons.append("manual_trigger_met")
        else:
            reasons.append("manual_trigger_waiting")

        return SignalDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            signal=signal,
            direction=direction,
            reason=reasons,
            features={
                "close": close,
                "trigger_price": trigger_price,
                "trigger_direction": trigger_direction,
                "close_by_factor": close_by_factor,
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
            raise ValueError("Cannot evaluate Manual Trigger exit on an empty frame")

        latest = frame.tail(1).to_dicts()[0]
        return ExitDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=latest["timestamp"],
            exit=False,
            action="hold",
            reason=["strategy_exit_not_configured"],
            features={"position_option_symbol": position.option_symbol},
        )


def _trigger_expr(*, trigger_price: float, trigger_direction: str, close_by_factor: float) -> pl.Expr:
    if trigger_direction == "ABOVE":
        return pl.col("close") >= trigger_price
    if trigger_direction == "BELOW":
        return pl.col("close") <= trigger_price
    if trigger_direction == "CLOSE_BY":
        return (
            (pl.col("close") - trigger_price).abs() / trigger_price
        ) <= close_by_factor
    raise ValueError(f"Unsupported trigger_direction: {trigger_direction}")


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if math.isnan(numeric):
        return None
    return numeric

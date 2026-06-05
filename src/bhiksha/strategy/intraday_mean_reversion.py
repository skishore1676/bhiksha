"""Bhiksha runtime adapter for the IWM/QQQ intraday mean-reversion playbook."""

from __future__ import annotations

from datetime import time
from typing import Any

import polars as pl

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.market_data.newton.transforms import jerk_column_name, velocity_column_name
from bhiksha.market_data.session import et_date_expr, et_time_expr
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.base import coerce_time


class IntradayMeanReversionStrategy:
    """Recompute the playbook entry decision against Bhiksha feature frames."""

    key = "intraday_mean_reversion_extremes"

    def required_features(self, params: dict[str, Any]) -> set[str]:
        velocity_periods_back = _positive_int(params.get("velocity_periods_back", 5))
        jerk_periods_back = _positive_int(params.get("jerk_periods_back", velocity_periods_back))
        stage_timeframe = str(params.get("stage_timeframe", "1m"))
        stretch_source = str(params.get("stretch_source", "opening_vwap_rth"))
        relative_volume_period = _positive_int(params.get("relative_volume_period", 20))

        features = {
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "opening_vwap_rth",
            "prior_rth_close",
            "daily_rth_atr_14",
            "gap_state_rth_open",
            velocity_column_name(velocity_periods_back),
            _market_stage_feature(stage_timeframe),
        }
        if stretch_source == "prior_rth_close_atr":
            features.add("atr_distance_from_prior_rth_close")
        elif stretch_source == "vpoc_4h":
            features.add("vpoc_4h")
        else:
            features.add("opening_vwap_rth")
        if bool(params.get("use_jerk_confirmation", True)):
            features.add(jerk_column_name(jerk_periods_back))
        if params.get("relative_volume_threshold") is not None:
            features.add(f"relative_volume_rth_{relative_volume_period}")
        return features

    def generate_signals(self, frame: pl.DataFrame, params: dict[str, Any]) -> pl.DataFrame:
        missing = self.required_features(params) - set(frame.columns)
        missing -= _alternate_stage_columns(frame, params)
        if _stage_column_available(frame, params):
            missing.discard(_market_stage_feature(str(params.get("stage_timeframe", "1m"))))
        if missing:
            raise ValueError(f"Mean reversion adapter requires columns: {sorted(missing)}")

        entry_window_start = coerce_time(params.get("entry_window_start", "09:30"))
        entry_window_end = coerce_time(params.get("entry_window_end", "10:15"))
        stretch_source = str(params.get("stretch_source", "opening_vwap_rth"))
        stretch_threshold = float(params.get("stretch_threshold", 2.0))
        z_score_window = max(5, int(params.get("z_score_window", 30)))
        reversal_range_minutes = max(2, int(params.get("reversal_range_minutes", 5)))
        confirming_bars = max(1, int(params.get("confirming_bars", 1)))
        velocity_periods_back = _positive_int(params.get("velocity_periods_back", 5))
        jerk_periods_back = _positive_int(params.get("jerk_periods_back", velocity_periods_back))
        velocity_filter = str(params.get("velocity_filter", "no_filter"))
        velocity_atr_threshold = abs(float(params.get("velocity_atr_threshold", 0.10)))
        stage_filter = str(params.get("stage_filter", "no_filter"))
        gap_state_filter = str(params.get("gap_state_filter", "no_filter"))
        relative_volume_period = _positive_int(params.get("relative_volume_period", 20))
        relative_volume_threshold = params.get("relative_volume_threshold")
        velocity_col = velocity_column_name(velocity_periods_back)
        jerk_col = jerk_column_name(jerk_periods_back)
        stage_col = _stage_column(frame, params)
        relvol_col = f"relative_volume_rth_{relative_volume_period}"

        context = (
            frame.with_columns(
                [
                    et_date_expr("timestamp").alias("_trade_date"),
                    et_time_expr("timestamp").alias("_bar_time"),
                ]
            )
            .with_columns(_stretch_raw_expr(stretch_source).alias("_stretch_raw"))
            .with_columns(_stretch_value_expr(stretch_source, z_score_window).alias("_stretch_value"))
            .with_columns(
                [
                    pl.col("high")
                    .rolling_max(window_size=reversal_range_minutes, min_samples=reversal_range_minutes)
                    .shift(1)
                    .over("_trade_date")
                    .alias("_reversal_high"),
                    pl.col("low")
                    .rolling_min(window_size=reversal_range_minutes, min_samples=reversal_range_minutes)
                    .shift(1)
                    .over("_trade_date")
                    .alias("_reversal_low"),
                    pl.col("_stretch_value")
                    .rolling_max(window_size=reversal_range_minutes, min_samples=reversal_range_minutes)
                    .shift(1)
                    .over("_trade_date")
                    .alias("_prev_max_stretch"),
                    pl.col("_stretch_value")
                    .rolling_min(window_size=reversal_range_minutes, min_samples=reversal_range_minutes)
                    .shift(1)
                    .over("_trade_date")
                    .alias("_prev_min_stretch"),
                    pl.col(velocity_col)
                    .rolling_max(window_size=reversal_range_minutes, min_samples=reversal_range_minutes)
                    .shift(1)
                    .over("_trade_date")
                    .alias("_prev_max_velocity"),
                    pl.col(velocity_col)
                    .rolling_min(window_size=reversal_range_minutes, min_samples=reversal_range_minutes)
                    .shift(1)
                    .over("_trade_date")
                    .alias("_prev_min_velocity"),
                ]
            )
            .with_columns(((pl.col("_reversal_high") + pl.col("_reversal_low")) / 2.0).alias("playbook_reversal_midpoint"))
            .with_columns(
                [
                    pl.col("_reversal_high").alias("playbook_reversal_high"),
                    pl.col("_reversal_low").alias("playbook_reversal_low"),
                    pl.lit(stretch_source).alias("playbook_stretch_source"),
                    pl.col("_stretch_raw").alias("playbook_stretch_raw"),
                    pl.col("_stretch_value").alias("playbook_stretch_value"),
                    pl.col("_prev_max_stretch").alias("playbook_prior_max_stretch"),
                    pl.col("_prev_min_stretch").alias("playbook_prior_min_stretch"),
                    _reference_price_expr(stretch_source).alias("playbook_reference_price"),
                ]
            )
        )

        time_filter = (pl.col("_bar_time") >= entry_window_start) & (
            pl.col("_bar_time") <= entry_window_end
        )
        stage_expr = pl.lit(True) if stage_filter == "no_filter" else (pl.col(stage_col) == stage_filter).fill_null(False)
        gap_expr = (
            pl.lit(True)
            if gap_state_filter == "no_filter"
            else (pl.col("gap_state_rth_open") == gap_state_filter).fill_null(False)
        )
        volume_expr = (
            pl.lit(True)
            if relative_volume_threshold is None
            else (pl.col(relvol_col) >= float(relative_volume_threshold)).fill_null(False)
        )

        long_breakout = pl.col("close") > pl.col("_reversal_high")
        short_breakout = pl.col("close") < pl.col("_reversal_low")
        if confirming_bars >= 2:
            long_breakout = long_breakout & (
                pl.col("close").shift(1).over("_trade_date")
                > pl.col("_reversal_high").shift(1).over("_trade_date")
            )
            short_breakout = short_breakout & (
                pl.col("close").shift(1).over("_trade_date")
                < pl.col("_reversal_low").shift(1).over("_trade_date")
            )

        long_velocity = _velocity_filter_expr("long", velocity_filter, velocity_atr_threshold)
        short_velocity = _velocity_filter_expr("short", velocity_filter, velocity_atr_threshold)
        if bool(params.get("use_jerk_confirmation", True)):
            long_jerk = pl.col(jerk_col) > 0
            short_jerk = pl.col(jerk_col) < 0
        else:
            long_jerk = pl.lit(True)
            short_jerk = pl.lit(True)

        long_signal = (
            pl.lit(bool(params.get("allow_long", True)))
            & time_filter
            & stage_expr
            & gap_expr
            & volume_expr
            & (pl.col("_prev_min_stretch") <= -stretch_threshold)
            & (pl.col("_stretch_raw") < 0)
            & long_breakout
            & long_velocity
            & long_jerk
        )
        short_signal = (
            pl.lit(bool(params.get("allow_short", True)))
            & time_filter
            & stage_expr
            & gap_expr
            & volume_expr
            & (pl.col("_prev_max_stretch") >= stretch_threshold)
            & (pl.col("_stretch_raw") > 0)
            & short_breakout
            & short_velocity
            & short_jerk
        )

        return (
            context.with_columns(
                [
                    (long_signal | short_signal).fill_null(False).alias("signal"),
                    pl.when(long_signal)
                    .then(pl.lit("long"))
                    .when(short_signal)
                    .then(pl.lit("short"))
                    .otherwise(pl.lit(None))
                    .alias("signal_direction"),
                    pl.lit(entry_window_end.isoformat(timespec="minutes")).alias(
                        "playbook_entry_cutoff_et"
                    ),
                    pl.lit(stage_filter).alias("playbook_stage_filter"),
                    pl.lit(gap_state_filter).alias("playbook_gap_state_filter"),
                    pl.lit(str(params.get("stop_family", "reversal_extreme"))).alias(
                        "playbook_stop_family"
                    ),
                    pl.lit(str(params.get("exit_family", "fixed_1r"))).alias("playbook_exit_family"),
                ]
            )
            .drop(["_trade_date", "_bar_time", "_stretch_raw"])
        )

    def evaluate_entry(
        self,
        frame: pl.DataFrame,
        deployment_id: str,
        params: dict[str, Any],
    ) -> SignalDecision:
        enriched = self.generate_signals(frame, params)
        latest = enriched.tail(1).to_dicts()
        if not latest:
            raise ValueError("Cannot evaluate mean reversion on an empty frame")
        row = latest[0]
        direction = row.get("signal_direction")
        return SignalDecision(
            deployment_id=deployment_id,
            symbol=str(row.get("symbol") or params.get("symbol") or ""),
            timestamp=row["timestamp"],
            signal=bool(row.get("signal", False)),
            direction=SignalDirection(direction) if direction else None,
            reason=["intraday_mean_reversion_extremes"] if row.get("signal") else ["no_signal"],
            features={
                "playbook_stretch_value": row.get("playbook_stretch_value"),
                "playbook_prior_max_stretch": row.get("playbook_prior_max_stretch"),
                "playbook_prior_min_stretch": row.get("playbook_prior_min_stretch"),
                "playbook_reversal_high": row.get("playbook_reversal_high"),
                "playbook_reversal_low": row.get("playbook_reversal_low"),
            },
        )

    def evaluate_exit(
        self,
        frame: pl.DataFrame,
        deployment_id: str,
        params: dict[str, Any],
        position: TrackedPosition,
    ) -> ExitDecision:
        latest = frame.tail(1).to_dicts()
        if not latest:
            raise ValueError("Cannot evaluate mean reversion exit on an empty frame")
        row = latest[0]
        return ExitDecision(
            deployment_id=deployment_id,
            symbol=str(row.get("symbol") or params.get("symbol") or ""),
            timestamp=row["timestamp"],
            exit=False,
            action="hold",
            reason=["exit_management_not_promoted_to_runtime_adapter_yet"],
        )


def _stage_column(frame: pl.DataFrame, params: dict[str, Any]) -> str:
    candidates = _stage_candidates(str(params.get("stage_timeframe", "1m")))
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    return candidates[0]


def _alternate_stage_columns(frame: pl.DataFrame, params: dict[str, Any]) -> set[str]:
    candidates = set(_stage_candidates(str(params.get("stage_timeframe", "1m"))))
    return candidates if candidates & set(frame.columns) else set()


def _stage_column_available(frame: pl.DataFrame, params: dict[str, Any]) -> bool:
    return bool(set(_stage_candidates(str(params.get("stage_timeframe", "1m")))) & set(frame.columns))


def _stage_candidates(timeframe: str) -> tuple[str, ...]:
    if timeframe == "1m":
        return ("market_pulse_stage", "impulse_stage", "impulse_stage_1m")
    return (f"market_pulse_stage_{timeframe}", f"impulse_stage_{timeframe}")


def _market_stage_feature(timeframe: str) -> str:
    return "market_impulse" if timeframe == "1m" else f"market_impulse:{timeframe}"


def _stretch_raw_expr(stretch_source: str) -> pl.Expr:
    if stretch_source == "prior_rth_close_atr":
        return pl.col("atr_distance_from_prior_rth_close")
    if stretch_source == "vpoc_4h":
        reference = pl.col("vpoc_4h")
    else:
        reference = pl.col("opening_vwap_rth")
    return pl.when(reference > 0).then((pl.col("close") - reference) / reference).otherwise(None)


def _stretch_value_expr(stretch_source: str, z_score_window: int) -> pl.Expr:
    if stretch_source == "prior_rth_close_atr":
        return pl.col("_stretch_raw")
    min_samples = min(5, z_score_window)
    mean = pl.col("_stretch_raw").rolling_mean(
        window_size=z_score_window,
        min_samples=min_samples,
    ).over("_trade_date")
    std = pl.col("_stretch_raw").rolling_std(
        window_size=z_score_window,
        min_samples=min_samples,
    ).over("_trade_date")
    return pl.when(std > 0).then((pl.col("_stretch_raw") - mean) / std).otherwise(None)


def _reference_price_expr(stretch_source: str) -> pl.Expr:
    if stretch_source == "vpoc_4h":
        return pl.col("vpoc_4h")
    if stretch_source == "prior_rth_close_atr":
        return pl.col("prior_rth_close")
    return pl.col("opening_vwap_rth")


def _velocity_filter_expr(
    direction: str,
    velocity_filter: str,
    velocity_atr_threshold: float,
) -> pl.Expr:
    if velocity_filter == "no_filter":
        return pl.lit(True)
    if direction == "long":
        velocity = pl.col("_prev_min_velocity")
        aligned = velocity < 0
    else:
        velocity = pl.col("_prev_max_velocity")
        aligned = velocity > 0
    if velocity_filter == "aligned":
        return aligned.fill_null(False)

    normalized = (velocity.abs() / pl.col("daily_rth_atr_14")).fill_null(0.0)
    if velocity_filter == "climactic":
        return (aligned & (normalized >= velocity_atr_threshold)).fill_null(False)
    return (aligned & (normalized < velocity_atr_threshold)).fill_null(False)


def _positive_int(value: Any) -> int:
    normalized = int(value)
    if normalized <= 0:
        raise ValueError("period must be positive")
    return normalized

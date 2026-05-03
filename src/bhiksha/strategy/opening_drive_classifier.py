"""Opening Drive Classifier strategy plugin for the live runtime."""

from __future__ import annotations

from datetime import datetime, time, timedelta
import math
from typing import Any

import polars as pl

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.market_data.session import ET, ensure_utc, et_time_expr, et_timestamp_expr
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.base import coerce_time


def _time_plus_minutes(base: time, minutes: int) -> time:
    anchor = datetime(2000, 1, 1, base.hour, base.minute)
    shifted = anchor + timedelta(minutes=minutes)
    return shifted.time()


class OpeningDriveClassifierStrategy:
    """Evaluate opening-drive continuation and failure setups on the latest bar."""

    key = "opening_drive_classifier"

    def required_features(self, params: dict[str, Any]) -> set[str]:
        required = {"timestamp", "symbol", "open", "high", "low", "close"}
        if bool(params.get("use_volume_filter", True)):
            required.add("volume")
        if bool(params.get("use_directional_mass", True)):
            required.add("directional_mass")
        if bool(params.get("use_regime_filter", False)):
            regime_timeframe = str(params.get("regime_timeframe", "5m"))
            required.add(f"impulse_regime_{regime_timeframe}")
        return required

    def evaluate_entry(self, frame: pl.DataFrame, deployment_id: str, params: dict[str, Any]) -> SignalDecision:
        if frame.is_empty():
            raise ValueError("Cannot evaluate Opening Drive Classifier on an empty frame")

        required = self.required_features(params)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Opening Drive Classifier requires columns: {sorted(missing)}")

        market_open = coerce_time(params.get("market_open", "09:30"))
        opening_window_minutes = int(params.get("opening_window_minutes", 25))
        entry_start_offset_minutes = int(params.get("entry_start_offset_minutes", 25))
        entry_end_offset_minutes = int(params.get("entry_end_offset_minutes", 120))
        min_drive_return_pct = float(params.get("min_drive_return_pct", 0.0015))
        breakout_buffer_pct = float(params.get("breakout_buffer_pct", 0.0))
        use_volume_filter = bool(params.get("use_volume_filter", True))
        volume_multiplier = float(params.get("volume_multiplier", 1.2))
        use_directional_mass = bool(params.get("use_directional_mass", True))
        use_jerk_confirmation = bool(params.get("use_jerk_confirmation", True))
        use_regime_filter = bool(params.get("use_regime_filter", False))
        regime_timeframe = str(params.get("regime_timeframe", "5m"))
        regime_col = f"impulse_regime_{regime_timeframe}"
        allow_long = bool(params.get("allow_long", True))
        allow_short = bool(params.get("allow_short", True))
        enable_continue = bool(params.get("enable_continue", True))
        enable_fail = bool(params.get("enable_fail", True))
        kinematic_periods_back = max(int(params.get("kinematic_periods_back", 1)), 1)
        direction_filter = str(params.get("direction", "")).strip().lower() or None

        opening_end = _time_plus_minutes(market_open, opening_window_minutes)
        entry_start = _time_plus_minutes(market_open, entry_start_offset_minutes)
        entry_end = _time_plus_minutes(market_open, entry_end_offset_minutes)

        working = (
            frame.with_columns(
                [
                    et_timestamp_expr("timestamp").dt.date().alias("_trade_date"),
                    (pl.col("close") - pl.col("close").shift(kinematic_periods_back)).alias("_velocity"),
                ]
            )
            .with_columns(
                [
                    (pl.col("_velocity") - pl.col("_velocity").shift(kinematic_periods_back)).alias("_accel"),
                ]
            )
            .with_columns(
                [
                    (pl.col("_accel") - pl.col("_accel").shift(kinematic_periods_back)).alias("_jerk"),
                ]
            )
        )

        in_opening_window = (et_time_expr("timestamp") >= market_open) & (et_time_expr("timestamp") < opening_end)
        in_entry_window = (et_time_expr("timestamp") >= entry_start) & (et_time_expr("timestamp") <= entry_end)

        working = (
            working.with_columns(
                [
                    pl.col("open").filter(in_opening_window).first().over("_trade_date").alias("_opening_open"),
                    pl.col("close").filter(in_opening_window).last().over("_trade_date").alias("_opening_close"),
                    pl.col("high").filter(in_opening_window).max().over("_trade_date").alias("_opening_high"),
                    pl.col("low").filter(in_opening_window).min().over("_trade_date").alias("_opening_low"),
                    (
                        pl.col("volume").filter(in_opening_window).mean().over("_trade_date")
                        if use_volume_filter
                        else pl.lit(None)
                    ).alias("_opening_vol_mean"),
                ]
            )
            .with_columns(
                [
                    ((pl.col("_opening_close") - pl.col("_opening_open")) / pl.col("_opening_open")).alias("_opening_return"),
                    ((pl.col("_opening_high") + pl.col("_opening_low")) / 2.0).alias("_opening_mid"),
                ]
            )
            .with_columns(
                [
                    pl.when(pl.col("_opening_return") >= min_drive_return_pct)
                    .then(pl.lit("up"))
                    .when(pl.col("_opening_return") <= -min_drive_return_pct)
                    .then(pl.lit("down"))
                    .otherwise(pl.lit(None))
                    .alias("_drive_direction"),
                ]
            )
        )

        volume_gate = (
            pl.col("volume") > (volume_multiplier * pl.col("_opening_vol_mean"))
            if use_volume_filter
            else pl.lit(True)
        )
        long_mass_gate = pl.col("directional_mass") > 0 if use_directional_mass else pl.lit(True)
        short_mass_gate = pl.col("directional_mass") < 0 if use_directional_mass else pl.lit(True)
        long_jerk_gate = pl.col("_jerk") > 0 if use_jerk_confirmation else pl.lit(True)
        short_jerk_gate = pl.col("_jerk") < 0 if use_jerk_confirmation else pl.lit(True)
        bullish_regime_gate = pl.col(regime_col) == "bullish" if use_regime_filter else pl.lit(True)
        bearish_regime_gate = pl.col(regime_col) == "bearish" if use_regime_filter else pl.lit(True)

        continue_long = (
            in_entry_window
            & (pl.col("_drive_direction") == "up")
            & (pl.col("close") >= pl.col("_opening_high") * (1.0 + breakout_buffer_pct))
            & (pl.col("_accel") > 0)
            & long_jerk_gate
            & volume_gate
            & long_mass_gate
            & bullish_regime_gate
        )
        continue_short = (
            in_entry_window
            & (pl.col("_drive_direction") == "down")
            & (pl.col("close") <= pl.col("_opening_low") * (1.0 - breakout_buffer_pct))
            & (pl.col("_accel") < 0)
            & short_jerk_gate
            & volume_gate
            & short_mass_gate
            & bearish_regime_gate
        )
        fail_long = (
            in_entry_window
            & (pl.col("_drive_direction") == "down")
            & (pl.col("close") > pl.col("_opening_mid"))
            & (pl.col("_accel") > 0)
            & long_jerk_gate
            & volume_gate
            & long_mass_gate
            & bullish_regime_gate
        )
        fail_short = (
            in_entry_window
            & (pl.col("_drive_direction") == "up")
            & (pl.col("close") < pl.col("_opening_mid"))
            & (pl.col("_accel") < 0)
            & short_jerk_gate
            & volume_gate
            & short_mass_gate
            & bearish_regime_gate
        )

        long_raw = (
            (continue_long if enable_continue else pl.lit(False))
            | (fail_long if enable_fail else pl.lit(False))
        )
        short_raw = (
            (continue_short if enable_continue else pl.lit(False))
            | (fail_short if enable_fail else pl.lit(False))
        )
        if not allow_long:
            long_raw = pl.lit(False)
        if not allow_short:
            short_raw = pl.lit(False)

        working = (
            working.with_columns(
                [
                    in_entry_window.alias("_in_entry_window"),
                    volume_gate.alias("_volume_gate"),
                    long_mass_gate.alias("_long_mass_gate"),
                    short_mass_gate.alias("_short_mass_gate"),
                    long_jerk_gate.alias("_long_jerk_gate"),
                    short_jerk_gate.alias("_short_jerk_gate"),
                    long_raw.fill_null(False).alias("_long_raw"),
                    short_raw.fill_null(False).alias("_short_raw"),
                    continue_long.fill_null(False).alias("_continue_long"),
                    continue_short.fill_null(False).alias("_continue_short"),
                    fail_long.fill_null(False).alias("_fail_long"),
                    fail_short.fill_null(False).alias("_fail_short"),
                ]
            )
            .with_columns(
                [
                    (
                        pl.col("_long_raw")
                        & (pl.col("_long_raw").cast(pl.Int64).cum_sum().over("_trade_date") == 1)
                    ).alias("_long_signal"),
                    (
                        pl.col("_short_raw")
                        & (pl.col("_short_raw").cast(pl.Int64).cum_sum().over("_trade_date") == 1)
                    ).alias("_short_signal"),
                ]
            )
            .with_columns(
                [
                    pl.when(pl.col("_long_signal"))
                    .then(pl.lit("long"))
                    .when(pl.col("_short_signal"))
                    .then(pl.lit("short"))
                    .otherwise(pl.lit(None))
                    .alias("signal_direction"),
                    pl.when(pl.col("_long_signal") & pl.col("_continue_long"))
                    .then(pl.lit("continue"))
                    .when(pl.col("_short_signal") & pl.col("_continue_short"))
                    .then(pl.lit("continue"))
                    .when(pl.col("_long_signal") & pl.col("_fail_long"))
                    .then(pl.lit("fail"))
                    .when(pl.col("_short_signal") & pl.col("_fail_short"))
                    .then(pl.lit("fail"))
                    .otherwise(pl.lit(None))
                    .alias("opening_drive_mode"),
                    pl.col("_drive_direction").alias("opening_drive_direction"),
                ]
            )
        )

        latest = working.tail(1).to_dicts()[0]
        timestamp = latest["timestamp"]
        close = _as_float(latest.get("close"))
        accel = _as_float(latest.get("_accel"))
        jerk = _as_float(latest.get("_jerk"))
        suffix = "1m" if kinematic_periods_back == 1 else str(kinematic_periods_back)
        directional_mass = _as_float(latest.get("directional_mass"))
        volume = _as_float(latest.get("volume"))
        opening_volume_mean = _as_float(latest.get("_opening_vol_mean"))
        regime = latest.get(regime_col) if use_regime_filter else None

        reasons = _shared_reasons(
            latest=latest,
            use_volume_filter=use_volume_filter,
            use_directional_mass=use_directional_mass,
            use_jerk_confirmation=use_jerk_confirmation,
            use_regime_filter=use_regime_filter,
            regime=regime,
            allow_long=allow_long,
            allow_short=allow_short,
            enable_continue=enable_continue,
            enable_fail=enable_fail,
        )

        signal = False
        direction: SignalDirection | None = None
        if direction_filter == "long":
            if bool(latest.get("_long_signal", False)):
                signal = True
                direction = SignalDirection.LONG
                reasons.append(f"opening_drive_{latest.get('opening_drive_mode')}_long")
        elif direction_filter == "short":
            if bool(latest.get("_short_signal", False)):
                signal = True
                direction = SignalDirection.SHORT
                reasons.append(f"opening_drive_{latest.get('opening_drive_mode')}_short")
        else:
            if bool(latest.get("_long_signal", False)):
                signal = True
                direction = SignalDirection.LONG
                reasons.append(f"opening_drive_{latest.get('opening_drive_mode')}_long")
            elif bool(latest.get("_short_signal", False)):
                signal = True
                direction = SignalDirection.SHORT
                reasons.append(f"opening_drive_{latest.get('opening_drive_mode')}_short")
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
                f"accel_{suffix}": accel,
                f"jerk_{suffix}": jerk,
                "directional_mass": directional_mass,
                "volume": volume,
                "opening_volume_mean": opening_volume_mean,
                "opening_drive_direction": latest.get("opening_drive_direction"),
                "opening_drive_mode": latest.get("opening_drive_mode"),
                "opening_high": _as_float(latest.get("_opening_high")),
                "opening_low": _as_float(latest.get("_opening_low")),
                "opening_mid": _as_float(latest.get("_opening_mid")),
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
            raise ValueError("Cannot evaluate Opening Drive Classifier exit on an empty frame")

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


def _shared_reasons(
    *,
    latest: dict[str, Any],
    use_volume_filter: bool,
    use_directional_mass: bool,
    use_jerk_confirmation: bool,
    use_regime_filter: bool,
    regime: Any,
    allow_long: bool,
    allow_short: bool,
    enable_continue: bool,
    enable_fail: bool,
) -> list[str]:
    timestamp = latest["timestamp"]
    et_bar = ensure_utc(timestamp).astimezone(ET)
    reasons = [f"bar_time_et={et_bar.strftime('%H:%M')}"]

    if bool(latest.get("_in_entry_window")):
        reasons.append("time_window_ok")
    else:
        reasons.append("time_window_blocked")

    drive_direction = latest.get("opening_drive_direction")
    if drive_direction is None:
        reasons.append("drive_direction_unclassified")
    else:
        reasons.append(f"drive_{drive_direction}")

    accel = _as_float(latest.get("_accel"))
    if accel is None:
        reasons.append("accel_unavailable")
    elif accel > 0:
        reasons.append("accel_positive")
    elif accel < 0:
        reasons.append("accel_negative")
    else:
        reasons.append("accel_flat")

    if use_jerk_confirmation:
        jerk = _as_float(latest.get("_jerk"))
        if jerk is None:
            reasons.append("jerk_unavailable")
        elif jerk > 0:
            reasons.append("jerk_positive")
        elif jerk < 0:
            reasons.append("jerk_negative")
        else:
            reasons.append("jerk_flat")
    else:
        reasons.append("jerk_filter_disabled")

    if use_volume_filter:
        reasons.append("volume_gate_ok" if bool(latest.get("_volume_gate")) else "volume_gate_blocked")
    else:
        reasons.append("volume_filter_disabled")

    if use_directional_mass:
        long_mass_gate = bool(latest.get("_long_mass_gate"))
        short_mass_gate = bool(latest.get("_short_mass_gate"))
        if long_mass_gate and not short_mass_gate:
            reasons.append("directional_mass_positive")
        elif short_mass_gate and not long_mass_gate:
            reasons.append("directional_mass_negative")
        else:
            reasons.append("directional_mass_blocked")
    else:
        reasons.append("directional_mass_filter_disabled")

    if use_regime_filter:
        if regime == "bullish":
            reasons.append("regime_bullish")
        elif regime == "bearish":
            reasons.append("regime_bearish")
        else:
            reasons.append("regime_neutral")
    else:
        reasons.append("regime_filter_disabled")

    if not allow_long:
        reasons.append("longs_disabled")
    if not allow_short:
        reasons.append("shorts_disabled")
    if not enable_continue:
        reasons.append("continue_mode_disabled")
    if not enable_fail:
        reasons.append("fail_mode_disabled")
    return reasons


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if math.isnan(numeric):
        return None
    return numeric

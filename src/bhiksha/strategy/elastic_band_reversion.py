"""Elastic Band Reversion strategy plugin for the live runtime."""

from __future__ import annotations

from typing import Any

import math
import polars as pl

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.state.position_tracker import TrackedPosition


class ElasticBandReversionStrategy:
    """Evaluate the latest bar for VPOC stretch mean-reversion setups."""

    key = "elastic_band_reversion"

    def required_features(self, params: dict[str, Any]) -> set[str]:
        required = {"timestamp", "symbol", "close", "vpoc_4h"}
        if bool(params.get("use_directional_mass", True)):
            required.add("directional_mass")
        return required

    def evaluate_entry(self, frame: pl.DataFrame, deployment_id: str, params: dict[str, Any]) -> SignalDecision:
        if frame.is_empty():
            raise ValueError("Cannot evaluate Elastic Band Reversion on an empty frame")

        required = self.required_features(params)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Elastic Band Reversion requires columns: {sorted(missing)}")

        z_score_threshold = float(params.get("z_score_threshold", 2.0))
        z_score_window = max(int(params.get("z_score_window", 240)), 2)
        kinematic_periods_back = max(int(params.get("kinematic_periods_back", 1)), 1)
        use_directional_mass = bool(params.get("use_directional_mass", True))
        use_jerk_confirmation = bool(params.get("use_jerk_confirmation", True))
        direction_filter = str(params.get("direction", "")).strip().lower() or None

        working = (
            frame.with_columns(
                ((pl.col("close") - pl.col("vpoc_4h")) / pl.col("vpoc_4h")).alias("_dist_pct")
            )
            .with_columns(
                [
                    pl.col("_dist_pct").rolling_mean(window_size=z_score_window).alias("_dist_mean"),
                    pl.col("_dist_pct").rolling_std(window_size=z_score_window).alias("_dist_std"),
                    (pl.col("close") - pl.col("close").shift(kinematic_periods_back)).alias("_velocity"),
                ]
            )
            .with_columns(
                [
                    pl.when(pl.col("_dist_std").is_not_null() & (pl.col("_dist_std") > 0))
                    .then((pl.col("_dist_pct") - pl.col("_dist_mean")) / pl.col("_dist_std"))
                    .otherwise(pl.lit(None))
                    .alias("_z_score"),
                    (pl.col("_velocity") - pl.col("_velocity").shift(kinematic_periods_back)).alias("_accel"),
                ]
            )
            .with_columns(
                (pl.col("_accel") - pl.col("_accel").shift(kinematic_periods_back)).alias("_jerk")
            )
        )
        latest = working.tail(1).to_dicts()[0]

        timestamp = latest["timestamp"]
        close = _as_float(latest.get("close"))
        vpoc = _as_float(latest.get("vpoc_4h"))
        z_score = _as_float(latest.get("_z_score"))
        velocity = _as_float(latest.get("_velocity"))
        jerk = _as_float(latest.get("_jerk"))
        directional_mass = _as_float(latest.get("directional_mass"))

        long_jerk_gate = (jerk is not None and jerk > 0) if use_jerk_confirmation else True
        short_jerk_gate = (jerk is not None and jerk < 0) if use_jerk_confirmation else True
        long_dm_gate = (directional_mass is not None and directional_mass > 0) if use_directional_mass else True
        short_dm_gate = (directional_mass is not None and directional_mass < 0) if use_directional_mass else True

        long_candidate = (
            z_score is not None
            and z_score <= -z_score_threshold
            and velocity is not None
            and velocity < 0
            and long_jerk_gate
            and long_dm_gate
        )
        short_candidate = (
            z_score is not None
            and z_score >= z_score_threshold
            and velocity is not None
            and velocity > 0
            and short_jerk_gate
            and short_dm_gate
        )

        signal = False
        direction: SignalDirection | None = None
        reasons = _shared_reasons(
            z_score=z_score,
            z_score_threshold=z_score_threshold,
            velocity=velocity,
            jerk=jerk,
            use_jerk_confirmation=use_jerk_confirmation,
            directional_mass=directional_mass,
            use_directional_mass=use_directional_mass,
        )

        if direction_filter == "long":
            if long_candidate:
                signal = True
                direction = SignalDirection.LONG
                reasons.append("elastic_band_long")
        elif direction_filter == "short":
            if short_candidate:
                signal = True
                direction = SignalDirection.SHORT
                reasons.append("elastic_band_short")
        else:
            if long_candidate:
                signal = True
                direction = SignalDirection.LONG
                reasons.append("elastic_band_long")
            elif short_candidate:
                signal = True
                direction = SignalDirection.SHORT
                reasons.append("elastic_band_short")
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
                "z_score": z_score,
                "velocity": velocity,
                "jerk": jerk,
                "directional_mass": directional_mass,
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
            raise ValueError("Cannot evaluate Elastic Band Reversion exit on an empty frame")

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
    z_score: float | None,
    z_score_threshold: float,
    velocity: float | None,
    jerk: float | None,
    use_jerk_confirmation: bool,
    directional_mass: float | None,
    use_directional_mass: bool,
) -> list[str]:
    reasons = []
    if z_score is None:
        reasons.append("z_score_unavailable")
    elif abs(z_score) >= z_score_threshold:
        reasons.append("stretch_extreme_ok")
    else:
        reasons.append("stretch_extreme_blocked")

    if velocity is None:
        reasons.append("velocity_unavailable")
    elif velocity < 0:
        reasons.append("velocity_negative")
    elif velocity > 0:
        reasons.append("velocity_positive")
    else:
        reasons.append("velocity_flat")

    if use_jerk_confirmation:
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

    if use_directional_mass:
        if directional_mass is None or math.isnan(directional_mass):
            reasons.append("directional_mass_unavailable")
        elif directional_mass > 0:
            reasons.append("directional_mass_positive")
        elif directional_mass < 0:
            reasons.append("directional_mass_negative")
        else:
            reasons.append("directional_mass_flat")
    else:
        reasons.append("directional_mass_filter_disabled")
    return reasons


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(converted):
        return None
    return converted

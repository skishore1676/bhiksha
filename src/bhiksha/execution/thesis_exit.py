"""Evaluate underlying-anchored thesis exits from deployment payload metadata."""

from __future__ import annotations

from typing import Any

import polars as pl

from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import ExitDecision
from bhiksha.state.position_tracker import TrackedPosition


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

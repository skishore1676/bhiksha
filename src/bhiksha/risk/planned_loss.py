"""Shared planned-stop risk calculations for entry and recovery paths."""

from __future__ import annotations

from bhiksha.config.models import DeploymentManifest


def resolve_planned_stop_loss_pct(
    deployment: DeploymentManifest,
) -> tuple[float | None, str]:
    exit_spec = deployment.exit
    if exit_spec.stop_loss_pct is not None and exit_spec.stop_loss_pct > 0:
        return float(exit_spec.stop_loss_pct), "deployment_native"
    if deployment.risk.stop_loss_pct is not None and deployment.risk.stop_loss_pct > 0:
        return float(deployment.risk.stop_loss_pct), "global_fallback"
    if exit_spec.profile_exit_id:
        if exit_spec.initial_stop_pct is not None and exit_spec.initial_stop_pct > 0:
            return float(exit_spec.initial_stop_pct), "profile_initial_stop"
        if (
            exit_spec.premium_disaster_stop_pct is not None
            and exit_spec.premium_disaster_stop_pct > 0
        ):
            return float(exit_spec.premium_disaster_stop_pct), "profile_disaster_stop"
    return None, "unavailable"


def planned_stop_loss_usd(
    *,
    entry_price: float | None,
    quantity: int,
    stop_price: float | None = None,
    stop_loss_pct: float | None = None,
) -> float | None:
    if entry_price is None or entry_price <= 0 or quantity <= 0:
        return None
    if stop_price is not None and stop_price > 0:
        loss_per_contract = max(float(entry_price) - float(stop_price), 0.0)
    elif stop_loss_pct is not None and stop_loss_pct > 0:
        loss_per_contract = float(entry_price) * float(stop_loss_pct)
    else:
        return None
    return round(loss_per_contract * quantity * 100, 2)

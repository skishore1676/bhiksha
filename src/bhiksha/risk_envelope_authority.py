"""Canonical, non-self-referential identity for one bounded live canary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any


AUTHORIZATION_FINGERPRINT_SCHEMA = "risk-envelope-startup-authority.v1"


def risk_envelope_authorization_fingerprint(
    *,
    active_plan_id: str | None,
    deployments: Iterable[Any],
) -> str:
    """Hash stable canary authority/config inputs without volatile/self fields."""

    canaries: list[dict[str, Any]] = []
    for deployment in deployments:
        exit_spec = deployment.exit
        if (
            not deployment.enabled
            or exit_spec.risk_envelope_live_mode != "canary"
        ):
            continue
        execution = deployment.execution
        risk = deployment.risk
        canaries.append(
            {
                "deployment_id": deployment.deployment_id,
                "symbol": deployment.symbol,
                "candidate_id": exit_spec.risk_envelope_live_candidate_id,
                "candidate_overlay_hash": (
                    exit_spec.risk_envelope_live_candidate_overlay_hash
                ),
                "authorization_id": (
                    exit_spec.risk_envelope_live_authorization_id
                ),
                "start_at": _json_value(exit_spec.risk_envelope_live_start_at),
                "expires_at": _json_value(
                    exit_spec.risk_envelope_live_expires_at
                ),
                "authorized_deployment_id": (
                    exit_spec.risk_envelope_live_authorized_deployment_id
                ),
                "authorized_symbol": (
                    exit_spec.risk_envelope_live_authorized_symbol
                ),
                "authorized_active_plan_id": (
                    exit_spec.risk_envelope_live_authorized_active_plan_id
                ),
                "rollback_action": (
                    exit_spec.risk_envelope_live_rollback_action
                ),
                "max_premium_cap_fraction": (
                    exit_spec.risk_envelope_live_max_premium_cap_fraction
                ),
                "max_quote_age_ms": (
                    exit_spec.risk_envelope_live_max_quote_age_ms
                ),
                "max_spread_pct": (
                    exit_spec.risk_envelope_live_max_spread_pct
                ),
                "runtime_source_policy_hash": exit_spec.exit_policy_hash,
                "profile_exit_drives_live": (
                    exit_spec.profile_exit_drives_live
                ),
                "runtime_mode": execution.runtime_mode,
                "shadow_only": execution.shadow_only,
                "dte_min": execution.dte_min,
                "dte_max": execution.dte_max,
                "dte_fallback_policy": execution.dte_fallback_policy,
                "max_trade_premium_usd": risk.max_trade_premium_usd,
                "max_contracts": risk.max_contracts,
            }
        )
    payload = {
        "schema": AUTHORIZATION_FINGERPRINT_SCHEMA,
        "active_plan_id": active_plan_id,
        "canaries": sorted(
            canaries,
            key=lambda item: (item["deployment_id"], item["symbol"]),
        ),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _json_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value

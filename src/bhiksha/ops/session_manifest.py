"""Generated Exit Engine V2 session-policy receipts.

The active plan plus ``startup_config`` remain configuration authority. These
JSON/Markdown files are immutable review projections of that already-resolved
startup payload, never a second input to trading.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from bhiksha.config.models import DeploymentManifest
from bhiksha.execution.exit_policy import canonical_policy_hash


@dataclass(frozen=True, slots=True)
class SessionManifestPaths:
    json_path: Path
    markdown_path: Path


def effective_exit_policy_records(
    deployments: Iterable[DeploymentManifest],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for deployment in sorted(deployments, key=lambda item: item.deployment_id):
        exit_spec = deployment.exit
        snapshot = dict(exit_spec.exit_policy_snapshot)
        recorded_hash = exit_spec.exit_policy_hash
        computed_hash = canonical_policy_hash(snapshot) if snapshot else None
        hash_verified = bool(
            snapshot and recorded_hash and computed_hash == recorded_hash
        )
        records.append(
            {
                "deployment_id": deployment.deployment_id,
                "symbol": deployment.symbol,
                "strategy_id": deployment.strategy.key,
                "runtime_mode": getattr(
                    deployment.execution.runtime_mode,
                    "value",
                    deployment.execution.runtime_mode,
                ),
                "shadow_only": deployment.execution.shadow_only,
                "selected_dte_window": {
                    "min": deployment.execution.dte_min,
                    "max": deployment.execution.dte_max,
                    "fallback_policy": deployment.execution.dte_fallback_policy,
                },
                "max_contracts": deployment.risk.max_contracts,
                "risk_envelope_live": {
                    "mode": exit_spec.risk_envelope_live_mode,
                    "candidate_id": exit_spec.risk_envelope_live_candidate_id,
                    "candidate_overlay_hash": (
                        exit_spec.risk_envelope_live_candidate_overlay_hash
                    ),
                    "authorization_id": (
                        exit_spec.risk_envelope_live_authorization_id
                    ),
                    "start_at": (
                        exit_spec.risk_envelope_live_start_at.isoformat()
                        if exit_spec.risk_envelope_live_start_at
                        else None
                    ),
                    "expires_at": (
                        exit_spec.risk_envelope_live_expires_at.isoformat()
                        if exit_spec.risk_envelope_live_expires_at
                        else None
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
                },
                "profile_label": exit_spec.profile_exit_id,
                "policy_schema_version": exit_spec.exit_policy_schema_version,
                "policy_id": exit_spec.exit_policy_id,
                "policy_hash": recorded_hash,
                "policy_hash_verified": hash_verified,
                "resolution_status": (
                    exit_spec.exit_policy_provenance.get("resolution")
                    if exit_spec.exit_policy_provenance
                    else (
                        "not_profile_managed"
                        if not exit_spec.profile_exit_id
                        else "missing_authoritative_policy"
                    )
                ),
                "policy": snapshot,
                "provenance": dict(exit_spec.exit_policy_provenance),
                "source": deployment.source.model_dump(mode="json"),
            }
        )
    return records


def build_session_manifest(
    startup_snapshot: dict[str, Any],
    *,
    rollback_latches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selection = startup_snapshot.get("deployment_selection") or {}
    active_plan = startup_snapshot.get("active_plan") or {}
    active_plan_id = (
        selection.get("active_plan_id")
        or active_plan.get("active_plan_id")
        or "unknown_active_plan"
    )
    fingerprint = startup_snapshot.get("config_fingerprint")
    startup_authority_fingerprint = startup_snapshot.get(
        "risk_envelope_authorization_fingerprint"
    )
    effective_policies = startup_snapshot.get("effective_exit_policies", [])
    latches_by_deployment = {
        str(item.get("deployment_id")): dict(item)
        for item in (rollback_latches or [])
        if str(item.get("deployment_id") or "").strip()
    }
    canaries = []
    now = datetime.now(UTC)
    for item in effective_policies:
        live = item.get("risk_envelope_live") or {}
        if live.get("mode") != "canary":
            continue
        start = _aware_datetime(live.get("start_at"))
        expires = _aware_datetime(live.get("expires_at"))
        rollback_latch = latches_by_deployment.get(
            str(item.get("deployment_id"))
        )
        state = (
            "disarmed_rollback_latched"
            if rollback_latch is not None
            else "safety_blocked_invalid_authorization_window"
            if start is None or expires is None or start >= expires
            else "disarmed_authorization_not_yet_valid"
            if now < start
            else "disarmed_authorization_expired"
            if now >= expires
            else "armed"
        )
        canaries.append(
            {
                "deployment_id": item.get("deployment_id"),
                **dict(live),
                "startup_authorization_fingerprint": (
                    startup_authority_fingerprint
                ),
                "rollback_latch": rollback_latch,
                "state": state,
            }
        )
    return {
        "contract_name": "exit_engine_session_manifest",
        "schema_version": 2,
        "artifact_role": "generated_receipt_only",
        "configuration_authority": ["active_plan", "startup_config"],
        "session_manifest_id": f"{active_plan_id}:{fingerprint or 'unfingerprinted'}",
        "session_id": f"{active_plan_id}:{fingerprint or 'unfingerprinted'}",
        "active_plan_id": active_plan_id,
        "plan_revision_id": active_plan.get("plan_revision_id"),
        "trading_date": active_plan.get("trading_date"),
        "config_fingerprint": fingerprint,
        "risk_envelope_authorization_fingerprint": (
            startup_authority_fingerprint
        ),
        "code_version": startup_snapshot.get("code_version"),
        "source": active_plan.get("source") or {},
        "rejected_or_suppressed_inputs": active_plan.get("suppressed") or [],
        "effective_exit_policies": effective_policies,
        "risk_envelope_canaries": canaries,
        "risk_envelope_rollback_latches": [
            latches_by_deployment[key]
            for key in sorted(latches_by_deployment)
        ],
    }


def render_session_manifest_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# Exit Engine Session Manifest",
        "",
        f"- Session manifest: `{manifest['session_manifest_id']}`",
        f"- Plan revision: `{manifest.get('plan_revision_id')}`",
        f"- Active plan: `{manifest['active_plan_id']}`",
        f"- Config fingerprint: `{manifest.get('config_fingerprint')}`",
        "- Authority: `active_plan` + `startup_config` (this file is a receipt)",
        "",
        "| Deployment | Symbol | Policy | Hash | Resolution | Verified |",
        "|---|---|---|---|---|---|",
    ]
    for item in manifest.get("effective_exit_policies", []):
        policy_hash = item.get("policy_hash") or "-"
        lines.append(
            "| {deployment_id} | {symbol} | {policy_id} | `{policy_hash}` | "
            "{resolution_status} | {verified} |".format(
                deployment_id=item.get("deployment_id"),
                symbol=item.get("symbol"),
                policy_id=item.get("policy_id") or item.get("profile_label") or "-",
                policy_hash=policy_hash,
                resolution_status=item.get("resolution_status"),
                verified="yes" if item.get("policy_hash_verified") else "no",
            )
        )
    canaries = manifest.get("risk_envelope_canaries") or []
    if canaries:
        lines.extend(
            [
                "",
                "## Dynamic Risk Envelope canary",
                "",
                "| Deployment | State | Rollback reason | Latched at |",
                "|---|---|---|---|",
            ]
        )
        for item in canaries:
            latch = item.get("rollback_latch") or {}
            lines.append(
                f"| `{item.get('deployment_id')}` | "
                f"`{item.get('state')}` | "
                f"{latch.get('reason') or '-'} | "
                f"`{latch.get('latched_at') or '-'}` |"
            )
    suppressed = manifest.get("rejected_or_suppressed_inputs", [])
    lines.extend(
        [
            "",
            f"Rejected or suppressed inputs: **{len(suppressed)}**",
            "",
        ]
    )
    return "\n".join(lines)


def write_session_manifest(
    startup_snapshot: dict[str, Any],
    *,
    output_dir: str | Path,
    rollback_latches: list[dict[str, Any]] | None = None,
) -> SessionManifestPaths:
    manifest = build_session_manifest(
        startup_snapshot,
        rollback_latches=rollback_latches,
    )
    trading_date = manifest.get("trading_date") or "unknown-date"
    fingerprint = manifest.get("config_fingerprint") or "unfingerprinted"
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    stem = f"session_manifest_{trading_date}_{fingerprint}"
    json_path = root / f"{stem}.json"
    markdown_path = root / f"{stem}.md"
    _atomic_write(
        json_path,
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    _atomic_write(markdown_path, render_session_manifest_markdown(manifest))
    return SessionManifestPaths(json_path=json_path, markdown_path=markdown_path)


def _atomic_write(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None

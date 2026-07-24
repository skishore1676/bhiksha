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
) -> dict[str, Any]:
    selection = startup_snapshot.get("deployment_selection") or {}
    active_plan = startup_snapshot.get("active_plan") or {}
    active_plan_id = (
        selection.get("active_plan_id")
        or active_plan.get("active_plan_id")
        or "unknown_active_plan"
    )
    fingerprint = startup_snapshot.get("config_fingerprint")
    return {
        "contract_name": "exit_engine_session_manifest",
        "schema_version": 1,
        "artifact_role": "generated_receipt_only",
        "configuration_authority": ["active_plan", "startup_config"],
        "session_manifest_id": f"{active_plan_id}:{fingerprint or 'unfingerprinted'}",
        "active_plan_id": active_plan_id,
        "trading_date": active_plan.get("trading_date"),
        "config_fingerprint": fingerprint,
        "code_version": startup_snapshot.get("code_version"),
        "source": active_plan.get("source") or {},
        "rejected_or_suppressed_inputs": active_plan.get("suppressed") or [],
        "effective_exit_policies": startup_snapshot.get(
            "effective_exit_policies", []
        ),
    }


def render_session_manifest_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# Exit Engine Session Manifest",
        "",
        f"- Session manifest: `{manifest['session_manifest_id']}`",
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
) -> SessionManifestPaths:
    manifest = build_session_manifest(startup_snapshot)
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

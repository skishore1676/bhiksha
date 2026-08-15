"""One semantic owner-status projection for the Cartographer shadow lane."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def owner_status(
    producer: Mapping[str, Any],
    *,
    projection: Mapping[str, Any] | None = None,
    compile_readback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project owner facts; launchd exit success alone is never healthy evidence."""

    producer_status = str(producer.get("status") or "unknown")
    lifecycle = str(producer.get("lifecycle") or "blocked")
    attention = bool(producer.get("attention_required"))
    reason = str(producer.get("reason") or "owner_status_missing")
    if attention or lifecycle == "blocked":
        return _status("blocked", producer_status, True, reason, producer, projection, compile_readback)
    if lifecycle == "running":
        return _status("recovering", producer_status, False, reason, producer, projection, compile_readback)
    if projection is not None and projection.get("status") not in {"dry_run", "no_signal", "succeeded"}:
        return _status("blocked", "projection_failed", True, "projection_receipt_failed", producer, projection, compile_readback)
    if compile_readback is not None and compile_readback.get("status") not in {"succeeded", "no_plan"}:
        return _status("blocked", "compile_or_readback_failed", True, "compile_readback_failed", producer, projection, compile_readback)
    quiet = producer_status == "no_plan"
    return _status(
        "complete",
        "no_signal" if quiet else "succeeded",
        False,
        "quiet_completion" if quiet else "fresh_owner_evidence",
        producer,
        projection,
        compile_readback,
    )


def _status(
    lifecycle: str,
    status: str,
    attention_required: bool,
    reason: str,
    producer: Mapping[str, Any],
    projection: Mapping[str, Any] | None,
    compile_readback: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema": "bhiksha.cartographer_owner_status.v1",
        "unit_id": "com.bhiksha.cartographer-shadow",
        "kind": "external_launchd_job",
        "lifecycle": lifecycle,
        "last_run_status": status,
        "attention_required": attention_required,
        "findings": [] if not attention_required else [reason],
        "last": {
            "domain": {
                "ok": not attention_required,
                "status": status,
                "attention_required": attention_required,
                "reason": reason,
            },
            "producer": dict(producer),
            "projection": dict(projection or {}),
            "compile_readback": dict(compile_readback or {}),
        },
        "available_actions": [],
    }


__all__ = ["owner_status"]

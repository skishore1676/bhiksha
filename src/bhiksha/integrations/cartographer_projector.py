"""Pure A:V projector for Cartographer signals; Google I/O stays outside this module."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.cartographer_profiles import canonical_hash, profile_bundle, validate_profile_bundle


MANUAL_ENTRY_HEADERS = [
    "id", "enabled", "mode", "strategy", "symbol", "direction", "trigger", "trigger_when",
    "after", "start", "end_in_days", "notes", "bhiksha_status", "bhiksha_last_event_at",
    "bhiksha_last_note", "bhiksha_last_trade_id", "idea_invalidation", "management_policy",
    "management_policy_spec", "metadata", "execution", "risk",
]
OWNER = "market_cartographer"


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_json(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return dict(loaded) if isinstance(loaded, dict) else None


def _validate_signal(signal: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in signal.items() if key not in {"signal_id", "signal_hash"}}
    expected = canonical_hash(body)
    if signal.get("signal_hash") != expected or signal.get("signal_id") != f"mc-v1-{expected.split(':', 1)[1][:24]}":
        raise ValueError("Cartographer signal identity is not content-bound")
    if signal.get("authorization_mode") != "shadow":
        raise ValueError("Cartographer projector accepts shadow-only signals")
    if signal.get("management_policy") != "TREND_CONTINUATION":
        raise ValueError("Cartographer signal has an unknown management profile")
    return dict(signal)


def _projected_row(signal: Mapping[str, Any], *, operator_premium_ceiling: float) -> list[Any]:
    signal = _validate_signal(signal)
    bundle = validate_profile_bundle(profile_bundle(str(signal["management_policy"])))
    execution = bundle["execution"]
    management = bundle["management"]
    requested = float(bundle["requested_max_trade_premium_usd"])
    if operator_premium_ceiling <= 0:
        raise ValueError("operator premium ceiling must be positive")
    effective = min(requested, operator_premium_ceiling)
    evidence = dict(signal["evidence"])
    metadata = {
        "source_owner": OWNER,
        "signal_id": signal["signal_id"],
        "signal_hash": signal["signal_hash"],
        "cartographer_version": signal["cartographer_version"],
        "run_id": signal["run_id"],
        "trading_date": signal["trading_date"],
        "valid_through": signal["valid_through"],
        "profile_slug": signal["management_policy"],
        "bundle_hash": bundle["bundle_hash"],
        "invalidation_price": signal["invalidation_price"],
        "evidence": evidence,
    }
    risk = {
        "requested_max_trade_premium_usd": requested,
        "operator_max_trade_premium_usd": operator_premium_ceiling,
        "effective_max_trade_premium_usd": effective,
    }
    rationale = "; ".join(str(item["text"]) for item in signal["rationale"])
    valid_after_et = datetime.fromisoformat(str(signal["valid_after"]).replace("Z", "+00:00")).astimezone(
        ZoneInfo("America/New_York")
    ).strftime("%H:%M")
    return [
        signal["signal_id"], True, "shadow", "manual_trigger", signal["symbol"], signal["direction"],
        signal["trigger_price"], signal["trigger_direction"], valid_after_et, signal["trading_date"],
        execution["dte_max"], rationale, "", "", "", "", signal["invalidation_price"],
        signal["management_policy"], _json(management), _json(metadata), _json(execution), _json(risk),
    ]


def project_signals(
    existing_rows: Sequence[Sequence[Any]],
    signal_batch: Mapping[str, Any],
    *,
    operator_premium_ceiling: float,
    trading_date: str,
) -> tuple[list[list[Any]], dict[str, Any]]:
    """Return an idempotent fake-workbook update and a zero-write receipt."""

    if signal_batch.get("schema") != "market_cartographer.signal_batch.v1":
        raise ValueError("unsupported Cartographer signal batch")
    body = {key: value for key, value in signal_batch.items() if key != "signal_batch_hash"}
    if signal_batch.get("signal_batch_hash") != canonical_hash(body):
        raise ValueError("Cartographer signal batch hash mismatch")
    output = [list(row) + [""] * max(0, 22 - len(row)) for row in existing_rows]
    by_id = {str(row[0]): index for index, row in enumerate(output) if row and row[0]}
    actions: list[dict[str, Any]] = []
    for signal in signal_batch.get("signals", []):
        normalized = _validate_signal(signal)
        target = _projected_row(normalized, operator_premium_ceiling=operator_premium_ceiling)
        row_id = str(target[0])
        if str(normalized["trading_date"]) != trading_date:
            actions.append({"signal_id": row_id, "action": "expired"})
            continue
        index = by_id.get(row_id)
        if index is None:
            output.append(target)
            by_id[row_id] = len(output) - 1
            actions.append({"signal_id": row_id, "action": "created"})
            continue
        existing = output[index]
        owner = _load_json(existing[19])
        if owner is None or owner.get("source_owner") != OWNER:
            raise ValueError("refusing to overwrite a non-Cartographer row with the same ID")
        if owner.get("signal_hash") != normalized["signal_hash"]:
            raise ValueError("Cartographer signal ID collision has mismatched identity")
        # Bhiksha owns its writebacks and consumed state; the human owns an explicit
        # mode flip.  A retry cannot re-arm or alter either.
        preserved = list(target)
        preserved[1] = existing[1]
        preserved[2] = existing[2]
        preserved[12:16] = existing[12:16]
        output[index] = preserved
        actions.append({"signal_id": row_id, "action": "preserved"})
    receipt_body = {
        "schema": "bhiksha.cartographer_projection_receipt.v1",
        "status": "no_signal" if signal_batch.get("status") == "no_signal" else "dry_run",
        "signal_batch_hash": signal_batch["signal_batch_hash"],
        "row_count": len(output),
        "actions": actions,
        "effects": {"broker": False, "orders": False, "auth": False, "sheet": False, "active_plan": False, "external_send": False},
    }
    return output, {**receipt_body, "receipt_hash": canonical_hash(receipt_body)}


def row_to_compiler_payload(row: Sequence[Any]) -> dict[str, Any]:
    """Map one A:V row to the existing Bhiksha manual-row normalizer shape."""

    if len(row) < 22:
        raise ValueError("Cartographer manual-entry row must be A:V")
    return {
        "id": row[0], "enabled": row[1], "mode": row[2], "type": "manual", "strategy": row[3],
        "symbol": row[4], "direction": row[5], "trigger": row[6], "trigger_when": row[7],
        "after": row[8], "start": row[9], "end_in_days": row[10], "notes": row[11],
        "idea_invalidation": row[16], "management_policy": row[17], "management_policy_spec": row[18],
        "metadata": row[19], "execution": row[20], "risk": row[21],
    }


__all__ = ["MANUAL_ENTRY_HEADERS", "project_signals", "row_to_compiler_payload"]

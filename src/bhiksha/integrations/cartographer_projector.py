"""Pure A:V projector for Cartographer signals; Google I/O stays outside this module."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from bhiksha.cartographer_profiles import canonical_hash, profile_bundle, validate_profile_bundle


MANUAL_ENTRY_HEADERS = [
    "id", "enabled", "mode", "strategy", "symbol", "direction", "trigger", "trigger_when",
    "after", "start", "end_in_days", "notes", "bhiksha_status", "bhiksha_last_event_at",
    "bhiksha_last_note", "bhiksha_last_trade_id", "idea_invalidation", "management_policy",
    "management_policy_spec", "metadata", "execution", "risk",
]
OWNER = "market_cartographer"
_SHEETS_DATE_EPOCH = date(1899, 12, 30)


class TableClient(Protocol):
    def read_headers(self) -> list[str]: ...
    def read_rows(self) -> list[dict[str, Any]]: ...
    def update_exact_rows(self, *, headers: list[str], rows: list[tuple[int, list[Any]]]) -> None: ...


class ProjectionApplyError(RuntimeError):
    """An apply may have changed owned Sheet cells but could not be confirmed."""

    def __init__(self, message: str, *, preimage: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.preimage = preimage


def _sheet_equivalent_value(index: int, value: Any) -> Any:
    """Normalize the date/time coercions produced by USER_ENTERED Sheet writes."""

    if index == 8 and isinstance(value, (int, float)) and not isinstance(value, bool):
        minutes = round(float(value) * 24 * 60) % (24 * 60)
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
    if index == 9 and isinstance(value, (int, float)) and not isinstance(value, bool):
        return (_SHEETS_DATE_EPOCH + timedelta(days=int(value))).isoformat()
    return value


def _sheet_rows_equal(left: Sequence[Any], right: Sequence[Any]) -> bool:
    return len(left) == len(right) and all(
        _sheet_equivalent_value(index, left_value)
        == _sheet_equivalent_value(index, right_value)
        for index, (left_value, right_value) in enumerate(zip(left, right, strict=True))
    )


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


def project_with_table(
    table: TableClient,
    signal_batch: Mapping[str, Any],
    *,
    operator_premium_ceiling: float,
    trading_date: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Plan, optionally apply, and verify an owned A:V projection.

    This is deliberately the only impure seam.  The default is dry-run and an
    apply is accepted only after an exact header/preimage read and followed by
    a readback that must reproduce every Cartographer-owned target row.
    """

    headers = table.read_headers()
    if headers != MANUAL_ENTRY_HEADERS:
        raise ValueError("manual_entry headers must exactly match the A:V projection contract")
    before = table.read_rows()
    ids: set[str] = set()
    existing_rows: list[list[Any]] = []
    indexes: dict[str, int] = {}
    for record in before:
        row_id = str(record.get("id") or "")
        if row_id and row_id in ids:
            raise ValueError("manual_entry has duplicate IDs; refusing ambiguous projection")
        if row_id:
            ids.add(row_id)
            indexes[row_id] = int(record["row_index"])
        existing_rows.append([record.get(header, "") for header in headers])
    projected, pure_receipt = project_signals(
        existing_rows, signal_batch,
        operator_premium_ceiling=operator_premium_ceiling, trading_date=trading_date,
    )
    batch_ids = {str(signal.get("signal_id") or "") for signal in signal_batch.get("signals", [])}
    expired_rows: list[str] = []
    for index, row in enumerate(projected[: len(existing_rows)]):
        owner = _load_json(row[19])
        if (
            owner is not None
            and owner.get("source_owner") == OWNER
            and str(owner.get("trading_date") or "") < trading_date
            and str(row[0]) not in batch_ids
            and row[1] is not False
        ):
            row[1] = False
            expired_rows.append(str(row[0]))
    updates: list[tuple[int, list[Any]]] = []
    preimage: list[dict[str, Any]] = []
    next_index = max((int(record["row_index"]) for record in before), default=1) + 1
    for row in projected:
        row_id = str(row[0])
        # Formatted legacy Sheet rows can be returned as empty records. They are
        # neither owned inputs nor append targets; only a non-empty signal ID
        # can authorize a projector write.
        if not row_id:
            continue
        index = indexes.get(row_id)
        if index is None:
            updates.append((next_index, row))
            preimage.append({"row_index": next_index, "before": None, "id": row_id})
            next_index += 1
            continue
        current = next(record for record in before if int(record["row_index"]) == index)
        values = [current.get(header, "") for header in headers]
        if not _sheet_rows_equal(values, row):
            updates.append((index, row))
            preimage.append({"row_index": index, "before": values, "id": row_id})
    receipt_body: dict[str, Any] = {
        **{key: value for key, value in pure_receipt.items() if key != "receipt_hash"},
        "status": "applied" if apply else pure_receipt["status"],
        "producer_run_id": signal_batch.get("run_id"),
        "trading_date": trading_date,
        "apply_requested": apply,
        "header_contract": "A:V_exact",
        "planned_updates": len(updates),
        "preimage": preimage,
        "expired_rows": expired_rows,
        "effects": {**pure_receipt["effects"], "sheet": apply},
    }
    if not apply:
        return {**receipt_body, "receipt_hash": canonical_hash(receipt_body)}
    try:
        table.update_exact_rows(headers=headers, rows=updates)
        after = table.read_rows()
        after_by_id = {str(record.get("id") or ""): record for record in after}
        for _, expected in updates:
            actual = after_by_id.get(str(expected[0]))
            if actual is None or not _sheet_rows_equal(
                [actual.get(header, "") for header in headers], expected
            ):
                raise RuntimeError("Cartographer projection readback mismatch")
    except Exception as exc:
        raise ProjectionApplyError(
            f"{exc}; retain preimage for rollback", preimage=preimage
        ) from exc
    postimage = [
        {"row_index": index, "id": str(values[0])}
        for index, values in updates
    ]
    receipt_body["postimage"] = postimage
    return {**receipt_body, "receipt_hash": canonical_hash(receipt_body)}


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


__all__ = [
    "MANUAL_ENTRY_HEADERS",
    "ProjectionApplyError",
    "project_signals",
    "project_with_table",
    "row_to_compiler_payload",
]

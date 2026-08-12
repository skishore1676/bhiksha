"""Exact, idempotent Google Sheet projection for chart-scenario shadow rows."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mala_bhiksha_kernel import canonical_sha256

from bhiksha.chart_scenarios.paths import require_experiment_path
from bhiksha.chart_scenarios.validation import ShadowPlan, validate_bundle
from bhiksha.integrations.google_sheets import GoogleSheetTableClient

REQUEST_SCHEMA = "tradelab.market_context_sheet_upsert_request.v1"
RECEIPT_SCHEMA = "tradelab.market_context_sheet_projection_receipt.v1"
SHEET_NAME = "Chart_Scenarios_v1"
SPREADSHEET_ID = "1cJPWfkQB6pp91TAFNT86R5Pi1cUfzCgT3bUWgjY6rbc"
ALLOWED_ARMS = frozenset({"chart_deterministic", "chart_agentic_rerank"})
KEY_COLUMNS = ("campaign_id", "run_id", "arm", "scenario_id")
HEADERS = (
    "trading_date",
    "arm",
    "rank",
    "symbol",
    "direction",
    "thesis",
    "entry_summary",
    "invalidation_summary",
    "exit_profile",
    "shadow_status",
    "net_r",
    "program_id",
    "experiment_family_id",
    "experiment_version",
    "campaign_id",
    "run_id",
    "scenario_id",
    "candidate_id",
    "input_cutoff",
    "scenario_expires_at",
    "triggered_at",
    "terminal_at",
    "component_manifest_hash",
    "candidate_pool_hash",
    "scenario_hash",
    "exit_policy_hash",
    "chart_evidence_hash",
    "context_packet_hash",
    "quote_status",
    "option_contract",
    "event_count",
    "terminal_reason",
    "last_receipt_at",
    "authorization_mode",
    "source_type",
    "comparable",
    "quarantine_reason",
)


def project_sheet_upsert_request(
    request: Mapping[str, Any],
    *,
    client: GoogleSheetTableClient,
    receipt_path: str | Path,
    plan: ShadowPlan,
) -> dict[str, Any]:
    """Upsert only exact experiment keys, then authenticate an exact reread."""

    sealed = validate_sheet_upsert_request(request, plan=plan)
    if client.spreadsheet_id != sealed["spreadsheet_id"]:
        raise ValueError("Google Sheet client spreadsheet differs from request")
    if client.sheet_name != SHEET_NAME:
        raise ValueError("Google Sheet client did not resolve the exact experiment tab")
    if tuple(client.read_headers()) != HEADERS:
        raise ValueError("Chart_Scenarios_v1 physical headers differ from contract")

    existing_rows = client.read_rows()
    keyed, empty_slots = _index_existing_rows(existing_rows)
    requested = [
        dict(zip(HEADERS, values, strict=True)) for values in sealed["values"][1:]
    ]
    updates: list[tuple[int, list[Any]]] = []
    inserted = 0
    updated = 0
    keys: list[list[str]] = []
    for row in requested:
        key = _row_key(row)
        keys.append(list(key))
        row_index = keyed.get(key)
        if row_index is None:
            if not empty_slots:
                raise ValueError(
                    "Chart_Scenarios_v1 has no preformatted empty keyed row; "
                    "refusing to append outside preserved validations"
                )
            row_index = empty_slots.pop(0)
            inserted += 1
        else:
            updated += 1
        updates.append((row_index, [row[header] for header in HEADERS]))
    client.update_exact_rows(headers=list(HEADERS), rows=updates)

    reread_rows = client.read_rows()
    reread_index, _ = _index_existing_rows(reread_rows)
    exact_rows: list[list[Any]] = []
    for expected in requested:
        key = _row_key(expected)
        row_index = reread_index.get(key)
        if row_index is None:
            raise ValueError(f"Sheet exact reread omitted projection key: {key}")
        actual = next(row for row in reread_rows if int(row["row_index"]) == row_index)
        expected_values = [_normalize_cell(expected[header]) for header in HEADERS]
        actual_values = [_normalize_cell(actual.get(header, "")) for header in HEADERS]
        if actual_values != expected_values:
            raise ValueError(f"Sheet exact reread mismatch for projection key: {key}")
        exact_rows.append(actual_values)
    body = {
        "schema": RECEIPT_SCHEMA,
        "status": "succeeded",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "request_hash": sealed["content_hash"],
        "spreadsheet_id": sealed["spreadsheet_id"],
        "sheet_name": SHEET_NAME,
        "header_hash": sealed["header_hash"],
        "row_count": len(exact_rows),
        "inserted_count": inserted,
        "updated_count": updated,
        "keys": keys,
        "reread_values_hash": canonical_sha256([list(HEADERS), *exact_rows]),
        "exact_reread": True,
        "effects": {
            "broker": False,
            "orders": False,
            "authorization": False,
            "sheet_tab": SHEET_NAME,
        },
    }
    receipt = {**body, "content_hash": canonical_sha256(body)}
    _write_atomic(Path(receipt_path), receipt)
    return receipt


def validate_sheet_upsert_request(
    value: Mapping[str, Any], *, plan: ShadowPlan
) -> dict[str, Any]:
    expected = {
        "schema",
        "spreadsheet_id",
        "sheet_name",
        "key_columns",
        "header_hash",
        "values",
        "expected_reread",
        "effects",
        "content_hash",
    }
    if set(value) != expected:
        raise ValueError("Sheet upsert request must declare exact top-level fields")
    if (
        value.get("schema") != REQUEST_SCHEMA
        or value.get("sheet_name") != SHEET_NAME
        or value.get("spreadsheet_id") != SPREADSHEET_ID
    ):
        raise ValueError("unsupported Sheet upsert request schema or tab")
    if value.get("key_columns") != list(KEY_COLUMNS):
        raise ValueError("Sheet upsert request key columns differ from contract")
    if str(value.get("header_hash", "")).removeprefix("sha256:") != canonical_sha256(
        list(HEADERS)
    ):
        raise ValueError("Sheet upsert request header hash mismatch")
    values = value.get("values")
    if (
        not isinstance(values, list)
        or not values
        or values[0] != list(HEADERS)
        or any(not isinstance(row, list) or len(row) != len(HEADERS) for row in values)
    ):
        raise ValueError("Sheet upsert request values do not match exact 37 columns")
    rows = [dict(zip(HEADERS, row, strict=True)) for row in values[1:]]
    for row in rows:
        if (
            row["arm"] not in ALLOWED_ARMS
            or row["authorization_mode"] != "shadow"
            or row["source_type"] != "chart_scenario_experiment"
        ):
            raise ValueError("Sheet projection row is not an allowed shadow scenario")
    _validate_rows_against_plan(rows, plan=plan)
    keys = [_row_key(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Sheet upsert request contains duplicate keys")
    reread = value.get("expected_reread")
    if reread != {
        "header": f"{SHEET_NAME}!A1:AK1",
        "row_count": len(rows),
        "receipt_schema": RECEIPT_SCHEMA,
        "require_exact_values": True,
    }:
        raise ValueError("Sheet upsert request reread contract mismatch")
    if value.get("effects") != {
        "broker": False,
        "orders": False,
        "authorization": False,
    }:
        raise ValueError("Sheet upsert request has prohibited effects")
    computed = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    if str(value.get("content_hash", "")).removeprefix("sha256:") != computed:
        raise ValueError("Sheet upsert request content hash mismatch")
    return {**dict(value), "content_hash": computed}


def _validate_rows_against_plan(
    rows: list[dict[str, Any]], *, plan: ShadowPlan
) -> None:
    sealed = validate_bundle(plan.model_dump(mode="json"))
    scenarios = {
        (scenario.arm_id.value, scenario.scenario_id): scenario
        for scenario in sealed.scenarios
    }
    for row in rows:
        scenario = scenarios.get((str(row["arm"]), str(row["scenario_id"])))
        if scenario is None:
            raise ValueError("Sheet projection row is not installed in the shadow plan")
        evidence_hashes = [
            reference.evidence_hash for reference in scenario.chart_evidence_refs
        ]
        exact = {
            "program_id": scenario.program_id,
            "experiment_family_id": scenario.experiment_family_id,
            "experiment_version": scenario.experiment_version,
            "campaign_id": scenario.campaign_id,
            "run_id": scenario.run_id,
            "candidate_id": scenario.candidate_id,
            "symbol": scenario.symbol,
            "direction": scenario.direction.value,
            "exit_profile": scenario.exit_profile.value,
            "component_manifest_hash": scenario.component_manifest_hash,
            "candidate_pool_hash": scenario.candidate_pool_hash,
            "scenario_hash": scenario.scenario_hash,
            "exit_policy_hash": scenario.exit_policy_hash,
            "chart_evidence_hash": ",".join(evidence_hashes),
            "authorization_mode": "shadow",
            "source_type": "chart_scenario_experiment",
        }
        if any(str(row[field]) != str(expected) for field, expected in exact.items()):
            raise ValueError(
                "Sheet projection row identity differs from installed plan"
            )


def _index_existing_rows(
    rows: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str, str, str], int], list[int]]:
    keyed: dict[tuple[str, str, str, str], int] = {}
    empty: list[int] = []
    for row in rows:
        values = tuple(str(row.get(column, "")).strip() for column in KEY_COLUMNS)
        occupied = tuple(bool(value) for value in values)
        row_index = int(row["row_index"])
        if not any(occupied):
            # Preformatted rows may contain comparable=FALSE. Occupancy is
            # intentionally determined only by the four experiment keys.
            empty.append(row_index)
            continue
        if not all(occupied):
            raise ValueError(f"Sheet row {row_index} has a partial experiment key")
        key = values
        if key in keyed:
            raise ValueError(f"Sheet contains duplicate experiment key: {key}")
        keyed[key] = row_index
    return keyed, sorted(empty)


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    key = tuple(str(row.get(column, "")).strip() for column in KEY_COLUMNS)
    if any(not value for value in key):
        raise ValueError("Sheet projection row has a blank experiment key")
    return key  # type: ignore[return-value]


def _normalize_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    rendered = str(value)
    if rendered.upper() in {"TRUE", "FALSE"}:
        return rendered.upper()
    if re.fullmatch(r"-?\d+(?:\.\d+)?", rendered):
        integer, dot, fraction = rendered.partition(".")
        if dot:
            fraction = fraction.rstrip("0")
            return integer if not fraction else f"{integer}.{fraction}"
    return rendered


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    target = require_experiment_path(path, role="Sheet projection receipt")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


__all__ = [
    "HEADERS",
    "KEY_COLUMNS",
    "RECEIPT_SCHEMA",
    "REQUEST_SCHEMA",
    "SHEET_NAME",
    "SPREADSHEET_ID",
    "project_sheet_upsert_request",
    "validate_sheet_upsert_request",
]
